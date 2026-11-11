"""Single-use recovery links; only their digests are persisted."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from hashlib import sha256
import hmac
import logging
import re
import secrets
import smtplib
import ssl
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.core.cache import CacheService
from app.core.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.password_reset import PasswordResetToken
from app.models.session import RefreshSession
from app.models.user import User

logger = logging.getLogger("dataflow.authentication")
RESET_REQUEST_MESSAGE = "If an eligible account exists, password reset instructions will be sent."
INVALID_RESET_MESSAGE = "Invalid or expired password reset token."


def _delivery_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_FROM_EMAIL and settings.PASSWORD_RESET_URL)


def _send_message(recipient: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    connection: smtplib.SMTP
    if settings.SMTP_TLS_MODE == "ssl":
        connection = smtplib.SMTP_SSL(
            settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS, context=context,
        )
    else:
        connection = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS)
    with connection as smtp:
        if settings.SMTP_TLS_MODE == "starttls":
            smtp.starttls(context=context)
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD.get_secret_value())
        if smtp.send_message(message):
            raise OSError("The recovery message was not accepted.")


def deliver_reset_link(recipient: str, token: str) -> None:
    link = f"{settings.PASSWORD_RESET_URL}#{urlencode({'reset_token': token})}"
    _send_message(
        recipient,
        "Reset your DataFlow password",
        f"A password reset was requested for your DataFlow account.\n\n"
        f"Use this link within {settings.PASSWORD_RESET_EXPIRE_MINUTES} minutes:\n{link}\n\n"
        "This link can be used once. If you did not request it, ignore this message. "
        "Your password and existing sessions have not been changed.",
    )


def notify_password_changed(recipient: str) -> None:
    if not _delivery_configured():
        return
    try:
        _send_message(
            recipient,
            "Your DataFlow password was changed",
            "Your DataFlow password was reset. Existing sessions and outstanding recovery links have been invalidated. "
            "Sign in again with your new password. If you did not make this change, request a new recovery link "
            "and secure access to your email account.",
        )
    except Exception:
        # Provider exceptions can contain recipients, credentials, or message bodies.
        logger.warning("Password change notification could not be delivered.")


def request_password_reset(email: str) -> None:
    """Run after the HTTP response; do not reveal account lookup/delivery timing."""
    token_hash: str | None = None
    try:
        key = hmac.new(settings.SECRET_KEY.encode(), email.casefold().encode(), sha256).hexdigest()
        if CacheService.increment(f"password-reset:{key}", settings.PASSWORD_RESET_REQUEST_COOLDOWN_SECONDS) > 1:
            return
        if not _delivery_configured():
            logger.warning("Password recovery delivery is not configured.")
            return
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == email, User.is_active.is_(True)).first()
            if user is None:
                return
            # Share the owner lock with login, refresh, and password confirmation.
            db.query(User).filter(User.id == user.id).update({User.id: User.id}, synchronize_session=False)
            db.refresh(user)
            if not user.is_active:
                db.rollback()
                return
            now = datetime.now(timezone.utc)
            db.query(PasswordResetToken).filter(
                PasswordResetToken.user_id == user.id, PasswordResetToken.expires_at <= now,
            ).delete(synchronize_session=False)
            token = secrets.token_urlsafe(32)
            token_hash = sha256(token.encode()).hexdigest()
            db.add(PasswordResetToken(
                user_id=user.id, token_hash=token_hash,
                expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES),
            ))
            recipient = user.email
            db.commit()
        # No database transaction or account lock is held during SMTP I/O.
        deliver_reset_link(recipient, token)
        with SessionLocal() as db:
            db.query(PasswordResetToken).filter(
                PasswordResetToken.token_hash == token_hash, PasswordResetToken.used_at.is_(None),
            ).update({PasswordResetToken.delivered_at: datetime.now(timezone.utc)}, synchronize_session=False)
            db.commit()
    except Exception:
        if token_hash:
            try:
                with SessionLocal() as db:
                    db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).update(
                        {PasswordResetToken.used_at: datetime.now(timezone.utc)}, synchronize_session=False,
                    )
                    db.commit()
            except Exception:
                # Undelivered records are unusable even if cleanup cannot commit.
                pass
        logger.warning("Password recovery request could not be completed.")


def confirm_password_reset(db: Session, token: str, new_password: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError(INVALID_RESET_MESSAGE)
    token_hash = sha256(token.encode()).hexdigest()
    record = db.query(PasswordResetToken).filter_by(token_hash=token_hash).first()
    if record is None:
        raise ValueError(INVALID_RESET_MESSAGE)
    # CPU work occurs before the lock. Eligibility is checked again below.
    password_hash = hash_password(new_password)
    db.query(User).filter(User.id == record.user_id).update({User.id: User.id}, synchronize_session=False)
    record = db.query(PasswordResetToken).filter_by(token_hash=token_hash).populate_existing().first()
    now = datetime.now(timezone.utc)
    user = db.query(User).filter(User.id == record.user_id).populate_existing().first() if record else None
    expires_at = record.expires_at if record else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (record is None or record.used_at is not None or record.delivered_at is None
            or expires_at <= now or user is None or not user.is_active):
        db.rollback()
        raise ValueError(INVALID_RESET_MESSAGE)
    user.password_hash = password_hash
    user.auth_version += 1
    db.query(RefreshSession).filter(
        RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None),
    ).update({RefreshSession.revoked_at: now}, synchronize_session=False)
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None),
    ).update({PasswordResetToken.used_at: now}, synchronize_session=False)
    recipient = user.email
    db.commit()
    return recipient
