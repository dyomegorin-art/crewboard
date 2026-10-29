"""Общие помощники тестов биллинга: полный путь «действие → outbox → вебхук → состояние»."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select

import app.billing.handlers  # noqa: F401  (регистрирует обработчики outbox)
from app.billing.providers import make_provider
from app.billing.service import scan_dunning, scan_renewals
from app.clock import FakeClock
from app.db import SessionLocal
from app.models import Invoice, Subscription, SystemIssue
from app.outbox import Deps, process_pending

CARD_OK = "4242 4242 4242 4242"
CARD_DECLINED = "4000 0000 0000 0002"
CARD_LATER = "4000 0000 0000 9995"


def W(ws: Any, path: str = "") -> str:
    return f"/api/w/{ws.id}{path}"


class Stack:
    """Гоняет outbox в процессе теста: письма пишутся в список, вебхуки идут в ASGI-приложение."""

    def __init__(self, client: AsyncClient, clock: FakeClock) -> None:
        self.client = client
        self.clock = clock
        self.emails: list[tuple[str, str, str]] = []
        self.webhook_status: list[int] = []

    def deps(self, now: datetime | None = None) -> Deps:
        at = now or self.clock.current

        async def send_email(to: str, subject: str, body: str) -> None:
            self.emails.append((to, subject, body))

        async def post_webhook(url: str, body: bytes, headers: dict[str, str]) -> int:
            r = await self.client.post(url, content=body, headers=headers)
            self.webhook_status.append(r.status_code)
            return r.status_code

        return Deps(
            now=at,
            send_email=send_email,
            post_webhook=post_webhook,
            make_provider=lambda s: make_provider(s, at),
        )

    async def outbox(self, rounds: int = 10) -> int:
        """Выполняет команды, пока очередь не опустеет (вебхук может породить новые)."""
        total = 0
        for _ in range(rounds):
            n = await process_pending(SessionLocal, self.deps())
            total += n
            if n == 0:
                break
        return total

    async def tick(self) -> dict[str, int]:
        """Один проход cron-задачи биллинга + выполнение породивших команд."""
        async with SessionLocal() as s:
            stats = {
                **await scan_renewals(s, self.clock.current),
                **await scan_dunning(s, self.clock.current),
            }
            await s.commit()
        await self.outbox()
        return stats

    async def pay(self, checkout_url: str, card: str = CARD_OK) -> dict[str, Any]:
        session_id = checkout_url.rsplit("/", 1)[1]
        r = await self.client.post(
            f"/api/mock/checkout/{session_id}/pay", json={"card_number": card}
        )
        assert r.status_code == 200, r.text
        await self.outbox()
        return r.json()  # type: ignore[no-any-return]


async def sub_of(ws: Any) -> Subscription:
    async with SessionLocal() as s:
        sub = await s.get(Subscription, ws.id)
        assert sub is not None
        return sub


async def invoices_of(ws: Any) -> list[Invoice]:
    async with SessionLocal() as s:
        return list(
            (
                await s.scalars(
                    select(Invoice).where(Invoice.workspace_id == ws.id).order_by(Invoice.id)
                )
            ).all()
        )


async def issues() -> list[SystemIssue]:
    async with SessionLocal() as s:
        return list((await s.scalars(select(SystemIssue).order_by(SystemIssue.id))).all())


def count_emails(stack: Stack, predicate: Callable[[str], bool]) -> int:
    return sum(1 for _, subject, _ in stack.emails if predicate(subject))
