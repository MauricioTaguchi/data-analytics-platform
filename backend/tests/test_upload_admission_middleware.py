import asyncio
import json

from starlette.responses import JSONResponse

from app.core import upload_guard
from app.core.cache import CacheService
from app.core.config import settings
from app.core.security import create_access_token
from app.core.upload_guard import (
    RequestBodyLimitMiddleware,
    UploadAdmissionMiddleware,
)
from app.services.job_service import JobCapacityExceeded


def upload_scope(
    *,
    headers=None,
    method="POST",
    path="/api/v1/datasets/project/1",
):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 50_000),
        "server": ("testserver", 80),
    }


def response_details(messages):
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    headers = dict(
        next(
            message["headers"]
            for message in messages
            if message["type"] == "http.response.start"
        )
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, headers, json.loads(body)


def test_declared_size_is_rejected_without_reading_the_body(monkeypatch):
    downstream_calls = []
    receive_calls = []

    async def downstream(_scope, _receive, _send):
        downstream_calls.append(True)

    async def receive():
        receive_calls.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    messages = []
    monkeypatch.setattr(upload_guard, "_request_limit_bytes", lambda: 8)
    monkeypatch.setattr(
        CacheService,
        "increment",
        lambda *_args: (_ for _ in ()).throw(AssertionError("rate limit ran")),
    )
    middleware = UploadAdmissionMiddleware(downstream)
    scope = upload_scope(headers=[(b"content-length", b"9")])

    asyncio.run(middleware(scope, receive, messages.append))

    status, _headers, body = response_details(messages)
    assert status == 413
    assert "allowed request size" in body["detail"]
    assert not receive_calls
    assert not downstream_calls


def test_streamed_size_is_rejected_before_the_excess_chunk_reaches_parser(
    monkeypatch,
):
    parsed_bytes = []
    chunks = iter(
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        ]
    )

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            parsed_bytes.append(len(message.get("body", b"")))
            if not message.get("more_body", False):
                break
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        return next(chunks)

    messages = []
    monkeypatch.setattr(upload_guard, "_request_limit_bytes", lambda: 8)
    monkeypatch.setattr(CacheService, "increment", lambda *_args: 1)
    middleware = UploadAdmissionMiddleware(downstream)

    asyncio.run(middleware(upload_scope(), receive, messages.append))

    status, _headers, _body = response_details(messages)
    assert status == 413
    assert parsed_bytes == [5]


