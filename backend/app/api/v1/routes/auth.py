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


def issue_token_pair(db: Session, user: User) -> TokenResponse:
    jti = uuid4().hex
    session = RefreshSession(
        user_id=user.id,
        jti=jti,
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
        payload = jwt.decode(raw_token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            raise InvalidTokenError("Wrong token type")
        return payload
    except InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.") from exc


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    claims = decode_refresh(payload.refresh_token)
    now = datetime.now(timezone.utc)
    session = db.query(RefreshSession).filter(RefreshSession.jti == claims.get("jti")).first()
    subject = int(claims.get("sub", 0))
    if not session or session.user_id != subject:
        raise HTTPException(status_code=401, detail="Refresh session expired or revoked.")

    updated = (
        db.query(RefreshSession)
        .filter(
            RefreshSession.id == session.id,
            RefreshSession.revoked_at.is_(None),
            RefreshSession.expires_at > now,
        )
        .update({RefreshSession.revoked_at: now}, synchronize_session=False)
    )
    if updated != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="Refresh session expired or revoked.")

    user = db.query(User).filter(User.id == subject, User.is_active.is_(True)).first()
    if not user:
        db.rollback()
        raise HTTPException(status_code=401, detail="User not found.")
    return issue_token_pair(db, user)


@router.post("/logout", status_code=204)
def logout(payload: LogoutRequest, db: Session = Depends(get_db)):
    claims = decode_refresh(payload.refresh_token)
    now = datetime.now(timezone.utc)
    db.query(RefreshSession).filter(
        RefreshSession.jti == claims.get("jti"),
        RefreshSession.user_id == int(claims.get("sub", 0)),
        RefreshSession.revoked_at.is_(None),
    ).update({RefreshSession.revoked_at: now}, synchronize_session=False)
    db.commit()
    return None
