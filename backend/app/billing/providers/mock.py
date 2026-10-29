"""Тестовый провайдер: всё работает без внешних ключей.

Оплата на странице /mock-checkout/:session не меняет состояние напрямую: провайдер ставит в
outbox команду MOCK_WEBHOOK, воркер шлёт подписанный HMAC вебхук на наш же эндпоинт. Так
путь «вебхук → состояние» один для всех провайдеров.
"""

import hashlib
import hmac
import json
import random
import secrets
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.providers.base import (
    ChargeResult,
    CheckoutSession,
    MethodInfo,
    ProviderEvent,
    WebhookAuthError,
    WebhookPayloadError,
)
from app.config import get_settings
from app.models import Invoice, PaymentMethod
from app.outbox import enqueue

SIGNATURE_HEADER = "x-mock-signature"

# Тестовые карты.
CARD_OK = "4242424242424242"
CARD_DECLINED = "4000000000000002"
CARD_LATER_DECLINED = "4000000000009995"  # успех при оплате, дальше автосписания отклоняются
KNOWN_CARDS = {CARD_OK, CARD_DECLINED, CARD_LATER_DECLINED}


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def normalize_card(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


class MockProvider:
    name = "mock"

    def __init__(
        self,
        session: AsyncSession,
        now: datetime,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._session = session
        self._now = now
        self._rng = rng

    async def create_checkout(
        self, *, invoice: Invoice, return_url: str, save_method: bool
    ) -> CheckoutSession:
        payment_id = f"mockpay_{uuid.uuid4().hex}"
        return CheckoutSession(
            id=payment_id, url=f"{get_settings().public_url}/mock-checkout/{payment_id}"
        )

    async def charge_saved_method(
        self, *, invoice: Invoice, method: PaymentMethod, idempotency_key: str
    ) -> ChargeResult:
        payment_id = f"mockpay_{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex}"
        declined = "later" in method.provider_token or "declined" in method.provider_token
        await self._emit(
            payment_id, invoice.id, "payment.failed" if declined else "payment.succeeded", None
        )
        return ChargeResult(status="pending", provider_payment_id=payment_id)

    async def complete_checkout(self, invoice: Invoice, card_number: str) -> str:
        """Страница оплаты нажала «Оплатить»: решаем исход по тестовой карте и шлём вебхук.
        Возвращает тип события."""
        card = normalize_card(card_number)
        assert invoice.provider_payment_id
        if card == CARD_DECLINED:
            event_type, method = "payment.failed", None
        else:
            token = (
                f"mock_pm_{'later' if card == CARD_LATER_DECLINED else 'ok'}_{secrets.token_hex(4)}"
            )
            method = MethodInfo(
                token=token, brand="Mastercard" if card.startswith("5") else "Visa", last4=card[-4:]
            )
            event_type = "payment.succeeded"
        await self._emit(invoice.provider_payment_id, invoice.id, event_type, method)
        return event_type

    async def _emit(
        self, payment_id: str, invoice_id: int, event_type: str, method: MethodInfo | None
    ) -> None:
        event: dict[str, Any] = {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": event_type,
            "payment_id": payment_id,
            "invoice_id": invoice_id,
        }
        if method is not None:
            event["method"] = {"token": method.token, "brand": method.brand, "last4": method.last4}
        copies = 2 if self._rng() < get_settings().mock_webhook_duplicate_rate else 1
        for n in range(copies):  # дубль — то же событие дважды: проверяем идемпотентность
            await enqueue(
                self._session,
                "MOCK_WEBHOOK",
                {"event": event},
                now=self._now,
                dedupe_key=f"mock-webhook:{event['id']}:{n}",
            )

    async def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        secret = get_settings().webhook_secret
        got = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER, "")
        if not secret or not hmac.compare_digest(got.strip().lower(), sign(raw_body, secret)):
            raise WebhookAuthError("bad signature")

    async def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> ProviderEvent:
        try:
            data = json.loads(raw_body)
            method = data.get("method")
            return ProviderEvent(
                event_id=str(data["id"]),
                type=str(data["type"]),
                provider_payment_id=data.get("payment_id"),
                invoice_id=int(data["invoice_id"]) if data.get("invoice_id") is not None else None,
                method=MethodInfo(**method) if method else None,
                canonical=data,
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise WebhookPayloadError(type(exc).__name__) from exc
