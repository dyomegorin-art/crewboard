from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityLog


async def audit(
    session: AsyncSession,
    workspace_id: int,
    actor_id: int | None,
    entity: str,
    entity_id: int | None,
    action: str,
    now: datetime | None = None,
    **detail: Any,
) -> ActivityLog:
    """Запись в лог активности воркспейса (основа ленты, истории задачи и аудита биллинга)."""
    row = ActivityLog(
        workspace_id=workspace_id,
        actor_id=actor_id,
        entity=entity,
        entity_id=entity_id,
        action=action,
        detail=detail,
    )
    if now is not None:
        row.created_at = now
    session.add(row)
    await session.flush()
    return row
