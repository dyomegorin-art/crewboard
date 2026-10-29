import asyncio
import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.providers.mock import SIGNATURE_HEADER, canonical_json, sign
from app.config import get_settings
from app.models import (
    InvoiceStatus,
    OutboxCommand,
    RawWebhookDelivery,
    SystemIssue,
    WebhookEvent,
)

from .helpers import CARD_OK, Stack, W, invoices_of, sub_of

URL = "/api/billing/webhooks/mock"


def signed(event: dict[str, Any], secret: str | None = None) -> tuple[bytes, dict[str, str]]:
    body = canonical_json(event)
    return body, {SIGNATURE_HEADER: sign(body, secret or get_settings().webhook_secret)}


async def open_invoice(client: AsyncClient, factory: Any) -> tuple[Any, Any, dict[str, Any]]:
    ws, _, oh = await factory.workspace()
    r = await client.post(W(ws, "/billing/checkout"), json={"period": "month"}, headers=oh)
    (invoice,) = await invoices_of(ws)
    event = {
        "id": "evt_test_1",
        "type": "payment.succeeded",
        "payment_id": invoice.provider_payment_id,
        "invoice_id": invoice.id,
        "method": {"token": "mock_pm_ok_x", "brand": "Visa", "last4": "4242"},
    }
    assert r.status_code == 200
    return ws, oh, event


