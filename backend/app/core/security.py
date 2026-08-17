from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"
BCRYPT_MAX_PASSWORD_BYTES = 72


def validate_bcrypt_password_length(password: str) -> str:
    if len(password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError("Password must not exceed 72 UTF-8 bytes.")
    return password

def hash_password(password: str) -> str:
    return pwd_context.hash(validate_bcrypt_password_length(password))

def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(
        validate_bcrypt_password_length(plain_password),
        password_hash,
    )

def _create_token(subject: str, token_type: str, expires_delta: timedelta, jti: str | None = None) -> str:
    expires_at = datetime.now(timezone.utc) + expires_delta
    payload = {"sub": subject, "exp": expires_at, "type": token_type, "jti": jti or uuid4().hex}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)

def create_access_token(subject: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return _create_token(subject, "access", expires_at - datetime.now(timezone.utc))

def create_refresh_token(subject: str, jti: str) -> str:
    return _create_token(subject, "refresh", timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), jti)
