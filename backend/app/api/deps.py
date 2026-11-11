import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import ALGORITHM
from app.db.session import get_db
from app.models.user import User

security = HTTPBearer()

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
        if payload.get("type", "access") != "access":
            raise InvalidTokenError("Wrong token type")
        subject = payload.get("sub")
        if (not isinstance(subject, str) or not subject.isascii() or not subject.isdigit()
                or len(subject) > 19 or not 0 < int(subject) <= 2**63 - 1):
            raise InvalidTokenError("Invalid subject")
        user_id = int(subject)
    except (InvalidTokenError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
        ) from exc

    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found.")
    version = payload.get("ver", 0)
    if type(version) is not int or version != user.auth_version:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    return user
