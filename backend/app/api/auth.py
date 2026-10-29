import secrets
from typing import Any

import jwt
from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_csrf_header
from app.clock import Clock, get_clock
from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.models import EmailTokenPurpose, OAuthAccount, RefreshToken, User
from app.ratelimit import check_rate_limit
from app.redis import get_redis
from app.schemas.auth import (
    AuthOut,
    LoginIn,
    ProvidersOut,
    RegisterIn,
    ResetConfirmIn,
    ResetRequestIn,
    TokenIn,
    UserOut,
)
from app.schemas.errors import ErrorResponse
from app.security import create_token, decode_token, hash_password, token_hash, verify_password
from app.services import github
from app.services.auth import (
    REFRESH_COOKIE,
    build_auth_out,
    clear_refresh_cookie,
    consume_email_token,
    issue_refresh,
    queue_reset_email,
    queue_verification_email,
    revoke_all_for_user,
    revoke_family,
    rotate_refresh,
    set_refresh_cookie,
    user_out,
)
from app.services.workspaces import create_workspace

router = APIRouter(prefix="/auth", tags=["auth"])

OAUTH_STATE_COOKIE = "crewboard_oauth_state"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _login_response(
    session: AsyncSession, response: Response, user: User, clock: Clock
) -> AuthOut:
    now = await clock.now()
    raw, _ = await issue_refresh(session, user.id, now)
    await session.commit()
    set_refresh_cookie(response, raw)
    return build_auth_out(user, now)


@router.get("/providers", response_model=ProvidersOut, summary="Какие способы входа включены")
async def providers() -> ProvidersOut:
    return ProvidersOut(github=get_settings().github_enabled)


@router.post(
    "/register",
    status_code=201,
    response_model=AuthOut,
    summary="Регистрация: создаёт пользователя и личный воркспейс",
    responses={409: {"model": ErrorResponse, "description": "Email уже занят"}},
)
async def register(
    body: RegisterIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    clock: Clock = Depends(get_clock),
) -> AuthOut:
    await check_rate_limit(
        redis, "register", _ip(request), get_settings().rate_limit_register_per_minute
    )
    email = body.email.lower()
    if await session.scalar(select(User.id).where(func.lower(User.email) == email)) is not None:
        raise AppError(409, "email_taken", "Email is already registered")
    now = await clock.now()
    user = User(email=email, password_hash=await hash_password(body.password), name=body.name)
    session.add(user)
    try:
        await session.flush()
        await create_workspace(session, user, f"{body.name}'s workspace", now)
        await queue_verification_email(session, user, now)
        raw, _ = await issue_refresh(session, user.id, now)
        await session.commit()
    except IntegrityError:  # гонка двух регистраций с одним email: UNIQUE в БД решает
        await session.rollback()
        raise AppError(409, "email_taken", "Email is already registered") from None
    set_refresh_cookie(response, raw)
    return build_auth_out(user, now)


@router.post(
    "/login",
    response_model=AuthOut,
    summary="Вход по email и паролю",
    responses={
        401: {"model": ErrorResponse, "description": "Неверный email или пароль"},
        429: {"model": ErrorResponse, "description": "Слишком много попыток"},
    },
)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    clock: Clock = Depends(get_clock),
) -> AuthOut:
    await check_rate_limit(
        redis,
        "login",
        f"{_ip(request)}:{body.email.lower()}",
        get_settings().rate_limit_login_per_minute,
    )
    user = await session.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    if (
        user is None
        or user.password_hash is None
        or not await verify_password(body.password, user.password_hash)
    ):
        raise AppError(401, "invalid_credentials", "Invalid email or password")
    return await _login_response(session, response, user, clock)


