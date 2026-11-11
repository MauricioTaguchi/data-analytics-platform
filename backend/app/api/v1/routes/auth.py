from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import ALGORITHM, create_access_token, create_refresh_token, hash_password, verify_password
from app.db.session import get_db
from app.models.session import RefreshSession
from app.models.user import User
from app.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest, RegisterRequest, TokenResponse

router = APIRouter()


def issue_token_pair(db: Session, user: User, *, family_id: str | None = None) -> TokenResponse:
    jti = uuid4().hex
    session = RefreshSession(
        user_id=user.id,
        jti=jti,
        family_id=family_id or uuid4().hex,
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(session)
    db.commit()
    return TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id), jti),
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=409, detail="Email is already registered.")
    user = User(name=payload.name, email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return issue_token_pair(db, user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    return issue_token_pair(db, user)


def decode_refresh(raw_token: str) -> dict:
    try:
        payload = jwt.decode(
            raw_token,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp", "type", "jti"]},
        )
        if payload.get("type") != "refresh":
            raise InvalidTokenError("Wrong token type")
        subject = payload["sub"]
        if (
            not isinstance(subject, str)
            or not subject.isascii()
            or not subject.isdigit()
            or len(subject) > 19
            or not 0 < int(subject) <= 2**63 - 1
        ):
            raise InvalidTokenError("Invalid subject")
        if not isinstance(payload["jti"], str) or not 0 < len(payload["jti"]) <= 64:
            raise InvalidTokenError("Invalid session identifier")
        return payload
    except (InvalidTokenError, TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.") from exc


def _lock_refresh_session(db: Session, claims: dict) -> RefreshSession | None:
    subject = int(claims["sub"])
    query = db.query(RefreshSession).filter(
        RefreshSession.jti == claims["jti"], RefreshSession.user_id == subject
    )
    if query.first() is None:
        return None

    # Serialize rotation and family revocation across different tokens. A write
    # locks the owner row on PostgreSQL and also works on SQLite, where FOR UPDATE
    # is ignored. Reload after waiting so a concurrent rotation cannot be missed.
    db.query(User).filter(User.id == subject).update({User.id: User.id}, synchronize_session=False)
    return query.populate_existing().first()


def _session_expired(session: RefreshSession, claims: dict, now: datetime) -> bool:
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    # The token may expire while this request waits for another rotation's lock.
    return expires_at <= now or int(claims["exp"]) <= now.timestamp()


def _revoke_family(db: Session, session: RefreshSession, now: datetime) -> None:
    db.query(RefreshSession).filter(
        RefreshSession.user_id == session.user_id,
        RefreshSession.family_id == session.family_id,
        RefreshSession.revoked_at.is_(None),
    ).update({RefreshSession.revoked_at: now}, synchronize_session=False)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    claims = decode_refresh(payload.refresh_token)
    session = _lock_refresh_session(db, claims)
    now = datetime.now(timezone.utc)
    if not session or _session_expired(session, claims, now):
        db.rollback()
        raise HTTPException(status_code=401, detail="Refresh session expired or revoked.")
    if session.consumed_at is not None:
        _revoke_family(db, session, now)
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh session expired or revoked.")

    updated = (
        db.query(RefreshSession)
        .filter(
            RefreshSession.id == session.id,
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > now,
        )
        .update(
            {RefreshSession.revoked_at: now, RefreshSession.consumed_at: now},
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="Refresh session expired or revoked.")

    user = db.query(User).filter(User.id == session.user_id, User.is_active.is_(True)).first()
    if not user:
        db.rollback()
        raise HTTPException(status_code=401, detail="User not found.")
    return issue_token_pair(db, user, family_id=session.family_id)


@router.post("/logout", status_code=204)
def logout(payload: LogoutRequest, db: Session = Depends(get_db)):
    claims = decode_refresh(payload.refresh_token)
    session = _lock_refresh_session(db, claims)
    now = datetime.now(timezone.utc)
    if session and not _session_expired(session, claims, now):
        _revoke_family(db, session, now)
    db.commit()
    return None
