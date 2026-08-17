from __future__ import annotations

import logging
from hashlib import sha256
from hmac import new as new_hmac
from threading import Lock
from uuid import UUID

import jwt
from jwt import InvalidTokenError
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.cache import CacheService
from app.core.config import settings
from app.core.security import ALGORITHM
from app.db.session import SessionLocal
from app.services.job_service import JobCapacityExceeded, JobService


UPLOAD_PATH_PREFIX = "/api/v1/datasets/project/"
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
logger = logging.getLogger("dataflow.uploads")


class UploadRequestTooLarge(MultiPartException):
    """Raised before an oversized request chunk reaches multipart parsing."""


class RequestBodyTooLarge(MultiPartException):
    """Raised before an oversized non-upload body reaches request parsing."""


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers", []) if key.lower() == name]


def _single_header(scope: Scope, name: bytes) -> str | None:
    values = _header_values(scope, name)
    if len(values) != 1:
        return None
    try:
        return values[0].decode("latin-1").strip()
    except UnicodeDecodeError:
        return None


def _declared_content_length(scope: Scope) -> int | None:
    values = _header_values(scope, b"content-length")
    if not values:
        return None
    try:
        parts = [
            part.strip()
            for value in values
            for part in value.decode("ascii").split(",")
        ]
        if not parts or any(not part or not part.isdigit() for part in parts):
            raise ValueError("Invalid Content-Length header.")
        lengths = {int(part) for part in parts}
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Invalid Content-Length header.") from exc
    if len(lengths) != 1:
        raise ValueError("Invalid Content-Length header.")
    return next(iter(lengths))


def _request_limit_bytes() -> int:
    return (
        settings.MAX_FILE_SIZE_MB + settings.UPLOAD_MULTIPART_OVERHEAD_MB
    ) * 1024 * 1024


def _general_request_limit_bytes() -> int:
    return settings.MAX_REQUEST_BODY_SIZE_MB * 1024 * 1024


def _matches_upload_endpoint(scope: Scope) -> bool:
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return False
    path = str(scope.get("path", ""))
    suffix = path.removeprefix(UPLOAD_PATH_PREFIX).rstrip("/")
    return path.startswith(UPLOAD_PATH_PREFIX) and bool(suffix) and "/" not in suffix


def _access_subject(scope: Scope) -> int | None:
    authorization = _single_header(scope, b"authorization")
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        return None
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
        )
        if payload.get("type", "access") != "access":
            return None
        subject_claim = payload.get("sub")
        if subject_claim is None:
            return None
        subject = int(subject_claim)
        return subject if subject > 0 else None
    except (InvalidTokenError, TypeError, ValueError):
        return None


def _client_fingerprint(scope: Scope) -> str:
    address = "unknown"
    if settings.TRUST_PROXY_HEADERS:
        forwarded = _single_header(scope, b"x-forwarded-for") or ""
        if forwarded:
            address = forwarded.split(",", 1)[0].strip()[:128] or "unknown"
    if address == "unknown":
        client = scope.get("client")
        if isinstance(client, tuple) and client:
            address = str(client[0])[:128]
    return new_hmac(
        settings.SECRET_KEY.encode("utf-8"),
        address.encode("utf-8"),
        sha256,
    ).hexdigest()[:32]


def _requested_task_id(scope: Scope) -> str | None:
    value = _single_header(scope, b"x-task-id")
    if not value:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _ensure_job_capacity(owner_id: int, exclude_task_id: str | None) -> None:
    db = SessionLocal()
    try:
        JobService.ensure_capacity(
            db,
            owner_id,
            exclude_task_id=exclude_task_id,
        )
    finally:
        db.close()