@router.post(
    "/refresh",
    summary="Ротация refresh-токена и новый access-токен",
    response_model=AuthOut,
    responses={401: {"model": ErrorResponse, "description": "Невалидный или повторный токен"}},
)
async def refresh(
    request: Request,
    response: Response,
    cookie: str | None = Cookie(None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    clock: Clock = Depends(get_clock),
) -> AuthOut:
    require_csrf_header(request)
    await check_rate_limit(
        redis, "refresh", _ip(request), get_settings().rate_limit_refresh_per_minute
    )
    if not cookie:
        raise AppError(401, "invalid_refresh_token", "Invalid or expired refresh token")
    now = await clock.now()
    try:
        new_raw, row = await rotate_refresh(session, cookie, now)
    except AppError:
        clear_refresh_cookie(response)
        raise
    user = await session.get(User, row.user_id)
    if user is None:
        raise AppError(401, "invalid_refresh_token", "Invalid or expired refresh token")
    await session.commit()
    set_refresh_cookie(response, new_raw)
    return build_auth_out(user, now)


@router.post("/logout", status_code=204, summary="Отозвать текущую сессию")
async def logout(
    request: Request,
    response: Response,
    cookie: str | None = Cookie(None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> None:
    require_csrf_header(request)
    if cookie:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash(cookie), RefreshToken.revoked_at.is_(None))
            .values(revoked_at=await clock.now())
        )
        await session.commit()
    clear_refresh_cookie(response)


@router.post("/logout-all", status_code=204, summary="Отозвать все сессии пользователя")
async def logout_all(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> None:
    require_csrf_header(request)
    await revoke_all_for_user(session, user.id, await clock.now())
    await session.commit()
    clear_refresh_cookie(response)


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return user_out(user)


@router.post(
    "/verify-email",
    response_model=UserOut,
    summary="Подтвердить email по токену из письма",
    responses={400: {"model": ErrorResponse, "description": "Токен недействителен"}},
)
async def verify_email(
    body: TokenIn,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> UserOut:
    now = await clock.now()
    row = await consume_email_token(session, body.token, EmailTokenPurpose.verify, now)
    user = await session.get(User, row.user_id)
    assert user is not None
    user.email_verified_at = user.email_verified_at or now
    await session.commit()
    return user_out(user)


@router.post(
    "/resend-verification", status_code=202, summary="Отправить письмо подтверждения ещё раз"
)
async def resend_verification(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    await check_rate_limit(redis, "resend", str(user.id), 3)
    if user.email_verified_at is None:
        await queue_verification_email(session, user, await clock.now())
        await session.commit()
    return {"status": "sent"}


@router.post(
    "/password-reset/request",
    status_code=202,
    summary="Запросить сброс пароля (ответ не зависит от существования email)",
)
async def password_reset_request(
    body: ResetRequestIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    await check_rate_limit(redis, "reset", _ip(request), get_settings().rate_limit_reset_per_minute)
    user = await session.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    if user is not None and not user.is_demo:
        await queue_reset_email(session, user, await clock.now())
        await session.commit()
    return {"status": "accepted"}


@router.post(
    "/password-reset/confirm",
    status_code=204,
    summary="Установить новый пароль по токену",
    responses={400: {"model": ErrorResponse, "description": "Токен недействителен"}},
)
async def password_reset_confirm(
    body: ResetConfirmIn,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> None:
    now = await clock.now()
    row = await consume_email_token(session, body.token, EmailTokenPurpose.reset, now)
    user = await session.get(User, row.user_id)
    assert user is not None
    user.password_hash = await hash_password(body.password)
    # Сброс — признак компрометации: все сессии отзываются.
    await revoke_all_for_user(session, user.id, now)
    await session.commit()


@router.post("/demo", response_model=AuthOut, status_code=201, summary="Демо-вход без регистрации")
async def demo_login(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    clock: Clock = Depends(get_clock),
) -> AuthOut:
    from app.services.demo import create_demo_workspace

    await check_rate_limit(redis, "demo", _ip(request), get_settings().rate_limit_demo_per_minute)
    user = await create_demo_workspace(session, await clock.now())
    return await _login_response(session, response, user, clock)


# --- GitHub OAuth ------------------------------------------------------------------------


def _github_or_404() -> None:
    if not get_settings().github_enabled:
        raise AppError(404, "oauth_disabled", "GitHub login is not enabled")


@router.get("/github/login", summary="Начать вход через GitHub")
async def github_login(clock: Clock = Depends(get_clock)) -> RedirectResponse:
    _github_or_404()
    nonce = secrets.token_urlsafe(16)
    signed = create_token(nonce, "oauth_state", await clock.now(), ttl=github.STATE_TTL)
    redirect = RedirectResponse(github.authorize_url(nonce), status_code=302)
    redirect.set_cookie(
        OAUTH_STATE_COOKIE,
        signed,
        max_age=int(github.STATE_TTL.total_seconds()),
        path="/api/auth/github",
        httponly=True,
        secure=not get_settings().is_local,
        samesite="lax",
    )
    return redirect


def _oauth_error(code: str) -> RedirectResponse:
    r = RedirectResponse(f"{get_settings().public_url}/login?error={code}", status_code=302)
    r.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/github")
    return r


@router.get("/github/callback", summary="Callback GitHub OAuth (authorization code + state)")
async def github_callback(
    code: str = "",
    state: str = "",
    state_cookie: str | None = Cookie(None, alias=OAUTH_STATE_COOKIE),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> RedirectResponse:
    _github_or_404()
    now = await clock.now()
    try:
        payload: dict[str, Any] = decode_token(state_cookie or "", "oauth_state", now)
    except jwt.PyJWTError:
        return _oauth_error("invalid_state")
    if not state or not secrets.compare_digest(payload["sub"], state) or not code:
        return _oauth_error("invalid_state")
    try:
        profile = await github.fetch_profile(code)
    except github.GithubError:
        return _oauth_error("github_failed")

    account = await session.scalar(
        select(OAuthAccount).where(
            OAuthAccount.provider == "github", OAuthAccount.provider_account_id == profile.id
        )
    )
    user: User | None = None
    if account is not None:
        user = await session.get(User, account.user_id)
    else:
        existing = await session.scalar(
            select(User).where(func.lower(User.email) == profile.email.lower())
        )
        if existing is not None and existing.email_verified_at is None:
            # Привязка к неподтверждённому аккаунту позволила бы захватить чужой email.
            return _oauth_error("email_unverified")
        if existing is None:
            existing = User(email=profile.email.lower(), name=profile.name, email_verified_at=now)
            session.add(existing)
            await session.flush()
            await create_workspace(session, existing, f"{profile.name}'s workspace", now)
        session.add(
            OAuthAccount(user_id=existing.id, provider="github", provider_account_id=profile.id)
        )
        user = existing
    if user is None:
        return _oauth_error("github_failed")
    raw, _ = await issue_refresh(session, user.id, now)
    await session.commit()
    redirect = RedirectResponse(f"{get_settings().public_url}/auth/callback", status_code=302)
    set_refresh_cookie(redirect, raw)
    redirect.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/github")
    return redirect


# Используется в тестах и ротации: единая точка отзыва семейства.
__all__ = ["revoke_family", "router"]
