"""Dev-эндпоинты стенда. Подключаются только при DEV_ENDPOINTS=true (и ENV=local/test)."""

from datetime import timedelta

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.clock import RedisOffsetClock
from app.redis import get_redis
from app.schemas.billing import AdvanceClockIn

router = APIRouter(prefix="/dev", tags=["dev"])


@router.post("/advance-clock", summary="Сдвинуть часы приложения вперёд (api и воркер)")
async def advance_clock(
    body: AdvanceClockIn, redis: Redis = Depends(get_redis)
) -> dict[str, object]:
    delta = timedelta(days=body.days, hours=body.hours, seconds=body.seconds)
    clock = RedisOffsetClock(redis)
    offset = await clock.advance(int(delta.total_seconds()))
    return {"offset_seconds": offset, "now": (await clock.now()).isoformat()}


@router.post("/reset-clock", summary="Вернуть часы в реальное время")
async def reset_clock(redis: Redis = Depends(get_redis)) -> dict[str, object]:
    clock = RedisOffsetClock(redis)
    await clock.reset()
    return {"offset_seconds": 0, "now": (await clock.now()).isoformat()}


@router.get("/clock", summary="Текущее время приложения")
async def read_clock(redis: Redis = Depends(get_redis)) -> dict[str, object]:
    return {"now": (await RedisOffsetClock(redis).now()).isoformat()}
