"""Адаптер YooKassa на фикстурах: HTTP замокан (httpx.MockTransport), сеть не используется."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.providers import yookassa
from app.billing.providers.base import WebhookAuthError, WebhookPayloadError
from app.billing.providers.yookassa import YooKassaProvider, kopecks, rubles
from app.config import get_settings
from app.models import Invoice, InvoiceKind, PaymentMethod, SystemIssue

from .helpers import W, invoices_of, sub_of

FIXTURES = Path(__file__).parent / "fixtures" / "yookassa"
GOOD_IP = {"x-client-ip": "185.71.76.5"}


def fx(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())  # type: ignore[no-any-return]


class Api:
    """Запоминает запросы и отвечает фикстурами."""

    def __init__(self, responses: dict[tuple[str, str], tuple[int, dict[str, Any]]]) -> None:
        self.responses = responses
        self.calls: list[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            status, body = self.responses[(request.method, request.url.path)]
            return httpx.Response(status, json=body)

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.yookassa.ru/v3",
            auth=("shop", "secret"),
        )


def invoice(**kw: Any) -> Invoice:
    base: dict[str, Any] = dict(
        id=1,
        workspace_id=1,
        kind=InvoiceKind.subscription,
        amount=49000,
        currency="RUB",
        attempts=0,
    )
    return Invoice(**{**base, **kw})


def test_money_helpers() -> None:
    assert rubles(49000) == "490.00" and rubles(5) == "0.05" and rubles(123456) == "1234.56"
    assert kopecks("490.00") == 49000 and kopecks("0.05") == 5


async def test_create_checkout_sends_saved_method_redirect_and_idempotence_key() -> None:
    api = Api({("POST", "/v3/payments"): (200, fx("payment_pending.json"))})
    p = YooKassaProvider(api.client())
    session = await p.create_checkout(
        invoice=invoice(), return_url="https://app.example/return", save_method=True
    )
    assert session.id == "2f1a7c10-000f-5000-8000-1d3a8f2b6c11" and "yoomoney.ru" in session.url
    (req,) = api.calls
    body = json.loads(req.content)
    assert body["amount"] == {"value": "490.00", "currency": "RUB"}
    assert body["save_payment_method"] is True and body["capture"] is True
    assert body["confirmation"] == {"type": "redirect", "return_url": "https://app.example/return"}
    assert body["metadata"] == {"invoice_id": "1"}
    assert req.headers["Idempotence-Key"] == "checkout:1:0"


async def test_charge_saved_method_uses_payment_method_id_and_caller_key() -> None:
    api = Api({("POST", "/v3/payments"): (200, {**fx("payment_pending.json"), "id": "pay-1"})})
    method = PaymentMethod(
        workspace_id=1, provider="yookassa", provider_token="pm-token", brand="Visa", last4="1111"
    )
    res = await YooKassaProvider(api.client()).charge_saved_method(
        invoice=invoice(), method=method, idempotency_key="renewal:1:2026-02-15"
    )
    assert (res.status, res.provider_payment_id) == ("pending", "pay-1")
    req = api.calls[0]
    assert json.loads(req.content)["payment_method_id"] == "pm-token"
    assert "confirmation" not in json.loads(req.content)
    assert req.headers["Idempotence-Key"] == "renewal:1:2026-02-15"


async def test_charge_canceled_by_provider_is_an_immediate_failure() -> None:
    api = Api({("POST", "/v3/payments"): (200, fx("payment_canceled.json"))})
    method = PaymentMethod(
        workspace_id=1, provider="yookassa", provider_token="pm", brand="Visa", last4="1111"
    )
    res = await YooKassaProvider(api.client()).charge_saved_method(
        invoice=invoice(), method=method, idempotency_key="k"
    )
    assert (res.status, res.error) == ("failed", "insufficient_funds")


async def test_api_errors_surface_as_exceptions_for_outbox_retry() -> None:
    api = Api({("POST", "/v3/payments"): (500, {"type": "error"})})
    with pytest.raises(yookassa.YooKassaError):
        await YooKassaProvider(api.client()).create_checkout(
            invoice=invoice(), return_url="x", save_method=True
        )

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    down = httpx.AsyncClient(
        transport=httpx.MockTransport(boom), base_url="https://api.yookassa.ru/v3"
    )
    with pytest.raises(yookassa.YooKassaError):
        await YooKassaProvider(down).create_checkout(
            invoice=invoice(), return_url="x", save_method=True
        )


@pytest.mark.parametrize(
    "ip",
    [
        "185.71.76.31",
        "185.71.77.1",
        "77.75.153.100",
        "77.75.156.11",
        "77.75.156.35",
        "77.75.154.200",
        "2a02:5180:0:1::1",
    ],
)
async def test_webhook_from_official_ips_is_accepted(ip: str) -> None:
    await YooKassaProvider().verify_webhook(b"{}", {"x-client-ip": ip})


@pytest.mark.parametrize(
    "headers",
    [
        {"x-client-ip": "8.8.8.8"},
        {"x-client-ip": "185.71.76.99"},
        {"x-client-ip": "garbage"},
        {},
        {"x-client-ip": "77.75.156.12"},
    ],
)
async def test_webhook_from_other_ips_is_rejected(headers: dict[str, str]) -> None:
    with pytest.raises(WebhookAuthError):
        await YooKassaProvider().verify_webhook(b"{}", headers)


async def test_forwarded_header_wins_over_proxy_address() -> None:
    p = YooKassaProvider()
    await p.verify_webhook(
        b"{}", {"x-forwarded-for": "185.71.76.5, 10.0.0.1", "x-client-ip": "10.0.0.2"}
    )
    with pytest.raises(WebhookAuthError):
        await p.verify_webhook(b"{}", {"x-forwarded-for": "8.8.8.8", "x-client-ip": "185.71.76.5"})


async def test_parse_trusts_only_the_api_response_not_the_notification_body() -> None:
    # Тело уведомления врёт, что платёж succeeded; API говорит: ещё pending.
    api = Api(
        {
            ("GET", "/v3/payments/2f1a7c10-000f-5000-8000-1d3a8f2b6c11"): (
                200,
                fx("payment_pending.json"),
            )
        }
    )
    ev = await YooKassaProvider(api.client()).parse_webhook(
        json.dumps(fx("notification_succeeded.json")).encode(), GOOD_IP
    )
    assert ev.type == "payment.pending" and ev.method is None
    assert api.calls[0].method == "GET"


async def test_parse_succeeded_extracts_saved_card_and_invoice() -> None:
    api = Api(
        {
            ("GET", "/v3/payments/2f1a7c10-000f-5000-8000-1d3a8f2b6c11"): (
                200,
                fx("payment_succeeded.json"),
            )
        }
    )
    ev = await YooKassaProvider(api.client()).parse_webhook(
        json.dumps(fx("notification_succeeded.json")).encode(), GOOD_IP
    )
    assert ev.type == "payment.succeeded" and ev.invoice_id == 1 and ev.amount == 49000
    assert ev.event_id == "2f1a7c10-000f-5000-8000-1d3a8f2b6c11:succeeded"
    assert ev.method is not None and (ev.method.brand, ev.method.last4) == ("MasterCard", "4444")


async def test_parse_canceled_maps_to_failed_and_bad_input_is_rejected() -> None:
    api = Api(
        {
            ("GET", "/v3/payments/abc"): (200, fx("payment_canceled.json")),
            ("GET", "/v3/payments/missing"): (404, {}),
        }
    )
    p = YooKassaProvider(api.client())
    ev = await p.parse_webhook(b'{"object": {"id": "abc"}}', GOOD_IP)
    assert ev.type == "payment.failed" and ev.method is None
    for body in (b"not json", b'{"object": {}}', b'{"object": {"id": "missing"}}'):
        with pytest.raises(WebhookPayloadError):
            await p.parse_webhook(body, GOOD_IP)


@pytest.fixture
def yk(monkeypatch: pytest.MonkeyPatch) -> Api:
    s = get_settings()
    monkeypatch.setattr(s, "payment_provider", "yookassa")
    monkeypatch.setattr(s, "yookassa_shop_id", "shop")
    monkeypatch.setattr(s, "yookassa_secret_key", "secret")
    api = Api(
        {
            ("POST", "/v3/payments"): (200, fx("payment_pending.json")),
            ("GET", "/v3/payments/2f1a7c10-000f-5000-8000-1d3a8f2b6c11"): (
                200,
                fx("payment_succeeded.json"),
            ),
        }
    )
    monkeypatch.setattr(yookassa, "make_http_client", api.client)
    return api


async def test_full_upgrade_through_yookassa_webhook(
    client: AsyncClient, factory: Any, yk: Api, session: AsyncSession
) -> None:
    ws, _, oh = await factory.workspace()
    r = await client.post(W(ws, "/billing/checkout"), json={"period": "month"}, headers=oh)
    assert r.json()["checkout_url"].startswith("https://yoomoney.ru/")
    (inv,) = await invoices_of(ws)
    assert inv.provider_payment_id == "2f1a7c10-000f-5000-8000-1d3a8f2b6c11" and inv.amount == 49000

    url = "/api/billing/webhooks/yookassa"
    body = json.dumps(fx("notification_succeeded.json")).encode()
    # С чужого адреса — 401 и ничего не сохранено.
    bad = await client.post(url, content=body, headers={"x-forwarded-for": "8.8.8.8"})
    assert bad.status_code == 401
    from app.models import RawWebhookDelivery

    assert (await session.scalars(select(RawWebhookDelivery))).all() == []

    ok = await client.post(url, content=body, headers={"x-forwarded-for": "185.71.76.5"})
    assert ok.status_code == 200 and ok.json()["result"] == "processed"
    sub = await sub_of(ws)
    assert (sub.plan, sub.status) == ("pro", "active")
    pm = await session.get(PaymentMethod, sub.payment_method_id)
    assert pm is not None and (pm.provider, pm.brand, pm.last4) == (
        "yookassa",
        "MasterCard",
        "4444",
    )
    # Повтор того же уведомления — дубль.
    again = await client.post(url, content=body, headers={"x-forwarded-for": "185.71.76.5"})
    assert again.json() == {"result": "duplicate"}
    # mock-вебхук при активном yookassa недоступен.
    assert (await client.post("/api/billing/webhooks/mock", content=b"{}")).status_code == 404


async def test_amount_mismatch_is_not_applied(
    client: AsyncClient, factory: Any, yk: Api, session: AsyncSession
) -> None:
    ws, _, oh = await factory.workspace()
    await client.post(W(ws, "/billing/checkout"), json={"period": "month"}, headers=oh)
    cheap = {**fx("payment_succeeded.json"), "amount": {"value": "1.00", "currency": "RUB"}}
    yk.responses[("GET", "/v3/payments/2f1a7c10-000f-5000-8000-1d3a8f2b6c11")] = (200, cheap)
    r = await client.post(
        "/api/billing/webhooks/yookassa",
        content=json.dumps(fx("notification_succeeded.json")).encode(),
        headers={"x-forwarded-for": "185.71.76.5"},
    )
    assert r.json()["outcome"] == "amount_mismatch"
    assert (await sub_of(ws)).plan == "free"
    issue = await session.scalar(
        select(SystemIssue).where(SystemIssue.kind == "PAYMENT_AMOUNT_MISMATCH")
    )
    assert issue is not None


async def test_pending_status_is_ignored_until_final(
    client: AsyncClient, factory: Any, yk: Api
) -> None:
    ws, _, oh = await factory.workspace()
    await client.post(W(ws, "/billing/checkout"), json={"period": "month"}, headers=oh)
    yk.responses[("GET", "/v3/payments/2f1a7c10-000f-5000-8000-1d3a8f2b6c11")] = (
        200,
        fx("payment_pending.json"),
    )
    r = await client.post(
        "/api/billing/webhooks/yookassa",
        content=json.dumps(fx("notification_succeeded.json")).encode(),
        headers={"x-forwarded-for": "185.71.76.5"},
    )
    assert r.json()["outcome"] == "ignored_type" and (await sub_of(ws)).plan == "free"
