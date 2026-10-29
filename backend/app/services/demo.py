import secrets
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services.workspaces import create_workspace


async def create_demo_workspace(session: AsyncSession, now: datetime) -> User:
    user = User(
        email=f"demo-{secrets.token_hex(6)}@demo.crewboard.local",
        name="Demo User",
        is_demo=True,
        email_verified_at=now,
    )
    session.add(user)
    await session.flush()
    await create_workspace(session, user, "Demo workspace", now)
    return user


async def cleanup_demo_data(session: AsyncSession, now: datetime) -> int:
    """Удаляет демо-воркспейсы и демо-пользователей старше TTL. Реальные данные не трогает."""
    from datetime import timedelta

    from sqlalchemy import delete, select

    from app.config import get_settings
    from app.models import Workspace

    cutoff = now - timedelta(hours=get_settings().demo_ttl_hours)
    old_users = (
        await session.scalars(
            select(User.id).where(User.is_demo.is_(True), User.created_at < cutoff)
        )
    ).all()
    if not old_users:
        return 0
    # Воркспейсы демо-владельцев уходят каскадом вместе с содержимым.
    await session.execute(delete(Workspace).where(Workspace.owner_id.in_(old_users)))
    await session.execute(delete(User).where(User.id.in_(old_users)))
    return len(old_users)