def test_upload_rate_limit_rejects_before_body_receive(monkeypatch):
    downstream_calls = []
    receive_calls = []

    async def downstream(_scope, _receive, _send):
        downstream_calls.append(True)

    async def receive():
        receive_calls.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    messages = []
    monkeypatch.setattr(upload_guard, "_request_limit_bytes", lambda: 100)
    monkeypatch.setattr(settings, "UPLOAD_RATE_LIMIT_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "UPLOAD_RATE_LIMIT_WINDOW_SECONDS", 90)
    monkeypatch.setattr(CacheService, "increment", lambda *_args: 2)

    asyncio.run(
        UploadAdmissionMiddleware(downstream)(
            upload_scope(),
            receive,
            messages.append,
        )
    )

    status, headers, _body = response_details(messages)
    assert status == 429
    assert headers[b"retry-after"] == b"90"
    assert not receive_calls
    assert not downstream_calls


def test_upload_cache_failure_returns_503_without_receiving_body(monkeypatch):
    receive_calls = []

    async def downstream(_scope, _receive, _send):
        raise AssertionError("downstream should not run")

    async def receive():
        receive_calls.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    def unavailable_cache(*_args):
        raise RuntimeError("redis unavailable")

    messages = []
    monkeypatch.setattr(CacheService, "increment", unavailable_cache)

    asyncio.run(
        UploadAdmissionMiddleware(downstream)(
            upload_scope(),
            receive,
            messages.append,
        )
    )

    status, _headers, body = response_details(messages)
    assert status == 503
    assert "protection is temporarily unavailable" in body["detail"]
    assert not receive_calls


def test_valid_token_job_cap_is_prechecked_without_receiving_body(monkeypatch):
    owner_id = 42
    checked = []
    receive_calls = []
    token = create_access_token(str(owner_id))

    async def downstream(_scope, _receive, _send):
        raise AssertionError("downstream should not run")

    async def receive():
        receive_calls.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    def reject_capacity(received_owner_id, exclude_task_id):
        checked.append((received_owner_id, exclude_task_id))
        raise JobCapacityExceeded("Too many jobs are already active for this account.")

    messages = []
    monkeypatch.setattr(CacheService, "increment", lambda *_args: 1)
    monkeypatch.setattr(upload_guard, "_ensure_job_capacity", reject_capacity)
    scope = upload_scope(
        headers=[
            (b"authorization", f"Bearer {token}".encode("ascii")),
            (b"x-task-id", b"123e4567-e89b-12d3-a456-426614174000"),
        ]
    )

    asyncio.run(
        UploadAdmissionMiddleware(downstream)(scope, receive, messages.append)
    )

    status, _headers, body = response_details(messages)
    assert status == 429
    assert "jobs are already active" in body["detail"]
    assert checked == [(owner_id, "123e4567-e89b-12d3-a456-426614174000")]
    assert not receive_calls


def test_job_precheck_failure_returns_503_without_receiving_body(monkeypatch):
    receive_calls = []
    token = create_access_token("42")

    async def downstream(_scope, _receive, _send):
        raise AssertionError("downstream should not run")

    async def receive():
        receive_calls.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    def unavailable_database(*_args):
        raise RuntimeError("database unavailable")

    messages = []
    monkeypatch.setattr(CacheService, "increment", lambda *_args: 1)
    monkeypatch.setattr(upload_guard, "_ensure_job_capacity", unavailable_database)
    scope = upload_scope(
        headers=[(b"authorization", f"Bearer {token}".encode("ascii"))]
    )

    asyncio.run(
        UploadAdmissionMiddleware(downstream)(scope, receive, messages.append)
    )

    status, _headers, body = response_details(messages)
    assert status == 503
    assert "admission is temporarily unavailable" in body["detail"]
    assert not receive_calls


def test_upload_concurrency_cap_is_process_local_and_nonblocking(monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENT_UPLOAD_REQUESTS", 1)
    monkeypatch.setattr(CacheService, "increment", lambda *_args: 1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def downstream(scope, receive, send):
        entered.set()
        await release.wait()
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def scenario():
        middleware = UploadAdmissionMiddleware(downstream)
        first_messages = []
        second_messages = []
        first = asyncio.create_task(
            middleware(upload_scope(), receive, first_messages.append)
        )
        await entered.wait()
        await middleware(upload_scope(), receive, second_messages.append)
        release.set()
        await first
        return first_messages, second_messages

    first_messages, second_messages = asyncio.run(scenario())
    first_status, _headers, _body = response_details(first_messages)
    second_status, second_headers, second_body = response_details(second_messages)
    assert first_status == 200
    assert second_status == 429
    assert second_headers[b"retry-after"] == b"1"
    assert "API process" in second_body["detail"]


def test_registered_app_rejects_declared_oversize_before_authentication(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 1)
    monkeypatch.setattr(settings, "UPLOAD_MULTIPART_OVERHEAD_MB", 1)
    monkeypatch.setattr(
        CacheService,
        "increment",
        lambda *_args: (_ for _ in ()).throw(AssertionError("body gate was late")),
    )

    response = client.post(
        "/api/v1/datasets/project/1",
        content=b"x" * ((2 * 1024 * 1024) + 1),
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413


def test_registered_app_returns_413_for_chunked_multipart_overflow(
    client,
    monkeypatch,
):
    boundary = "upload-guard-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="data.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    monkeypatch.setattr(upload_guard, "_request_limit_bytes", lambda: len(prefix) + 3)
    monkeypatch.setattr(CacheService, "increment", lambda *_args: 1)

    def chunks():
        yield prefix + b"a\n1"
        yield b"\n2\n" + suffix

    response = client.post(
        "/api/v1/datasets/project/1",
        content=chunks(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 413
    assert "allowed request size" in response.json()["detail"]


def test_general_body_limit_rejects_declared_size_before_receive(monkeypatch):
    receive_calls = []
    downstream_calls = []

    async def downstream(_scope, _receive, _send):
        downstream_calls.append(True)

    async def receive():
        receive_calls.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    messages = []
    monkeypatch.setattr(upload_guard, "_general_request_limit_bytes", lambda: 8)
    scope = upload_scope(
        path="/api/v1/auth/login",
        headers=[(b"content-length", b"9")],
    )

    asyncio.run(
        RequestBodyLimitMiddleware(downstream)(scope, receive, messages.append)
    )

    status, _headers, body = response_details(messages)
    assert status == 413
    assert "Request body exceeds" in body["detail"]
    assert not receive_calls
    assert not downstream_calls


def test_general_body_limit_counts_chunked_body_before_parser(monkeypatch):
    parsed_bytes = []
    chunks = iter(
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        ]
    )

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            parsed_bytes.append(len(message.get("body", b"")))
            if not message.get("more_body", False):
                break
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        return next(chunks)

    messages = []
    monkeypatch.setattr(upload_guard, "_general_request_limit_bytes", lambda: 8)
    scope = upload_scope(path="/api/v1/auth/login")

    asyncio.run(
        RequestBodyLimitMiddleware(downstream)(scope, receive, messages.append)
    )

    status, _headers, _body = response_details(messages)
    assert status == 413
    assert parsed_bytes == [5]


def test_general_body_limit_ignores_methods_without_request_bodies(monkeypatch):
    receive_calls = []

    async def downstream(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        receive_calls.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    messages = []
    monkeypatch.setattr(upload_guard, "_general_request_limit_bytes", lambda: 8)
    scope = upload_scope(
        method="GET",
        path="/health",
        headers=[(b"content-length", b"9")],
    )

    asyncio.run(
        RequestBodyLimitMiddleware(downstream)(scope, receive, messages.append)
    )

    status, _headers, _body = response_details(messages)
    assert status == 200
    assert not receive_calls


def test_general_body_limit_preserves_upload_specific_limit(monkeypatch):
    downstream_calls = []

    async def downstream(scope, receive, send):
        downstream_calls.append(True)
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    messages = []
    monkeypatch.setattr(upload_guard, "_general_request_limit_bytes", lambda: 8)
    scope = upload_scope(headers=[(b"content-length", b"9")])

    asyncio.run(
        RequestBodyLimitMiddleware(downstream)(scope, receive, messages.append)
    )

    status, _headers, _body = response_details(messages)
    assert status == 200
    assert downstream_calls == [True]


def test_registered_general_body_limit_runs_before_json_parsing(
    client,
    monkeypatch,
):
    monkeypatch.setattr(settings, "MAX_REQUEST_BODY_SIZE_MB", 1)
    monkeypatch.setattr(CacheService, "increment", lambda *_args: 1)

    response = client.post(
        "/api/v1/auth/login",
        content=b"x" * ((1024 * 1024) + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert "Request body exceeds" in response.json()["detail"]