class RequestBodyLimitMiddleware:
    """Bound non-upload HTTP bodies before framework parsing or dependencies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        await JSONResponse(
            status_code=status_code,
            content={"detail": detail},
        )(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") not in BODY_METHODS
            or _matches_upload_endpoint(scope)
        ):
            await self.app(scope, receive, send)
            return

        request_limit = _general_request_limit_bytes()
        try:
            content_length = _declared_content_length(scope)
        except ValueError as exc:
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                detail=str(exc),
            )
            return
        if content_length is not None and content_length > request_limit:
            await self._reject(
                scope,
                receive,
                send,
                status_code=413,
                detail=(
                    "Request body exceeds the allowed size "
                    f"of {settings.MAX_REQUEST_BODY_SIZE_MB} MiB."
                ),
            )
            return

        received_bytes = 0
        request_too_large = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, request_too_large
            message = await receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > request_limit:
                    request_too_large = True
                    raise RequestBodyTooLarge("Request body is too large.")
            return message

        async def limited_send(message: Message) -> None:
            if not request_too_large:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except RequestBodyTooLarge:
            pass
        if request_too_large:
            await self._reject(
                scope,
                receive,
                send,
                status_code=413,
                detail=(
                    "Request body exceeds the allowed size "
                    f"of {settings.MAX_REQUEST_BODY_SIZE_MB} MiB."
                ),
            )


class UploadAdmissionMiddleware:
    """Bound upload requests before Starlette creates multipart spool files."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._active_uploads = 0
        self._active_uploads_lock = Lock()

    def _try_acquire_upload_slot(self) -> bool:
        with self._active_uploads_lock:
            if self._active_uploads >= settings.MAX_CONCURRENT_UPLOAD_REQUESTS:
                return False
            self._active_uploads += 1
            return True

    def _release_upload_slot(self) -> None:
        with self._active_uploads_lock:
            self._active_uploads = max(0, self._active_uploads - 1)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail},
            headers=headers,
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _matches_upload_endpoint(scope):
            await self.app(scope, receive, send)
            return

        request_limit = _request_limit_bytes()
        try:
            content_length = _declared_content_length(scope)
        except ValueError as exc:
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                detail=str(exc),
            )
            return
        if content_length is not None and content_length > request_limit:
            await self._reject(
                scope,
                receive,
                send,
                status_code=413,
                detail=(
                    "Upload request exceeds the allowed request size "
                    f"({settings.MAX_FILE_SIZE_MB} MiB file limit plus multipart overhead)."
                ),
            )
            return

        owner_id = _access_subject(scope)
        rate_limit_identity = (
            f"user:{owner_id}"
            if owner_id is not None
            else f"client:{_client_fingerprint(scope)}"
        )
        try:
            attempts = await run_in_threadpool(
                CacheService.increment,
                f"rate-limit:upload:{rate_limit_identity}",
                settings.UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
            )
        except Exception:
            logger.exception("Upload rate-limit storage is unavailable.")
            await self._reject(
                scope,
                receive,
                send,
                status_code=503,
                detail="Upload protection is temporarily unavailable.",
            )
            return
        if attempts > settings.UPLOAD_RATE_LIMIT_MAX_ATTEMPTS:
            await self._reject(
                scope,
                receive,
                send,
                status_code=429,
                detail="Too many uploads. Try again shortly.",
                headers={
                    "Retry-After": str(settings.UPLOAD_RATE_LIMIT_WINDOW_SECONDS)
                },
            )
            return

        if owner_id is not None:
            try:
                await run_in_threadpool(
                    _ensure_job_capacity,
                    owner_id,
                    _requested_task_id(scope),
                )
            except JobCapacityExceeded as exc:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=429,
                    detail=str(exc),
                    headers={
                        "Retry-After": str(settings.UPLOAD_RATE_LIMIT_WINDOW_SECONDS)
                    },
                )
                return
            except Exception:
                logger.exception(
                    "Upload job-capacity precheck failed for account %s.",
                    owner_id,
                )
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=503,
                    detail="Upload admission is temporarily unavailable.",
                )
                return

        if not self._try_acquire_upload_slot():
            await self._reject(
                scope,
                receive,
                send,
                status_code=429,
                detail="This API process is already receiving its maximum number of uploads.",
                headers={"Retry-After": "1"},
            )
            return

        received_bytes = 0
        request_too_large = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, request_too_large
            message = await receive()
            if message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > request_limit:
                    request_too_large = True
                    # Starlette's multipart parser closes any files it has
                    # already opened when it receives a MultiPartException.
                    raise UploadRequestTooLarge("Upload request is too large.")
            return message

        async def limited_send(message: Message) -> None:
            # Request._get_form converts MultiPartException into its own 400
            # response. Suppress that internal response and emit the public
            # 413 contract below after parser cleanup completes.
            if not request_too_large:
                await send(message)

        try:
            try:
                await self.app(scope, limited_receive, limited_send)
            except UploadRequestTooLarge:
                pass
            if request_too_large:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    detail=(
                        "Upload request exceeds the allowed request size "
                        f"({settings.MAX_FILE_SIZE_MB} MiB file limit plus multipart overhead)."
                    ),
                )
        finally:
            self._release_upload_slot()
