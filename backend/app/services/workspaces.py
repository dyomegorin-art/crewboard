import re
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BillingPeriod,
    Membership,
    Plan,
    Role,
    Subscription,
    SubStatus,
    User,
    Workspace,
)


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return base[:40] or "team"


async def unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name)
    slug = base
    while await session.scalar(select(Workspace.id).where(Workspace.slug == slug)) is not None:
        slug = f"{base}-{secrets.token_hex(2)}"
    return slug


async def create_workspace(
    session: AsyncSession, owner: User, name: str, now: datetime
) -> Workspace:
    """Воркспейс + членство owner + подписка Free. Коммит — на вызывающем."""
    ws = Workspace(name=name, slug=await unique_slug(session, name), owner_id=owner.id)
    session.add(ws)
    await session.flush()
    session.add(Membership(workspace_id=ws.id, user_id=owner.id, role=Role.owner))
    session.add(
        Subscription(
            workspace_id=ws.id,
            plan=Plan.free,
            status=SubStatus.active,
            billing_period=BillingPeriod.month,
            seats=0,
            updated_at=now,
        )
    )
    await session.flush()
    return ws
