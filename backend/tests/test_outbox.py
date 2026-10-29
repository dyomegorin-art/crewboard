import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import app.billing.handlers  # noqa: F401
from app import outbox
from app.db import SessionLocal
from app.models import ActivityLog, OutboxCommand, SystemIssue
from app.outbox import (
    BACKOFF_CAP_SECONDS,
    Deps,
    PermanentCommandError,
    backoff_seconds,
    enqueue,
    process_pending,
)

T0 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def deps(now: datetime) -> Deps:
    async def noop(*a: Any) -> Any:
        return 200

    return Deps(now=now, send_email=noop, post_webhook=noop, make_provider=lambda s: None)


@pytest.fixture(autouse=True)
def _restore_handlers():  # type: ignore[no-untyped-def]
    saved = dict(outbox.HANDLERS)
    yield
    outbox.HANDLERS.clear()
    outbox.HANDLERS.update(saved)


async def add(command: str, payload: dict[str, Any] | None = None, **kw: Any) -> None:
    async with SessionLocal() as s:
        await enqueue(s, command, payload or {}, now=T0, **kw)
        await s.commit()


async def get_cmd() -> OutboxCommand:
    async with SessionLocal() as s:
        cmd = await s.scalar(select(OutboxCommand).order_by(OutboxCommand.id))
        assert cmd is not None
        return cmd


def test_backoff_sequence_and_cap() -> None:
    assert [backoff_seconds(n) for n in range(1, 6)] == [30, 60, 120, 240, 480]
    assert backoff_seconds(6) == BACKOFF_CAP_SECONDS == 900
    assert backoff_seconds(30) == 900


async def test_successful_command_is_done() -> None:
    seen: list[int] = []

    @outbox.handler("T_OK")
    async def ok(session, cmd, d):  # type: ignore[no-untyped-def]
        seen.append(cmd.id)
        return {}

    await add("T_OK")
    assert await process_pending(SessionLocal, deps(T0)) == 1
    cmd = await get_cmd()
    assert (cmd.status, cmd.attempts, seen) == ("DONE", 1, [cmd.id])
    assert await process_pending(SessionLocal, deps(T0)) == 0  # повторно не берётся


async def test_transient_failures_retry_with_exponential_backoff_then_fail() -> None:
    @outbox.handler("T_FLAKY")
    async def flaky(session, cmd, d):  # type: ignore[no-untyped-def]
        raise ConnectionError("smtp down")

    await add("T_FLAKY", {"workspace_id": None})
    now = T0
    delays = []
    for attempt in range(1, 5):
        assert await process_pending(SessionLocal, deps(now)) == 1
        cmd = await get_cmd()
        assert (cmd.status, cmd.attempts, cmd.error) == ("PENDING", attempt, "ConnectionError")
        delays.append(int((cmd.next_attempt_at - now).total_seconds()))
        # До срока команда не берётся.
        assert (
            await process_pending(SessionLocal, deps(now + timedelta(seconds=delays[-1] - 1))) == 0
        )
        now = cmd.next_attempt_at
    assert delays == [30, 60, 120, 240]

    assert await process_pending(SessionLocal, deps(now)) == 1  # 5-я попытка — последняя
    cmd = await get_cmd()
    assert (cmd.status, cmd.attempts) == ("FAILED", 5)
    async with SessionLocal() as s:
        issue = await s.scalar(select(SystemIssue))
    assert (
        issue is not None and issue.kind == "OUTBOX_COMMAND_FAILED" and issue.command_id == cmd.id
    )
    assert await process_pending(SessionLocal, deps(now + timedelta(days=1))) == 0


async def test_failure_after_attempts_writes_audit_event_for_workspace(factory: Any) -> None:
    ws, _, _ = await factory.workspace()

    @outbox.handler("T_BAD")
    async def bad(session, cmd, d):  # type: ignore[no-untyped-def]
        raise PermanentCommandError("nope")

    await add("T_BAD", {"workspace_id": ws.id})
    await process_pending(SessionLocal, deps(T0))
    async with SessionLocal() as s:
        event = await s.scalar(
            select(ActivityLog).where(ActivityLog.action == "outbox.command_failed")
        )
    assert event is not None and event.workspace_id == ws.id and event.detail["error"] == "nope"


async def test_permanent_error_is_not_retried() -> None:
    calls = 0

    @outbox.handler("T_PERM")
    async def perm(session, cmd, d):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise PermanentCommandError("malformed")

    await add("T_PERM")
    await process_pending(SessionLocal, deps(T0))
    await process_pending(SessionLocal, deps(T0 + timedelta(days=1)))
    cmd = await get_cmd()
    assert (cmd.status, cmd.attempts, cmd.error, calls) == ("FAILED", 1, "malformed", 1)


