from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import get_jwt_expire_minutes, get_jwt_secret
from app.db import get_db
from app.models import PaperAccount, User
from app import strategy_config as cfg
from app.timeutil import ist_date_str, store_naive_ist

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=get_jwt_expire_minutes())
    payload = {"sub": str(user_id), "email": email, "exp": expire}
    return jwt.encode(payload, get_jwt_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_jwt_secret(), algorithms=[ALGORITHM])


def create_user_with_account(db: Session, email: str, password: str, auto_trade: bool = False) -> User:
    existing = db.query(User).filter(User.email == email.lower().strip()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        email=email.lower().strip(),
        password_hash=hash_password(password),
        auto_trade=auto_trade,
        is_active=True,
        created_at=store_naive_ist(),
        updated_at=store_naive_ist(),
    )
    db.add(user)
    db.flush()
    account = PaperAccount(
        user_id=user.id,
        initial_balance=cfg.INITIAL_BALANCE,
        realized_pnl=0.0,
        daily_starting_equity=cfg.INITIAL_BALANCE,
        daily_realized_pnl=0.0,
        daily_reset_date=ist_date_str(),
        consecutive_losses=0,
        created_at=store_naive_ist(),
        updated_at=store_naive_ist(),
    )
    db.add(account)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
    user = db.query(User).filter(User.email == email.lower().strip()).first()
    if not user or not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


def get_user_from_token(db: Session, token: str) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub") or 0)
    except (JWTError, ValueError, TypeError):
        raise credentials_exception
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise credentials_exception
    return user


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return get_user_from_token(db, credentials.credentials)


def ensure_seed_admin(db: Session, email: str, password: str) -> None:
    existing = db.query(User).filter(User.email == email.lower().strip()).first()
    if existing:
        return
    create_user_with_account(db, email, password, auto_trade=True)
