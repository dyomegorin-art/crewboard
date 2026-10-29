from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.providers.base import PaymentProvider
from app.config import get_settings


def make_provider(session: AsyncSession, now: datetime) -> PaymentProvider:
    """Провайдер из настроек. Остальной код знает только про интерфейс PaymentProvider."""
    name = get_settings().payment_provider
    if name == "yookassa":
        from app.billing.providers.yookassa import YooKassaProvider

        return YooKassaProvider()  # type: ignore[return-value]
    from app.billing.providers.mock import MockProvider

    return MockProvider(session, now)
