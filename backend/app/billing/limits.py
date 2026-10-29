"""Проверки лимитов тарифа. Тарифные числа — в plans.py; здесь только применение."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.plans import PlanLimits, limits_for
from app.errors import AppError
from app.models import (
    PAID_ROLES,
    Invitation,
    Membership,
    Plan,
    Project,
    Role,
    Subscription,
    SubStatus,
)


async def get_subscription(
    session: AsyncSession, workspace_id: int, *, lock: bool = False
) -> Subscription:
    stmt = select(Subscription).where(Subscription.workspace_id == workspace_id)
    if lock:
        stmt = stmt.with_for_update()
    sub = await session.scalar(stmt)
    if sub is None:  # у каждого воркспейса подписка создаётся вместе с ним
        raise AppError(500, "subscription_missing", "Subscription not found")
    return sub


def plan_limits(sub: Subscription) -> PlanLimits:
    return limits_for(sub.plan)


def plan_limit_error(code: str, message: str) -> AppError:
    return AppError(402, code, message)


async def count_paid_members(session: AsyncSession, workspace_id: int) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.workspace_id == workspace_id, Membership.role.in_(PAID_ROLES))
        )
        or 0
    )


def active_invites_query(workspace_id: int, now: datetime, *, paid_roles_only: bool = True):  # type: ignore[no-untyped-def]
    stmt = select(Invitation).where(
        Invitation.workspace_id == workspace_id,
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > now,
    )
    if paid_roles_only:
        stmt = stmt.where(Invitation.role.in_(PAID_ROLES))
    return stmt


async def count_active_invites(session: AsyncSession, workspace_id: int, now: datetime) -> int:
    sub = active_invites_query(workspace_id, now).subquery()
    return await session.scalar(select(func.count()).select_from(sub)) or 0


async def check_can_add_member(
    session: AsyncSession, sub: Subscription, role: Role, now: datetime
) -> None:
    """Лимиты тарифа на добавление участника (или приглашения). Pro без лимита."""
    limits = plan_limits(sub)
    if role == Role.guest:
        if not limits.guests:
            raise plan_limit_error("plan_limit_guests", "Guests are available on the Pro plan")
        return
    if limits.max_members is None:
        return
    taken = await count_paid_members(session, sub.workspace_id) + await count_active_invites(
        session, sub.workspace_id, now
    )
    if taken >= limits.max_members:
        raise plan_limit_error(
            "plan_limit_members",
            f"The Free plan allows up to {limits.max_members} members. Upgrade to Pro",
        )


async def check_can_create_project(session: AsyncSession, sub: Subscription) -> None:
    limit = plan_limits(sub).max_projects
    if limit is None:
        return
    active = (
        await session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.workspace_id == sub.workspace_id, Project.archived_at.is_(None))
        )
        or 0
    )
    if active >= limit:
        raise plan_limit_error(
            "plan_limit_projects", f"The Free plan allows up to {limit} active projects"
        )


def require_custom_statuses(sub: Subscription) -> None:
    if not plan_limits(sub).custom_statuses:
        raise plan_limit_error(
            "plan_limit_statuses", "Custom columns are available on the Pro plan"
        )


def require_csv_export(sub: Subscription) -> None:
    if not plan_limits(sub).csv_export:
        raise plan_limit_error("plan_limit_export", "CSV export is available on the Pro plan")


def is_pro(sub: Subscription) -> bool:
    return sub.plan == Plan.pro


def is_paying(sub: Subscription) -> bool:
    """Платная подписка, по которой допустимы доплаты за места."""
    return sub.plan == Plan.pro and sub.status in (SubStatus.active,)
