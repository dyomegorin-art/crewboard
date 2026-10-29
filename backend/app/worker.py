from typing import Any

from arq.connections import RedisSettings

from app.config import get_settings
from app.db import engine
from app.logging import setup_logging


async def startup(ctx: dict[str, Any]) -> None:
    setup_logging(get_settings().log_level)


async def shutdown(ctx: dict[str, Any]) -> None:
    await engine.dispose()


async def noop(ctx: dict[str, Any]) -> None:
    return None


class WorkerSettings:
    functions = [noop]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