async def test_unknown_command_fails_permanently() -> None:
    await add("T_UNKNOWN")
    await process_pending(SessionLocal, deps(T0))
    cmd = await get_cmd()
    assert cmd.status == "FAILED" and "unknown command" in (cmd.error or "")


async def test_handler_side_effects_roll_back_on_failure() -> None:
    @outbox.handler("T_ROLLBACK")
    async def rb(session, cmd, d):  # type: ignore[no-untyped-def]
        session.add(SystemIssue(kind="SIDE_EFFECT", detail="x"))
        await session.flush()
        raise RuntimeError("after write")

    await add("T_ROLLBACK")
    await process_pending(SessionLocal, deps(T0))
    async with SessionLocal() as s:
        assert await s.scalar(select(SystemIssue).where(SystemIssue.kind == "SIDE_EFFECT")) is None


async def test_dedupe_key_prevents_second_command() -> None:
    async with SessionLocal() as s:
        a = await enqueue(s, "X", {}, now=T0, dedupe_key="k1")
        b = await enqueue(s, "X", {}, now=T0, dedupe_key="k1")
        c = await enqueue(s, "X", {}, now=T0, dedupe_key="k2")
        d = await enqueue(s, "X", {}, now=T0)
        e = await enqueue(s, "X", {}, now=T0)
        await s.commit()
    assert (a, b, c, d, e) == (True, False, True, True, True)


async def test_delay_postpones_command() -> None:
    @outbox.handler("T_DELAY")
    async def ok(session, cmd, d):  # type: ignore[no-untyped-def]
        return {}

    async with SessionLocal() as s:
        await enqueue(s, "T_DELAY", {}, now=T0, delay_seconds=60)
        await s.commit()
    assert await process_pending(SessionLocal, deps(T0)) == 0
    assert await process_pending(SessionLocal, deps(T0 + timedelta(seconds=60))) == 1


async def test_two_workers_never_process_the_same_command_skip_locked() -> None:
    processed: list[int] = []

    @outbox.handler("T_SLOW")
    async def slow(session, cmd, d):  # type: ignore[no-untyped-def]
        processed.append(cmd.id)
        await asyncio.sleep(0.15)  # держим блокировку, пока второй воркер ищет работу
        return {}

    for _ in range(6):
        await add("T_SLOW")
    a, b = await asyncio.gather(
        process_pending(SessionLocal, deps(T0)), process_pending(SessionLocal, deps(T0))
    )
    assert a + b == 6
    assert sorted(processed) == sorted(set(processed)) and len(processed) == 6  # без дублей
    async with SessionLocal() as s:
        statuses = {c.status for c in (await s.scalars(select(OutboxCommand))).all()}
        attempts = {c.attempts for c in (await s.scalars(select(OutboxCommand))).all()}
    assert statuses == {"DONE"} and attempts == {1}
    assert a > 0 and b > 0  # работа действительно разошлась по воркерам


async def test_email_handler_sends_and_rejects_malformed() -> None:
    sent: list[tuple[str, str, str]] = []

    async def send(to: str, subject: str, body: str) -> None:
        sent.append((to, subject, body))

    d = deps(T0)
    d.send_email = send
    await add("SEND_EMAIL", {"to": "a@example.com", "subject": "S", "body": "B"})
    await add("SEND_EMAIL", {"to": "a@example.com"})
    await process_pending(SessionLocal, d)
    assert sent == [("a@example.com", "S", "B")]
    async with SessionLocal() as s:
        cmds = (await s.scalars(select(OutboxCommand).order_by(OutboxCommand.id))).all()
    assert [c.status for c in cmds] == ["DONE", "FAILED"]


async def test_mock_webhook_handler_retries_on_server_error_and_stops_on_conflict() -> None:
    codes = iter([500, 409])
    calls = []

    async def post(url: str, body: bytes, headers: dict[str, str]) -> int:
        calls.append(url)
        return next(codes)

    d = deps(T0)
    d.post_webhook = post
    await add("MOCK_WEBHOOK", {"event": {"id": "e1", "type": "payment.succeeded"}})
    await process_pending(SessionLocal, d)
    cmd = await get_cmd()
    assert cmd.status == "PENDING" and cmd.attempts == 1  # 5xx → ретрай
    d.now = cmd.next_attempt_at
    await process_pending(SessionLocal, d)
    cmd = await get_cmd()
    assert cmd.status == "FAILED" and cmd.attempts == 2  # 409 → без ретраев
    assert calls[0].endswith("/api/billing/webhooks/mock")
    await add("MOCK_WEBHOOK", {"event": "garbage"})
    await process_pending(SessionLocal, d)
