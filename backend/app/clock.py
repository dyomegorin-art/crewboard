"""Часы приложения. Сервисы получают `now` параметром, а не зовут datetime.now() сами,
поэтому тесты двигают время без sleep, а dev-эндпоинт сдвигает его для всего стенда."""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

OFFSET_KEY = "dev:clock_offset_seconds"


class Clock(Protocol):
    async def now(self) -> datetime: ...


class SystemClock:
    async def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Для тестов: время стоит на месте, пока его не сдвинули."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    async def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


class RedisOffsetClock:
    """Реальное время + сдвиг из Redis (общий для api и воркера). Только для ENV=local."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def now(self) -> datetime:
        offset = 0
        try:
            raw = await self._redis.get(OFFSET_KEY)
            offset = int(raw) if raw else 0
        except (RedisError, ValueError):
            pass
        return datetime.now(UTC) + timedelta(seconds=offset)

    async def advance(self, seconds: int) -> int:
        return int(await self._redis.incrby(OFFSET_KEY, seconds))

    async def reset(self) -> None:
        await self._redis.delete(OFFSET_KEY)


_default: Clock | None = None


def default_clock() -> Clock:
    """Часы процесса: в local/test с dev-эндпоинтами — со сдвигом из Redis, иначе системные."""
    global _default
    if _default is None:
        from app.config import get_settings
        from app.redis import redis_client

        _default = RedisOffsetClock(redis_client) if get_settings().dev_endpoints else SystemClock()
    return _default


async def get_clock() -> Clock:
    return default_clock()
