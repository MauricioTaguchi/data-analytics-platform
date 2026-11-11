"""Durable recovery delivery with encrypted secrets and recoverable leases.

SMTP cannot guarantee exactly-once delivery: a crash after SMTP acceptance but
before the database receipt may resend the same single-use link. No transaction
or account lock is held across SMTP I/O. Closing/resetting an account fences
future delivery and makes already accepted links unusable.
"""

import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import logging
import secrets
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.password_reset import PasswordResetToken
from app.models.password_reset_delivery import PasswordResetDelivery
from app.models.user import User

logger = logging.getLogger("dataflow.authentication")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 300
RETRY_BASE_SECONDS = 30
TERMINAL_RETENTION_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def recipient_key(email: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), b"recovery-recipient-v1:" + email.casefold().encode(), sha256).hexdigest()


def _cipher() -> Fernet:
    key = hmac.new(settings.SECRET_KEY.encode(), b"recovery-delivery-encryption-v1", sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _encrypt(payload: dict[str, str]) -> str:
    return _cipher().encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _decrypt(value: str | None) -> dict[str, str]:
    if not value:
        raise ValueError("Missing recovery payload.")
    payload = json.loads(_cipher().decrypt(value.encode()))
    if not isinstance(payload, dict) or not isinstance(payload.get("email"), str):
        raise ValueError("Invalid recovery payload.")
    return payload


def enqueue_reset_request(db: Session, email: str) -> None:
    """Persist the same-shaped record for every address, without account lookup."""
    now = _now()
    db.add(PasswordResetDelivery(
        id=uuid4().hex, recipient_key=recipient_key(email), kind="reset", state="pending",
        encrypted_payload=_encrypt({"email": email}), attempts=0, next_attempt_at=now,
        expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES),
    ))


def enqueue_password_changed(db: Session, user: User) -> None:
    now = _now()
    db.add(PasswordResetDelivery(
        id=uuid4().hex, recipient_key=recipient_key(user.email), user_id=user.id,
        auth_version=user.auth_version, kind="changed", state="pending",
        encrypted_payload=_encrypt({"email": user.email}), attempts=0,
        next_attempt_at=now, expires_at=now + timedelta(hours=24),
    ))


def cancel_account_deliveries(db: Session, user: User) -> None:
    """Call under the owner lock and before anonymizing the email; never commits."""
    db.query(PasswordResetDelivery).filter(
        or_(PasswordResetDelivery.user_id == user.id, PasswordResetDelivery.recipient_key == recipient_key(user.email)),
        PasswordResetDelivery.state.in_(["pending", "sending"]),
    ).update({
        PasswordResetDelivery.state: "cancelled", PasswordResetDelivery.encrypted_payload: None,
        PasswordResetDelivery.lease_id: None, PasswordResetDelivery.lease_until: None,
        PasswordResetDelivery.completed_at: _now(),
    }, synchronize_session=False)


def _terminal(db: Session, record: PasswordResetDelivery, state: str) -> None:
    record.state = state
    record.encrypted_payload = None
    record.lease_id = None
    record.lease_until = None
    record.completed_at = _now()
    if record.token_id and state != "sent":
        db.query(PasswordResetToken).filter(
            PasswordResetToken.id == record.token_id, PasswordResetToken.used_at.is_(None),
        ).update({PasswordResetToken.used_at: _now()}, synchronize_session=False)


def _claim() -> tuple[str, str] | None:
    now = _now()
    eligible = or_(
        and_(PasswordResetDelivery.state == "pending", PasswordResetDelivery.next_attempt_at <= now),
        and_(PasswordResetDelivery.state == "sending", PasswordResetDelivery.lease_until <= now),
    )
    with SessionLocal() as db:
        # Compare-and-swap works with both PostgreSQL and SQLite. A competing
        # scheduler may see the candidate, but only one acquires its lease.
        candidate = db.query(PasswordResetDelivery.id).filter(eligible).order_by(
            PasswordResetDelivery.next_attempt_at, PasswordResetDelivery.id,
        ).first()
        if candidate is None:
            return None
        lease = uuid4().hex
        claimed = db.query(PasswordResetDelivery).filter(
            PasswordResetDelivery.id == candidate.id, eligible,
        ).update({
            PasswordResetDelivery.state: "sending", PasswordResetDelivery.lease_id: lease,
            PasswordResetDelivery.lease_until: now + timedelta(seconds=LEASE_SECONDS),
            PasswordResetDelivery.attempts: PasswordResetDelivery.attempts + 1,
        }, synchronize_session=False)
        db.commit()
        return (candidate.id, lease) if claimed else None


