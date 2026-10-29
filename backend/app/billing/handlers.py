"""Обработчики outbox-команд. Импорт модуля регистрирует их в реестре."""

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.providers.mock import SIGNATURE_HEADER, canonical_json, sign
from app.billing.service import charge_invoice, renew_subscription
from app.config import get_settings
from app.models import Invoice, OutboxCommand
from app.outbox import Deps, PermanentCommandError, handler

log = logging.getLogger("app.billing.handlers")


@handler("SEND_EMAIL")
async def send_email(session: AsyncSession, cmd: OutboxCommand, deps: Deps) -> dict[str, Any]:
    p = cmd.payload
    if not all(k in p for k in ("to", "subject", "body")):
        raise PermanentCommandError("malformed email command")
    await deps.send_email(p["to"], p["subject"], p["body"])
    return {}


@handler("MOCK_WEBHOOK")
async def mock_webhook(session: AsyncSession, cmd: OutboxCommand, deps: Deps) -> dict[str, Any]:
    """Шлёт подписанный вебхук на наш же эндпоинт — как настоящий провайдер."""
    settings = get_settings()
    event = cmd.payload.get("event")
    if not isinstance(event, dict):
        raise PermanentCommandError("malformed webhook command")
    body = canonical_json(event)
    status = await deps.post_webhook(
        f"{settings.api_internal_url}/api/billing/webhooks/mock",
        body,
        {"content-type": "application/json", SIGNATURE_HEADER: sign(body, settings.webhook_secret)},
    )
    if status == 409:
        raise PermanentCommandError("webhook rejected as conflicting")
    if status >= 400:
        raise RuntimeError(f"webhook endpoint answered {status}")  # временный сбой → ретрай
    return {"status": status}


@handler("CHARGE_INVOICE")
async def charge(session: AsyncSession, cmd: OutboxCommand, deps: Deps) -> dict[str, Any]:
    invoice = await session.get(Invoice, cmd.payload.get("invoice_id"), with_for_update=True)
    if invoice is None:
        raise PermanentCommandError("invoice not found")
    result = await charge_invoice(session, deps.make_provider(session), invoice, deps.now)
    return {"result": result}


@handler("RENEW_SUBSCRIPTION")
async def renew(session: AsyncSession, cmd: OutboxCommand, deps: Deps) -> dict[str, Any]:
    p = cmd.payload
    result = await renew_subscription(
        session, deps.make_provider(session), p["workspace_id"], p["period_end"], deps.now
    )
    return {"result": result}
