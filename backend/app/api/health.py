from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.redis import get_redis

router = APIRouter(tags=["health"])


@router.get("/health", summary="Проверка связи с PostgreSQL и Redis")
async def health(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "fail"
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "fail"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "degraded", **checks}, status_code=200 if ok else 503
    )
