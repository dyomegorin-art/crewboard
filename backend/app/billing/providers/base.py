from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.models import Invoice, PaymentMethod


class WebhookAuthError(Exception):
    """Вебхук не прошёл проверку подлинности → 401, ничего не сохраняем."""


class WebhookPayloadError(Exception):
    """Подлинный вебхук с нечитаемым телом → 400."""


@dataclass
class CheckoutSession:
    id: str  # provider_payment_id
    url: str


@dataclass
class ChargeResult:
    # pending — платёж создан, итог придёт вебхуком; failed — провайдер отказал сразу.
    status: Literal["pending", "failed"]
    provider_payment_id: str | None = None
    error: str | None = None


@dataclass
class MethodInfo:
    token: str
    brand: str
    last4: str


@dataclass
class ProviderEvent:
    event_id: str
    type: str  # payment.succeeded | payment.failed | ...
    provider_payment_id: str | None
    invoice_id: int | None
    method: MethodInfo | None = None
    amount: int | None = None  # копейки, если провайдер сообщает сумму
    # Что именно участвует в fingerprint: для вебхука без подписи это ответ API провайдера,
    # а не присланное тело.
    canonical: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(
        self, *, invoice: Invoice, return_url: str, save_method: bool
    ) -> CheckoutSession: ...

    async def charge_saved_method(
        self, *, invoice: Invoice, method: PaymentMethod, idempotency_key: str
    ) -> ChargeResult: ...

    async def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        """Проверка подлинности; бросает WebhookAuthError."""
        ...

    async def parse_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> ProviderEvent:
        """Разбор тела в событие; бросает WebhookPayloadError."""
        ...
