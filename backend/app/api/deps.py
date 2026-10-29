from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock, get_clock
from app.db import get_session
from app.errors import AppError
from app.models import Membership, Role, Subscription, SubStatus, User
from app.security import decode_token
from app.services.audit import audit

_bearer = HTTPBearer(auto_error=False)

# Права по ролям (как PERMISSIONS в repairdesk). Пользователь без права получает 403
# и запись ACCESS_DENIED в лог активности воркспейса.
ALL_PERMISSIONS = {
    "workspace.update",
    "workspace.delete",
    "workspace.transfer",
    "billing.manage",
    "billing.view",
    "members.view",
    "members.invite",
    "members.manage",
    "projects.read",
    "projects.manage",
    "statuses.manage",
    "tasks.read",
    "tasks.write",
    "comments.write",
    "activity.view",
    "export.csv",
}
PERMISSIONS: dict[Role, set[str]] = {
    Role.owner: set(ALL_PERMISSIONS),
    Role.admin: {
        "members.view",
        "members.invite",
        "members.manage",
        "projects.read",
        "projects.manage",
        "statuses.manage",
        "tasks.read",
        "tasks.write",
        "comments.write",
        "activity.view",
        "billing.view",
        "export.csv",
    },
    Role.member: {
        "members.view",
        "projects.read",
        "tasks.read",
        "tasks.write",
        "comments.write",
        "activity.view",
        "export.csv",
    },
    Role.guest: {"projects.read", "tasks.read", "comments.write"},
}


def _unauthorized(message: str = "Not authenticated") -> AppError:
    return AppError(401, "unauthorized", message, {"WWW-Authenticate": "Bearer"})


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> User:
    if creds is None:
        raise _unauthorized()
    try:
        payload = decode_token(creds.credentials, "access", await clock.now())
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        raise _unauthorized("Invalid or expired token") from None
    user = await session.get(User, user_id)
    if user is None:
        raise _unauthorized("User not found")
    return user


async def get_verified_user(user: User = Depends(get_current_user)) -> User:
    if user.email_verified_at is None:
        raise AppError(403, "email_not_verified", "Confirm your email first")
    return user


@dataclass
class WorkspaceCtx:
    user: User
    membership: Membership
    session: AsyncSession
    now_: Clock

    @property
    def workspace_id(self) -> int:
        return self.membership.workspace_id

    @property
    def role(self) -> Role:
        return self.membership.role


async def get_membership(
    workspace_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Membership:
    """Не участник получает 404 (не 403): перебором нельзя узнать, какие воркспейсы существуют."""
    m = await session.get(Membership, (workspace_id, user.id))
    if m is None:
        raise AppError(404, "workspace_not_found", "Workspace not found")
    return m


async def ensure_writable(session: AsyncSession, workspace_id: int) -> None:
    """В read_only (после неуплаты) все изменяющие запросы воркспейса отвечают 402."""
    sub = await session.get(Subscription, workspace_id)
    if sub is not None and sub.status == SubStatus.read_only:
        raise AppError(
            402,
            "workspace_read_only",
            "Workspace is read-only until the subscription is paid",
        )


def require(permission: str, *, write: bool = False) -> Callable[..., Awaitable[WorkspaceCtx]]:
    """Зависимость: участник воркспейса с правом `permission`.

    `write=True` — изменяющая операция: дополнительно проверяется `require_writable`.
    Порядок: не участник → 404, нет права → 403 (+ACCESS_DENIED), read_only → 402.
    """

    async def dependency(
        request: Request,
        membership: Membership = Depends(get_membership),
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
        clock: Clock = Depends(get_clock),
    ) -> WorkspaceCtx:
        if permission not in PERMISSIONS[membership.role]:
            await audit(
                session, membership.workspace_id, user.id, "permission", None, "access_denied",
                now=await clock.now(), permission=permission, path=request.url.path,
                method=request.method, role=membership.role.value,
            )  # fmt: skip
            await session.commit()
            raise AppError(403, "forbidden", "Insufficient permissions")
        if write:
            await ensure_writable(session, membership.workspace_id)
        return WorkspaceCtx(user=user, membership=membership, session=session, now_=clock)

    return dependency


async def require_writable(
    workspace_id: int,
    _: Membership = Depends(get_membership),
    session: AsyncSession = Depends(get_session),
) -> None:
    await ensure_writable(session, workspace_id)


def require_csrf_header(request: Request) -> None:
    """refresh-cookie уходит автоматически, поэтому cookie-эндпоинты требуют кастомный заголовок:
    кросс-сайтовая форма не может его поставить без CORS-preflight."""
    if not request.headers.get("X-Requested-With"):
        raise AppError(403, "csrf_header_missing", "X-Requested-With header is required")
