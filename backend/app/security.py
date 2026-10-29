import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

_hasher = PasswordHasher()
TokenType = Literal["access", "sse", "oauth_state"]
ALGORITHM = "HS256"


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_opaque_token() -> str:
    """Случайная строка для refresh/email/invite-токенов; в БД хранится только её хэш."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(
    subject: int | str,
    token_type: TokenType,
    now: datetime,
    ttl: timedelta | None = None,
    **claims: Any,
) -> str:
    s = get_settings()
    ttl = ttl or timedelta(minutes=s.jwt_access_ttl_minutes)
    payload = {"sub": str(subject), "type": token_type, "iat": now, "exp": now + ttl, **claims}
    return jwt.encode(payload, s.jwt_secret, algorithm=ALGORITHM)


def decode_token(
    token: str, expected_type: TokenType, now: datetime | None = None
) -> dict[str, Any]:
    """Бросает jwt.PyJWTError, если токен невалиден, просрочен или другого типа.

    `now` позволяет проверять срок по инжектируемым часам; без него берётся системное время.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub"], "verify_exp": now is None, "verify_iat": False},
    )
    if now is not None and payload["exp"] <= now.timestamp():
        raise jwt.ExpiredSignatureError("token expired")
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload
