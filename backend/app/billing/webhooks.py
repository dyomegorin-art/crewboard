"""Приём вебхуков платёжного провайдера (паттерн intake из repairdesk, порт на async).

1. Подлинность — до чего-либо: неподлинный вебхук → 401 и ничего не сохраняется.
2. Сырое тело + заголовки коммитятся ДО разбора — потерять доставку нельзя.
3. UNIQUE(provider, provider_event_id): повтор → 200 duplicate; тот же id с другим
   содержимым (fingerprint по каноническому JSON) → 409 + SystemIssue.
4. Изменение подписки и счёта — в той же транзакции, что и вставка события; гонку двух
   параллельных доставок решает ограничение БД.
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.providers.base import (
    PaymentProvider,
    ProviderEvent,
    WebhookAuthError,
    WebhookPayloadError,
)
from app.billing.service import apply_provider_event
from app.billing.state import InvalidTransition
from app.models import RawWebhookDelivery, SystemIssue, WebhookEvent

log = logging.getLogger("app.billing.webhooks")


@dataclass
class IntakeResult:
    http_status: int
    body: dict[str, Any]


def fingerprint(payload: dict[str, Any]) -> str:
    """Хэш канонического JSON: порядок ключей и пробелы не важны."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def receive(
    session: AsyncSession,
    provider: PaymentProvider,
    raw_body: bytes,
    headers: Mapping[str, str],
    now: datetime,
) -> IntakeResult:
    try:
        await provider.verify_webhook(raw_body, headers)
    except WebhookAuthError:
        return IntakeResult(
            401, {"error": {"code": "invalid_signature", "message": "Unauthorized"}}
        )

    delivery = RawWebhookDelivery(
        provider=provider.name,
        raw_body=raw_body,
        headers={
            k.lower(): v
            for k, v in headers.items()
            if k.lower().startswith("x-") or k.lower() == "content-type"
        },
        received_at=now,
    )
    session.add(delivery)
    await session.commit()  # до разбора
    delivery_id = delivery.id  # после rollback атрибуты объекта недоступны
    try:
        return await _process(session, provider, delivery_id, now)
    except Exception as exc:  # noqa: BLE001 - доставка уже сохранена; это последняя линия защиты
        await session.rollback()
        name = type(exc).__name__
        log.error("webhook processing failed: %s (delivery %s)", name, delivery_id)
        failed = await session.get(RawWebhookDelivery, delivery_id)
        if failed is not None:
            failed.parse_status = "FAILED"
        session.add(
            SystemIssue(kind="WEBHOOK_PROCESSING_FAILED", detail=f"delivery {delivery_id}: {name}")
        )
        await session.commit()
        return IntakeResult(
            500, {"error": {"code": "internal_error", "message": "Processing failed"}}
        )


async def _find_event(session: AsyncSession, provider: str, event_id: str) -> WebhookEvent | None:
    return await session.scalar(
        select(WebhookEvent).where(
            WebhookEvent.provider == provider, WebhookEvent.provider_event_id == event_id
        )
    )


async def _resolve_existing(
    session: AsyncSession, delivery_id: int, event: WebhookEvent, fp: str
) -> IntakeResult:
    delivery = await session.get(RawWebhookDelivery, delivery_id)
    assert delivery is not None
    delivery.event_id = event.id
    if event.fingerprint != fp:
        delivery.parse_status = "CONFLICT"
        session.add(
            SystemIssue(
                kind="WEBHOOK_IDEMPOTENCY_CONFLICT",
                detail=f"delivery {delivery_id} reuses event id {event.provider_event_id} "
                "with different content",
            )
        )
        await session.commit()
        return IntakeResult(409, {"error": {"code": "idempotency_conflict", "message": "Conflict"}})
    delivery.parse_status = "DUPLICATE"
    await session.commit()
    return IntakeResult(200, {"result": "duplicate"})


async def _process(
    session: AsyncSession, provider: PaymentProvider, delivery_id: int, now: datetime
) -> IntakeResult:
    delivery = await session.get(RawWebhookDelivery, delivery_id)
    assert delivery is not None
    try:
        ev: ProviderEvent = await provider.parse_webhook(delivery.raw_body, delivery.headers)
    except WebhookPayloadError:
        delivery.parse_status = "INVALID"
        await session.commit()
        return IntakeResult(400, {"error": {"code": "invalid_payload", "message": "Bad payload"}})
    fp = fingerprint(ev.canonical)

    existing = await _find_event(session, provider.name, ev.event_id)
    if existing is not None:
        return await _resolve_existing(session, delivery_id, existing, fp)

    event = WebhookEvent(
        provider=provider.name,
        provider_event_id=ev.event_id,
        fingerprint=fp,
        type=ev.type,
        received_at=now,
    )
    session.add(event)
    try:
        await session.flush()  # UNIQUE решает гонку параллельных доставок
    except IntegrityError:
        await session.rollback()
        winner = await _find_event(session, provider.name, ev.event_id)
        if winner is None:
            raise  # нарушение без победителя — настоящая ошибка
        return await _resolve_existing(session, delivery_id, winner, fp)

    try:
        outcome = await apply_provider_event(session, ev, now, provider.name)
    except InvalidTransition as exc:
        # Недопустимый переход: повтор не поможет. Событие откатываем, но SystemIssue
        # пишем заново — rollback унёс и ту запись, что сделал автомат.
        await session.rollback()
        d = await session.get(RawWebhookDelivery, delivery_id)
        assert d is not None
        d.parse_status = "REJECTED"
        session.add(
            SystemIssue(
                kind="INVALID_SUBSCRIPTION_TRANSITION",
                detail=f"delivery {delivery_id}: {exc}",
            )
        )
        await session.commit()
        return IntakeResult(200, {"result": "rejected_invalid_transition"})
    event.processed_at = now
    delivery = await session.get(RawWebhookDelivery, delivery_id)
    assert delivery is not None
    delivery.event_id = event.id
    delivery.parse_status = "OK"
    await session.commit()
    return IntakeResult(200, {"result": "processed", "outcome": outcome})
