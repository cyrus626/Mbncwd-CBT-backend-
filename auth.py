import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

import config  # noqa: F401  (loads the .env file)
from database import get_db
from models import Role, User

SECRET_KEY = os.environ.get("SECRET_KEY")  # long random string, never commit it
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set. Copy .env.example to .env and put a long random value in it "
        '(generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))").'
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 8 * 60  # long enough for any test; a 60-minute login would end long tests midway

_hasher = PasswordHash.recommended()  # argon2
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _hasher.verify(password, password_hash)


def create_access_token(user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_MINUTES)
    payload = {"sub": str(user.id), "role": user.role.value, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    error = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise error

    # Load from the DB every time, so a deactivated user is locked out immediately
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise error
    return user


def require_role(*roles: Role):
    def checker(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not allowed to do this")
        return user

    return checker


require_admin = require_role(Role.ADMIN)
require_participant = require_role(Role.PARTICIPANT)

# Usage in a route:
#   @router.post("/tests")
#   def create_test(data: TestIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)): ...
