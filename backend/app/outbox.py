"""Outbox: команды, которые должны выполниться «хотя бы раз» и пережить падение процесса.

Порт воркера repairdesk на async: SELECT … FOR UPDATE SKIP LOCKED, ретраи с экспоненциальной
паузой, PermanentCommandError без ретраев, после исчерпания попыток — FAILED + SystemIssue.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import OutboxCommand, SystemIssue
from app.services.audit import audit

log = logging.getLogger("app.outbox")

BATCH_LIMIT = 100
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 15 * 60

PENDING = "PENDING"
DONE = "DONE"
FAILED = "FAILED"


class PermanentCommandError(Exception):
    """Детерминированный сбой: повтор не поможет, поэтому команда не ретраится."""


@dataclass
class Deps:
    """Внешние зависимости обработчиков; в тестах подменяются фейками."""

    now: datetime
    send_email: Callable[[str, str, str], Awaitable[None]]
    post_webhook: Callable[[str, bytes, dict[str, str]], Awaitable[int]]
    make_provider: Callable[[AsyncSession], Any]  # → PaymentProvider (привязан к сессии)
    redis: Any = None


Handler = Callable[[AsyncSession, OutboxCommand, Deps], Awaitable[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def handler(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn

    return deco


async def enqueue(
    session: AsyncSession,
    command: str,
    payload: dict[str, Any],
    *,
    now: datetime,
    dedupe_key: str | None = None,
    delay_seconds: int = 0,
) -> bool:
    """Ставит команду в очередь. Повтор с тем же dedupe_key ничего не делает (возвращает False)."""
    stmt = (
        pg_insert(OutboxCommand)
        .values(
            command=command,
            payload=payload,
            dedupe_key=dedupe_key,
            status=PENDING,
            attempts=0,
            next_attempt_at=now + timedelta(seconds=delay_seconds),
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(OutboxCommand.id)
    )
    inserted_id: int | None = (await session.execute(stmt)).scalar_one_or_none()
    return inserted_id is not None


def backoff_seconds(attempt: int) -> int:
    """Пауза после `attempt`-й неудачной попытки: 30, 60, 120, 240 … не больше 15 минут."""
    return min(BACKOFF_BASE_SECONDS * (1 << (attempt - 1)), BACKOFF_CAP_SECONDS)


async def _claim_next(session: AsyncSession, now: datetime) -> OutboxCommand | None:
    """SKIP LOCKED позволяет нескольким воркерам работать рядом: занятую команду пропускаем."""
    return (
        await session.scalars(
            select(OutboxCommand)
            .where(OutboxCommand.status == PENDING, OutboxCommand.next_attempt_at <= now)
            .order_by(OutboxCommand.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).first()


async def _record_failure(
    session: AsyncSession, cmd: OutboxCommand, exc: Exception, now: datetime
) -> None:
    permanent = isinstance(exc, PermanentCommandError)
    # Свои сообщения безопасно хранить; для остального — только имя класса: тексты ошибок БД
    # могут содержать пользовательские значения.
    error = str(exc) if permanent else type(exc).__name__
    cmd.error = error
    workspace_id = cmd.payload.get("workspace_id")

    if not permanent and cmd.attempts < MAX_ATTEMPTS:
        delay = backoff_seconds(cmd.attempts)
        cmd.next_attempt_at = now + timedelta(seconds=delay)
        log.warning("outbox retry", extra={"command": cmd.command, "in": delay, "error": error})
        return

    cmd.status = FAILED
    session.add(
        SystemIssue(
            kind="OUTBOX_COMMAND_FAILED",
            command_id=cmd.id,
            detail=f"{cmd.command} failed after {cmd.attempts} attempt(s): {error}",
        )
    )
    if isinstance(workspace_id, int):
        await audit(
            session, workspace_id, None, "outbox", cmd.id, "outbox.command_failed", now=now,
            command=cmd.command, attempts=cmd.attempts, error=error,
        )  # fmt: skip
    log.error("outbox command failed", extra={"command": cmd.command, "error": error})


async def process_pending(
    factory: async_sessionmaker[AsyncSession], deps: Deps, limit: int = BATCH_LIMIT
) -> int:
    """Обрабатывает до `limit` готовых команд: claim → выполнить → commit (снимает блокировку).

    Временный сбой переносит команду с backoff до MAX_ATTEMPTS; постоянный или последняя
    неудачная попытка делают её FAILED с видимым SystemIssue. `deps.now` инжектируется,
    поэтому расписание проверяется без sleep.
    """
    processed = 0
    while processed < limit:
        async with factory() as session:
            cmd = await _claim_next(session, deps.now)
            if cmd is None:
                break
            cmd.attempts += 1
            try:
                fn = HANDLERS.get(cmd.command)
                if fn is None:
                    raise PermanentCommandError(f"unknown command {cmd.command}")
                async with session.begin_nested():
                    await fn(session, cmd, deps)
                cmd.status = DONE
                cmd.error = None
            except Exception as exc:  # noqa: BLE001 - каждый сбой ретраится или становится виден
                await _record_failure(session, cmd, exc, deps.now)
            await session.commit()
        processed += 1
    return processed
