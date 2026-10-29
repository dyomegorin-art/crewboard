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
