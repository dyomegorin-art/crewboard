"""Адаптер YooKassa (тестовый магазин). Включается PAYMENT_PROVIDER=yookassa.

- Платёж с `save_payment_method`, автосписания по `payment_method_id`, заголовок Idempotence-Key.
- У вебхука YooKassa нет подписи, поэтому подлинность проверяется в два шага: IP отправителя
  по официальному списку И повторный запрос объекта платежа через API. Состояние меняется
  только по ответу API, а не по присланному телу.
"""

import ipaddress
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import httpx

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

API = "https://api.yookassa.ru/v3"

# Официальный список адресов, с которых YooKassa шлёт уведомления.
TRUSTED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "185.71.76.0/27",
        "185.71.77.0/27",
        "77.75.153.0/25",
        "77.75.156.11/32",
        "77.75.156.35/32",
        "77.75.154.128/25",
        "2a02:5180::/32",
    )
]


class YooKassaError(Exception):
    pass


def rubles(kopecks: int) -> str:
    return str((Decimal(kopecks) / 100).quantize(Decimal("0.01")))


def kopecks(value: str) -> int:
    return int((Decimal(value) * 100).to_integral_value())


def make_http_client() -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        base_url=API, auth=(s.yookassa_shop_id, s.yookassa_secret_key), timeout=15
    )


def client_ip(headers: Mapping[str, str]) -> str:
    h = {k.lower(): v for k, v in headers.items()}
    forwarded = h.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or h.get("x-client-ip", "")


class YooKassaProvider:
    name = "yookassa"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        return self._client or make_http_client()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotence-Key": key} if key else {}
        client = self._http()
        try:
            r = await client.request(method, path, json=json_body, headers=headers)
        except httpx.HTTPError as exc:  # сеть: временный сбой, outbox повторит
            raise YooKassaError(type(exc).__name__) from exc
        finally:
            if self._client is None:
                await client.aclose()
        if r.status_code >= 400:
            raise YooKassaError(f"HTTP {r.status_code}")
        data: dict[str, Any] = r.json()
        return data

    async def create_checkout(
        self, *, invoice: Invoice, return_url: str, save_method: bool
    ) -> CheckoutSession:
        body = {
            "amount": {"value": rubles(invoice.amount), "currency": invoice.currency},
            "capture": True,
            "save_payment_method": save_method,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": f"Crewboard, счёт №{invoice.id}",
            "metadata": {"invoice_id": str(invoice.id)},
        }
        data = await self._call(
            "POST", "/payments", json_body=body, key=f"checkout:{invoice.id}:{invoice.attempts}"
        )
        return CheckoutSession(id=data["id"], url=data["confirmation"]["confirmation_url"])

    async def charge_saved_method(
        self, *, invoice: Invoice, method: PaymentMethod, idempotency_key: str
    ) -> ChargeResult:
        body = {
            "amount": {"value": rubles(invoice.amount), "currency": invoice.currency},
            "capture": True,
            "payment_method_id": method.provider_token,
            "description": f"Crewboard, счёт №{invoice.id}",
            "metadata": {"invoice_id": str(invoice.id)},
        }
        data = await self._call("POST", "/payments", json_body=body, key=idempotency_key)
        if data.get("status") == "canceled":
            reason = (data.get("cancellation_details") or {}).get("reason", "canceled")
            return ChargeResult(status="failed", provider_payment_id=data["id"], error=reason)
        return ChargeResult(status="pending", provider_payment_id=data["id"])

    async def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        try:
            ip = ipaddress.ip_address(client_ip(headers))
        except ValueError:
            raise WebhookAuthError("no sender ip") from None
        if not any(ip in net for net in TRUSTED_NETWORKS):
            raise WebhookAuthError("sender ip is not a YooKassa address")

    async def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> ProviderEvent:
        try:
            note = json.loads(raw_body)
            payment_id = str(note["object"]["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise WebhookPayloadError(type(exc).__name__) from exc
        # Доверяем только ответу API, а не телу уведомления.
        try:
            payment = await self._call("GET", f"/payments/{payment_id}")
        except YooKassaError as exc:
            raise WebhookPayloadError(str(exc)) from exc
        status = payment.get("status")
        event_type = {"succeeded": "payment.succeeded", "canceled": "payment.failed"}.get(
            str(status), "payment.pending"
        )
        pm = payment.get("payment_method") or {}
        card = pm.get("card") or {}
        method = (
            MethodInfo(
                token=str(pm["id"]),
                brand=str(card.get("card_type", "Card")),
                last4=str(card.get("last4", "0000")),
            )
            if pm.get("saved") and pm.get("id")
            else None
        )
        invoice_id = (payment.get("metadata") or {}).get("invoice_id")
        amount = payment.get("amount") or {}
        return ProviderEvent(
            event_id=f"{payment_id}:{status}",
            type=event_type,
            provider_payment_id=payment_id,
            invoice_id=int(invoice_id) if invoice_id and str(invoice_id).isdigit() else None,
            method=method,
            amount=kopecks(amount["value"]) if "value" in amount else None,
            canonical={
                "payment_id": payment_id,
                "status": status,
                "amount": amount,
                "invoice_id": invoice_id,
                "payment_method": pm.get("id"),
            },
        )
