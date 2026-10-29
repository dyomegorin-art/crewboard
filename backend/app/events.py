"""Real-time: изменения публикуются в Redis pub/sub ПОСЛЕ коммита, SSE-эндпоинт читает канал."""

import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


def channel(workspace_id: int) -> str:
    return f"ws:{workspace_id}"


async def publish(redis: Redis, workspace_id: int, event_type: str, **data: Any) -> None:
    """Best effort: недоставленное событие не должно ронять запрос (данные уже в БД)."""
    try:
        await redis.publish(channel(workspace_id), json.dumps({"type": event_type, **data}))
    except RedisError:
        log.warning("event publish failed", extra={"type": event_type})