def _prepare(record_id: str, lease: str) -> tuple[str, str, str | None] | None:
    with SessionLocal() as db:
        record = db.get(PasswordResetDelivery, record_id)
        if record is None or record.lease_id != lease or record.state != "sending":
            return None
        try:
            payload = _decrypt(record.encrypted_payload)
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            payload = None
        user = db.get(User, record.user_id) if record.user_id is not None else None
        if user is None and payload is not None:
            user = db.query(User).filter(User.email == payload["email"]).first()
        # Match authentication/reset/closure lock order: owner before delivery.
        if user is not None:
            db.query(User).filter(User.id == user.id).update({User.id: User.id}, synchronize_session=False)
            db.refresh(user)
        # A conditional write provides the same fenced row lock on SQLite and
        # PostgreSQL. Recheck after waiting; an expired lease or scrubbed request
        # must never receive a newly created token/encrypted payload.
        locked = db.query(PasswordResetDelivery).filter(
            PasswordResetDelivery.id == record_id, PasswordResetDelivery.lease_id == lease,
            PasswordResetDelivery.state == "sending", PasswordResetDelivery.lease_until > _now(),
        ).update({PasswordResetDelivery.id: record_id}, synchronize_session=False)
        if not locked:
            db.rollback()
            return None
        db.refresh(record)
        if payload is None or _aware(record.expires_at) <= _now() or record.attempts > MAX_ATTEMPTS:
            _terminal(db, record, "failed")
            db.commit()
            return None
        if (user is None or not user.is_active or user.email != payload["email"]
                or (record.user_id is not None and record.user_id != user.id)
                or (record.auth_version is not None and record.auth_version != user.auth_version)):
            _terminal(db, record, "cancelled")
            db.commit()
            return None
        if record.kind == "reset":
            if record.token_id is None:
                token = secrets.token_urlsafe(32)
                reset = PasswordResetToken(
                    user_id=user.id, token_hash=sha256(token.encode()).hexdigest(), expires_at=record.expires_at,
                )
                db.add(reset)
                db.flush()
                record.token_id = reset.id
                record.user_id = user.id
                record.auth_version = user.auth_version
                payload["token"] = token
                record.encrypted_payload = _encrypt(payload)
            else:
                reset = db.get(PasswordResetToken, record.token_id)
                if reset is None or reset.used_at is not None or _aware(reset.expires_at) <= _now():
                    _terminal(db, record, "cancelled")
                    db.commit()
                    return None
            token = payload.get("token")
            if not token or sha256(token.encode()).hexdigest() != reset.token_hash:
                _terminal(db, record, "failed")
                db.commit()
                return None
        elif record.kind == "changed":
            token = None
        else:
            _terminal(db, record, "failed")
            db.commit()
            return None
        kind, recipient = record.kind, user.email
        # The token and its encrypted deliverable become durable together.
        db.commit()
        return kind, recipient, token


def _finish(record_id: str, lease: str, *, accepted: bool) -> None:
    with SessionLocal() as db:
        record = db.get(PasswordResetDelivery, record_id)
        if record is None:
            return
        if record.user_id is not None:
            db.query(User).filter(User.id == record.user_id).update({User.id: User.id}, synchronize_session=False)
        record = db.query(PasswordResetDelivery).filter_by(id=record_id).with_for_update().populate_existing().first()
        if record is None or record.state != "sending" or record.lease_id != lease:
            return
        if accepted:
            if record.token_id:
                db.query(PasswordResetToken).filter(
                    PasswordResetToken.id == record.token_id, PasswordResetToken.used_at.is_(None),
                ).update({PasswordResetToken.delivered_at: _now()}, synchronize_session=False)
            _terminal(db, record, "sent")
        elif record.attempts >= MAX_ATTEMPTS or _aware(record.expires_at) <= _now():
            _terminal(db, record, "failed")
        else:
            record.state = "pending"
            record.lease_id = None
            record.lease_until = None
            record.next_attempt_at = _now() + timedelta(seconds=RETRY_BASE_SECONDS * 2 ** (record.attempts - 1))
        db.commit()


def _dispatch_recovery_mail() -> bool:
    """Deliver at most one due message; dependency failures leave durable state."""
    from app.services.password_reset_service import _delivery_configured, deliver_reset_link, notify_password_changed

    if not _delivery_configured():
        return False
    claim = _claim()
    if claim is None:
        return False
    prepared = _prepare(*claim)
    if prepared is None:
        return True
    kind, recipient, token = prepared
    try:
        if kind == "reset":
            assert token is not None
            deliver_reset_link(recipient, token)
        else:
            notify_password_changed(recipient)
    except Exception:
        # SMTP errors may include a full reset link, credentials or an address.
        logger.warning("Recovery mail delivery failed; bounded retry is scheduled.")
        _finish(*claim, accepted=False)
    else:
        _finish(*claim, accepted=True)
    return True


def dispatch_recovery_mail() -> bool:
    try:
        return _dispatch_recovery_mail()
    except Exception:
        # SQL/provider exceptions can contain personal data; preserve the lease
        # for recovery without exposing their parameterized statements.
        raise RuntimeError("Recovery mail dispatch is temporarily unavailable.") from None


def purge_recovery_delivery_history() -> int:
    """Expire stalled requests even with SMTP disabled and bound terminal history."""
    now = _now()
    with SessionLocal() as db:
        # Expired tokens are already unusable. Scrub delivery rows without
        # taking token locks in the opposite order to account reset/closure.
        db.query(PasswordResetDelivery).filter(
            PasswordResetDelivery.state.in_(["pending", "sending"]), PasswordResetDelivery.expires_at <= now,
        ).update({
            PasswordResetDelivery.state: "failed", PasswordResetDelivery.encrypted_payload: None,
            PasswordResetDelivery.lease_id: None, PasswordResetDelivery.lease_until: None,
            PasswordResetDelivery.completed_at: now,
        }, synchronize_session=False)
        removed = db.query(PasswordResetDelivery).filter(
            PasswordResetDelivery.completed_at < now - timedelta(days=TERMINAL_RETENTION_DAYS),
        ).delete(synchronize_session=False)
        db.commit()
        return removed
