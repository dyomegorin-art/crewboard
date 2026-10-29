import logging
from typing import Any

import httpx
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession

import app.billing.handlers  # noqa: F401  (регистрирует обработчики outbox)
from app.billing.providers import make_provider
from app.billing.service import scan_dunning, scan_renewals
from app.clock import default_clock
from app.config import get_settings
from app.db import SessionLocal, engine
from app.logging import setup_logging
from app.mailer import send_email
from app.outbox import Deps, process_pending
from app.services.demo import cleanup_demo_data

log = logging.getLogger("app.worker")


async def post_webhook(url: str, body: bytes, headers: dict[str, str]) -> int:
    async with httpx.AsyncClient(timeout=10) as http:
        return (await http.post(url, content=body, headers=headers)).status_code


async def build_deps() -> Deps:
    now = await default_clock().now()
    return Deps(
        now=now,
        send_email=send_email,
        post_webhook=post_webhook,
        make_provider=lambda session: make_provider(session, now),
    )


async def process_outbox(ctx: dict[str, Any]) -> int:
    """Берёт готовые команды из outbox (письма, вебхуки mock-провайдера, списания, продления)."""
    return await process_pending(SessionLocal, await build_deps())


async def billing_tick(ctx: dict[str, Any]) -> dict[str, int]:
    """Раз в минуту: продления, напоминания о trial, закрытие периодов и dunning."""
    now = await default_clock().now()
    async with SessionLocal() as session:
        stats = {**await scan_renewals(session, now), **await scan_dunning(session, now)}
        await session.commit()
    if any(stats.values()):
        log.info("billing tick", extra=stats)
    return stats


async def cleanup_demo(ctx: dict[str, Any]) -> int:
    """Демо-данные живут сутки."""
    now = await default_clock().now()
    async with SessionLocal() as session:
        removed = await cleanup_demo_data(session, now)
        await session.commit()
    if removed:
        log.info("demo cleanup", extra={"removed": removed})
    return removed


async def startup(ctx: dict[str, Any]) -> None:
    setup_logging(get_settings().log_level)
    get_settings().validate_startup()


async def shutdown(ctx: dict[str, Any]) -> None:
    await engine.dispose()


_ = AsyncSession


class WorkerSettings:
    functions = [process_outbox, billing_tick, cleanup_demo]
    cron_jobs = [
        cron(process_outbox, second=set(range(0, 60, 3)), run_at_startup=True, unique=False),
        cron(billing_tick, second={0}, run_at_startup=True),
        cron(cleanup_demo, minute={7}, second={0}),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