async def test_valid_webhook_updates_state_and_stores_raw_body(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, _, event = await open_invoice(client, factory)
    body, headers = signed(event)
    r = await client.post(URL, content=body, headers=headers)
    assert r.status_code == 200 and r.json()["result"] == "processed"
    assert (await sub_of(ws)).plan == "pro"
    delivery = await session.scalar(select(RawWebhookDelivery))
    assert delivery is not None and bytes(delivery.raw_body) == body  # сырое тело байтами
    assert delivery.parse_status == "OK" and delivery.event_id is not None
    ev = await session.scalar(select(WebhookEvent))
    assert ev is not None and ev.processed_at is not None and ev.type == "payment.succeeded"


async def test_bad_or_missing_signature_is_401_and_nothing_is_stored(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, _, event = await open_invoice(client, factory)
    body, _ = signed(event)
    for headers in (
        {},
        {SIGNATURE_HEADER: "deadbeef"},
        signed(event, "another-secret-value-0000000000")[1],
    ):
        r = await client.post(URL, content=body, headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_signature"
    assert (await session.scalars(select(RawWebhookDelivery))).all() == []
    assert (await sub_of(ws)).plan == "free"


async def test_unknown_provider_is_404(client: AsyncClient) -> None:
    assert (await client.post("/api/billing/webhooks/stripe", content=b"{}")).status_code == 404


async def test_duplicate_delivery_is_idempotent(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, _, event = await open_invoice(client, factory)
    body, headers = signed(event)
    first = await client.post(URL, content=body, headers=headers)
    # Тот же id, но другой порядок ключей и пробелы: содержимое то же → дубль, не конфликт.
    reordered = json.dumps(dict(reversed(list(event.items()))), indent=2).encode()
    second = await client.post(
        URL,
        content=reordered,
        headers={SIGNATURE_HEADER: sign(reordered, get_settings().webhook_secret)},
    )
    assert first.json()["result"] == "processed" and second.status_code == 200
    assert second.json() == {"result": "duplicate"}
    assert len((await session.scalars(select(WebhookEvent))).all()) == 1
    deliveries = (
        await session.scalars(select(RawWebhookDelivery).order_by(RawWebhookDelivery.id))
    ).all()
    assert [d.parse_status for d in deliveries] == ["OK", "DUPLICATE"]  # обе доставки сохранены
    assert [i.status for i in await invoices_of(ws)] == [InvoiceStatus.paid]
    assert (await sub_of(ws)).current_period_start == (await invoices_of(ws))[0].period_start


async def test_same_event_id_with_different_content_is_409_conflict(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, _, event = await open_invoice(client, factory)
    await client.post(URL, content=signed(event)[0], headers=signed(event)[1])
    tampered = {**event, "type": "payment.failed"}
    body, headers = signed(tampered)
    r = await client.post(URL, content=body, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"
    issue = await session.scalar(
        select(SystemIssue).where(SystemIssue.kind == "WEBHOOK_IDEMPOTENCY_CONFLICT")
    )
    assert issue is not None
    conflict = await session.scalar(
        select(RawWebhookDelivery).where(RawWebhookDelivery.parse_status == "CONFLICT")
    )
    assert conflict is not None  # видимая запись о конфликте
    assert (await sub_of(ws)).plan == "pro"  # состояние осталось от первой, подлинной доставки
    assert [i.status for i in await invoices_of(ws)] == [InvoiceStatus.paid]


async def test_parallel_delivery_of_the_same_event_applies_it_once(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, _, event = await open_invoice(client, factory)
    body, headers = signed(event)
    results = await asyncio.gather(
        *[client.post(URL, content=body, headers=headers) for _ in range(6)]
    )
    codes = sorted(
        r.json().get("result", r.json().get("error")) and r.json().get("result") for r in results
    )
    assert codes.count("processed") == 1 and codes.count("duplicate") == 5
    assert len((await session.scalars(select(WebhookEvent))).all()) == 1
    assert len((await session.scalars(select(RawWebhookDelivery))).all()) == 6
    from app.models import PaymentMethod

    assert (
        len((await session.scalars(select(PaymentMethod))).all()) == 1
    )  # карта сохранена один раз
    assert (await sub_of(ws)).plan == "pro"


async def test_invalid_payload_is_stored_and_rejected(
    client: AsyncClient, session: AsyncSession
) -> None:
    body = b"not json at all"
    r = await client.post(
        URL, content=body, headers={SIGNATURE_HEADER: sign(body, get_settings().webhook_secret)}
    )
    assert r.status_code == 400
    delivery = await session.scalar(select(RawWebhookDelivery))
    assert (
        delivery is not None
        and bytes(delivery.raw_body) == body
        and delivery.parse_status == "INVALID"
    )


async def test_event_for_unknown_invoice_is_recorded_not_applied(
    client: AsyncClient, session: AsyncSession
) -> None:
    body, headers = signed(
        {"id": "evt_x", "type": "payment.succeeded", "payment_id": "nope", "invoice_id": 99999}
    )
    r = await client.post(URL, content=body, headers=headers)
    assert r.status_code == 200 and r.json()["outcome"] == "unknown_invoice"
    issue = await session.scalar(
        select(SystemIssue).where(SystemIssue.kind == "WEBHOOK_UNKNOWN_INVOICE")
    )
    assert issue is not None


async def test_unrelated_event_type_is_acknowledged(
    client: AsyncClient, session: AsyncSession
) -> None:
    body, headers = signed({"id": "evt_y", "type": "refund.created"})
    r = await client.post(URL, content=body, headers=headers)
    assert r.status_code == 200 and r.json()["outcome"] == "ignored_type"


async def test_late_failure_after_payment_does_not_break_paid_invoice(
    client: AsyncClient, factory: Any
) -> None:
    ws, _, event = await open_invoice(client, factory)
    await client.post(URL, content=signed(event)[0], headers=signed(event)[1])
    late = {**event, "id": "evt_late", "type": "payment.failed"}
    r = await client.post(URL, content=signed(late)[0], headers=signed(late)[1])
    assert r.json()["outcome"] == "ignored_stale_failure"
    assert [i.status for i in await invoices_of(ws)] == [InvoiceStatus.paid] and (
        await sub_of(ws)
    ).plan == "pro"


async def test_processing_failure_marks_delivery_failed_and_returns_500(
    client: AsyncClient, factory: Any, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, _, event = await open_invoice(client, factory)
    from app.billing import webhooks

    async def boom(*a: Any, **k: Any) -> tuple[str, int | None]:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(webhooks, "apply_provider_event", boom)
    body, headers = signed(event)
    r = await client.post(URL, content=body, headers=headers)
    assert r.status_code == 500
    delivery = await session.scalar(select(RawWebhookDelivery))
    assert (
        delivery is not None
        and delivery.parse_status == "FAILED"
        and bytes(delivery.raw_body) == body
    )
    assert await session.scalar(select(WebhookEvent)) is None  # событие откатилось: повтор пройдёт
    issue = await session.scalar(
        select(SystemIssue).where(SystemIssue.kind == "WEBHOOK_PROCESSING_FAILED")
    )
    assert issue is not None and "RuntimeError" in issue.detail and "exploded" not in issue.detail
    monkeypatch.undo()
    assert (await client.post(URL, content=body, headers=headers)).json()["result"] == "processed"


async def test_invalid_state_transition_is_rejected_and_recorded(
    client: AsyncClient, factory: Any, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.billing import webhooks
    from app.billing.state import Event, InvalidTransition, transition
    from app.models import Subscription, SubStatus

    ws, _, event = await open_invoice(client, factory)

    async def illegal(s: AsyncSession, ev: Any, now: Any, provider: str) -> str:
        sub = await s.get(Subscription, ws.id)
        assert sub is not None
        await transition(s, sub, Event.DUNNING_EXHAUSTED, now)  # active → read_only запрещён
        return "unreachable"

    monkeypatch.setattr(webhooks, "apply_provider_event", illegal)
    body, headers = signed(event)
    r = await client.post(URL, content=body, headers=headers)
    assert r.status_code == 200 and r.json()["result"] == "rejected_invalid_transition"
    kinds = {i.kind for i in (await session.scalars(select(SystemIssue))).all()}
    assert "INVALID_SUBSCRIPTION_TRANSITION" in kinds
    assert (await sub_of(ws)).status == SubStatus.active
    delivery = await session.scalar(select(RawWebhookDelivery))
    assert delivery is not None and delivery.parse_status == "REJECTED"
    assert await session.scalar(select(WebhookEvent)) is None
    _ = InvalidTransition


async def test_duplicate_flag_sends_each_mock_webhook_twice(
    client: AsyncClient,
    factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    clock: Any,
) -> None:
    monkeypatch.setattr(get_settings(), "mock_webhook_duplicate_rate", 1.0)
    stack = Stack(client, clock)
    ws, _, oh = await factory.workspace()
    r = await client.post(W(ws, "/billing/checkout"), json={"period": "month"}, headers=oh)
    await stack.pay(r.json()["checkout_url"], CARD_OK)
    assert sorted(stack.webhook_status) == [200, 200]
    results = (
        await session.scalars(select(RawWebhookDelivery).order_by(RawWebhookDelivery.id))
    ).all()
    assert [d.parse_status for d in results] == ["OK", "DUPLICATE"]
    cmds = (
        await session.scalars(select(OutboxCommand).where(OutboxCommand.command == "MOCK_WEBHOOK"))
    ).all()
    assert len(cmds) == 2
    assert (await sub_of(ws)).plan == "pro"
