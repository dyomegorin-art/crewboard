import uuid
from datetime import datetime, timedelta

from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError
from app.models import EmailToken, EmailTokenPurpose, RefreshToken, User
from app.outbox import enqueue
from app.schemas.auth import AuthOut, UserOut
from app.security import create_token, new_opaque_token, token_hash

REFRESH_COOKIE = "crewboard_refresh"
REFRESH_COOKIE_PATH = "/api/auth"
VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(minutes=30)


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        email_verified=user.email_verified_at is not None,
        is_demo=user.is_demo,
    )


def set_refresh_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=s.jwt_refresh_ttl_days * 86400,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=not s.is_local,
        samesite="lax",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


async def issue_refresh(
    session: AsyncSession, user_id: int, now: datetime, family_id: uuid.UUID | None = None
) -> tuple[str, RefreshToken]:
    token = new_opaque_token()
    row = RefreshToken(
        user_id=user_id,
        family_id=family_id or uuid.uuid4(),
        token_hash=token_hash(token),
        expires_at=now + timedelta(days=get_settings().jwt_refresh_ttl_days),
    )
    session.add(row)
    await session.flush()
    return token, row


async def revoke_family(session: AsyncSession, family_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def revoke_all_for_user(session: AsyncSession, user_id: int, now: datetime) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def rotate_refresh(
    session: AsyncSession, raw_token: str, now: datetime
) -> tuple[str, RefreshToken]:
    """Ротация: старый токен отзывается, выдаётся новый в том же семействе.

    Пришёл уже отозванный токен → его украли или он повторно воспроизведён: отзываем всё
    семейство и отвечаем 401 (коммит до исключения, иначе отзыв откатится).
    """
    old = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash(raw_token))
        .with_for_update()
    )
    invalid = AppError(401, "invalid_refresh_token", "Invalid or expired refresh token")
    if old is None:
        raise invalid
    if old.revoked_at is not None:
        await revoke_family(session, old.family_id, now)
        await session.commit()
        raise AppError(401, "refresh_token_reused", "Refresh token reuse detected")
    if old.expires_at <= now:
        raise invalid
    new_raw, new = await issue_refresh(session, old.user_id, now, old.family_id)
    old.revoked_at = now
    old.replaced_by_id = new.id
    return new_raw, new


def build_auth_out(user: User, now: datetime) -> AuthOut:
    return AuthOut(access_token=create_token(user.id, "access", now), user=user_out(user))


async def create_email_token(
    session: AsyncSession, user_id: int, purpose: EmailTokenPurpose, now: datetime
) -> str:
    token = new_opaque_token()
    ttl = VERIFY_TTL if purpose == EmailTokenPurpose.verify else RESET_TTL
    session.add(
        EmailToken(
            user_id=user_id, purpose=purpose, token_hash=token_hash(token), expires_at=now + ttl
        )
    )
    await session.flush()
    return token


async def queue_verification_email(session: AsyncSession, user: User, now: datetime) -> None:
    token = await create_email_token(session, user.id, EmailTokenPurpose.verify, now)
    link = f"{get_settings().public_url}/verify-email?token={token}"
    await enqueue(
        session,
        "SEND_EMAIL",
        {
            "to": user.email,
            "subject": "Подтвердите email в Crewboard",
            "body": f"Здравствуйте, {user.name}!\n\nПодтвердите email: {link}\n",
        },
        now=now,
    )


async def queue_reset_email(session: AsyncSession, user: User, now: datetime) -> None:
    token = await create_email_token(session, user.id, EmailTokenPurpose.reset, now)
    link = f"{get_settings().public_url}/reset-password?token={token}"
    await enqueue(
        session,
        "SEND_EMAIL",
        {
            "to": user.email,
            "subject": "Сброс пароля Crewboard",
            "body": f"Ссылка для сброса пароля (действует 30 минут): {link}\n"
            "Если вы не запрашивали сброс, просто проигнорируйте письмо.\n",
        },
        now=now,
    )


async def consume_email_token(
    session: AsyncSession, raw: str, purpose: EmailTokenPurpose, now: datetime
) -> EmailToken:
    """Одноразовый токен: атомарно помечается использованным, повтор → 400."""
    row = await session.scalar(
        select(EmailToken)
        .where(EmailToken.token_hash == token_hash(raw), EmailToken.purpose == purpose)
        .with_for_update()
    )
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise AppError(400, "invalid_token", "Token is invalid or expired")
    row.used_at = now
    return row
