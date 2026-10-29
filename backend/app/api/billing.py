from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceCtx, require
from app.billing import service
from app.billing.limits import count_active_invites, count_paid_members, get_subscription
from app.billing.plans import CURRENCY, SEAT_PRICE
from app.billing.providers import make_provider
from app.billing.providers.mock import KNOWN_CARDS, MockProvider, normalize_card
from app.billing.webhooks import receive
from app.clock import Clock, get_clock
from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.events import publish
from app.models import (
    ActivityLog,
    BillingPeriod,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    PaymentMethod,
    Plan,
    Workspace,
)
from app.ratelimit import check_rate_limit
from app.redis import get_redis
from app.schemas.billing import (
    BillingOut,
    CardOut,
    CheckoutOut,
    InvoiceLineOut,
    InvoiceOut,
    InvoiceStatusOut,
    MockCheckoutOut,
    MockPayIn,
    PeriodIn,
    ReturnUrlIn,
    SeatsIn,
    SubscribeIn,
)
from app.schemas.errors import ErrorResponse

router = APIRouter(prefix="/w/{workspace_id}/billing", tags=["billing"])
public_router = APIRouter(tags=["billing"])


def _ensure_verified(ctx: WorkspaceCtx) -> None:
    if ctx.user.email_verified_at is None:
        raise AppError(403, "email_not_verified", "Confirm your email before paying")


def _return_url(slug: str, invoice_id: int | None = None, custom: str | None = None) -> str:
    if custom:
        return custom
    base = f"{get_settings().public_url}/w/{slug}/billing/return"
    return f"{base}?invoice={invoice_id}" if invoice_id else base


async def _slug(session: AsyncSession, workspace_id: int) -> str:
    ws = await session.get(Workspace, workspace_id)
    assert ws is not None
    return ws.slug


async def _invoice_out(session: AsyncSession, inv: Invoice) -> InvoiceOut:
    lines = (
        await session.scalars(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id).order_by(InvoiceLine.id)
        )
    ).all()
    return InvoiceOut(
        id=inv.id,
        kind=inv.kind.value,
        amount=inv.amount,
        currency=inv.currency,
        status=inv.status.value,
        period_start=inv.period_start,
        period_end=inv.period_end,
        created_at=inv.created_at,
        paid_at=inv.paid_at,
        lines=[
            InvoiceLineOut(
                description=line.description,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                amount=line.amount,
            )
            for line in lines
        ],
    )


@router.get(
    "",
    response_model=BillingOut,
    summary="Подписка: план, места, следующее списание, карта",
)
async def overview(ctx: WorkspaceCtx = Depends(require("billing.view"))) -> BillingOut:
    now = await ctx.now_.now()
    session = ctx.session
    sub = await get_subscription(session, ctx.workspace_id)
    used = await count_paid_members(session, ctx.workspace_id)
    reserved = await count_active_invites(session, ctx.workspace_id, now)
    card = (
        await session.get(PaymentMethod, sub.payment_method_id) if sub.payment_method_id else None
    )
    when, amount = service.next_charge(sub, used + reserved)
    outstanding = await service.outstanding_invoice(session, ctx.workspace_id)
    trial_used = await session.scalar(
        select(ActivityLog.id)
        .where(
            ActivityLog.workspace_id == ctx.workspace_id,
            ActivityLog.action == "billing.trial_started",
        )
        .limit(1)
    )
    return BillingOut(
        plan=sub.plan.value,
        status=sub.status.value,
        billing_period=sub.billing_period,
        pending_period=sub.pending_period,
        seats_paid=sub.seats,
        seats_used=used,
        seats_reserved=reserved,
        pending_seats=sub.pending_seats,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        trial_end=sub.trial_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        past_due_since=sub.past_due_since,
        next_charge_at=when,
        next_charge_amount=amount,
        card=CardOut(brand=card.brand, last4=card.last4) if card else None,
        outstanding_invoice_id=outstanding.id if outstanding else None,
        seat_price_month=SEAT_PRICE[BillingPeriod.month],
        seat_price_year=SEAT_PRICE[BillingPeriod.year],
        currency=CURRENCY,
        trial_available=sub.plan == Plan.free and trial_used is None,
        provider=get_settings().payment_provider,
    )


@router.get("/invoices", response_model=list[InvoiceOut], summary="Счета со строками")
async def invoices(ctx: WorkspaceCtx = Depends(require("billing.view"))) -> list[InvoiceOut]:
    rows = (
        await ctx.session.scalars(
            select(Invoice)
            .where(Invoice.workspace_id == ctx.workspace_id)
            .order_by(Invoice.id.desc())
            .limit(100)
        )
    ).all()
    return [await _invoice_out(ctx.session, i) for i in rows]


@router.get(
    "/invoices/{invoice_id}/status",
    response_model=InvoiceStatusOut,
    summary="Статус счёта и подписки (страница возврата опрашивает, пока не придёт вебхук)",
)
async def invoice_status(
    invoice_id: int, ctx: WorkspaceCtx = Depends(require("billing.view"))
) -> InvoiceStatusOut:
    inv = await ctx.session.get(Invoice, invoice_id)
    if inv is None or inv.workspace_id != ctx.workspace_id:
        raise AppError(404, "invoice_not_found", "Invoice not found")
    sub = await get_subscription(ctx.session, ctx.workspace_id)
    return InvoiceStatusOut(
        invoice_id=inv.id,
        invoice_status=inv.status.value,
        subscription_status=sub.status.value,
        plan=sub.plan.value,
    )


@router.post(
    "/trial",
    response_model=BillingOut,
    summary="Начать 14-дневный trial Pro без карты",
    responses={409: {"model": ErrorResponse, "description": "Trial уже использован / уже Pro"}},
)
async def start_trial(
    body: PeriodIn, ctx: WorkspaceCtx = Depends(require("billing.manage"))
) -> BillingOut:
    _ensure_verified(ctx)
    await service.start_trial(
        ctx.session, ctx.workspace_id, body.period, ctx.user.id, await ctx.now_.now()
    )
    await ctx.session.commit()
    await ctx.publish("billing.changed")
    return await overview(ctx)


@router.post(
    "/checkout", response_model=CheckoutOut, summary="Оплатить Pro: счёт и редирект в checkout"
)
async def upgrade_checkout(
    body: SubscribeIn, ctx: WorkspaceCtx = Depends(require("billing.manage"))
) -> CheckoutOut:
    _ensure_verified(ctx)
    now = await ctx.now_.now()
    provider = make_provider(ctx.session, now)
    slug = await _slug(ctx.session, ctx.workspace_id)
    invoice, url = await service.start_upgrade_checkout(
        ctx.session, provider, ctx.workspace_id, body.period,
        _return_url(slug, None, body.return_url), ctx.user.id, now,
    )  # fmt: skip
    await ctx.session.commit()
    return CheckoutOut(checkout_url=url, invoice_id=invoice.id)


@router.post("/card/checkout", response_model=CheckoutOut, summary="Привязать или обновить карту")
async def card_checkout(
    body: ReturnUrlIn, ctx: WorkspaceCtx = Depends(require("billing.manage"))
) -> CheckoutOut:
    _ensure_verified(ctx)
    now = await ctx.now_.now()
    slug = await _slug(ctx.session, ctx.workspace_id)
    invoice, url = await service.start_card_checkout(
        ctx.session, make_provider(ctx.session, now), ctx.workspace_id,
        _return_url(slug, None, body.return_url), ctx.user.id, now,
    )  # fmt: skip
    await ctx.session.commit()
    return CheckoutOut(checkout_url=url, invoice_id=invoice.id)


@router.post(
    "/pay-outstanding",
    response_model=CheckoutOut,
    summary="Оплатить просроченный счёт (новой картой); после успеха подписка сразу active",
)
async def pay_outstanding(
    body: ReturnUrlIn, ctx: WorkspaceCtx = Depends(require("billing.manage"))
) -> CheckoutOut:
    _ensure_verified(ctx)
    now = await ctx.now_.now()
    slug = await _slug(ctx.session, ctx.workspace_id)
    invoice, url = await service.start_outstanding_checkout(
        ctx.session, make_provider(ctx.session, now), ctx.workspace_id,
        _return_url(slug, None, body.return_url), ctx.user.id, now,
    )  # fmt: skip
    await ctx.session.commit()
    return CheckoutOut(checkout_url=url, invoice_id=invoice.id)


async def _simple(ctx: WorkspaceCtx, action: Any) -> BillingOut:
    await action
    await ctx.session.commit()
    await ctx.publish("billing.changed")
    return await overview(ctx)


@router.post("/cancel", response_model=BillingOut, summary="Отменить подписку в конце периода")
async def cancel(ctx: WorkspaceCtx = Depends(require("billing.manage"))) -> BillingOut:
    return await _simple(
        ctx,
        service.cancel_subscription(
            ctx.session, ctx.workspace_id, ctx.user.id, await ctx.now_.now()
        ),
    )


@router.post("/resume", response_model=BillingOut, summary="Отменить отмену")
async def resume(ctx: WorkspaceCtx = Depends(require("billing.manage"))) -> BillingOut:
    return await _simple(
        ctx,
        service.resume_subscription(
            ctx.session, ctx.workspace_id, ctx.user.id, await ctx.now_.now()
        ),
    )


@router.post(
    "/seats", response_model=BillingOut, summary="Уменьшить число мест со следующего периода"
)
async def seats(
    body: SeatsIn, ctx: WorkspaceCtx = Depends(require("billing.manage"))
) -> BillingOut:
    return await _simple(
        ctx,
        service.set_seats(
            ctx.session, ctx.workspace_id, body.seats, ctx.user.id, await ctx.now_.now()
        ),
    )


@router.post(
    "/period", response_model=BillingOut, summary="Сменить месяц ↔ год со следующего периода"
)
async def period(
    body: PeriodIn, ctx: WorkspaceCtx = Depends(require("billing.manage"))
) -> BillingOut:
    return await _simple(
        ctx,
        service.set_period(
            ctx.session, ctx.workspace_id, body.period, ctx.user.id, await ctx.now_.now()
        ),
    )


# --- Вебхуки и mock-checkout (без авторизации пользователя) -----------------------------


@public_router.post(
    "/billing/webhooks/{provider}",
    summary="Вебхук платёжного провайдера",
    responses={
        401: {"model": ErrorResponse, "description": "Неподлинный вебхук"},
        409: {"model": ErrorResponse, "description": "Тот же id события с другим содержимым"},
    },
)
async def webhook(
    provider: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    settings = get_settings()
    if provider != settings.payment_provider:
        raise AppError(404, "unknown_provider", "Unknown payment provider")
    ip = request.client.host if request.client else "unknown"
    await check_rate_limit(redis, "billing-webhook", ip, settings.rate_limit_webhook_per_minute)
    now = await clock.now()
    headers = dict(request.headers)
    headers["x-client-ip"] = ip
    result = await receive(session, make_provider(session, now), await request.body(), headers, now)
    if result.workspace_id is not None:  # открытые вкладки обновят баннеры и счета
        await publish(redis, result.workspace_id, "billing.changed")
    return JSONResponse(result.body, status_code=result.http_status)


def _require_mock() -> None:
    if get_settings().payment_provider != "mock":
        raise AppError(404, "not_found", "Not found")


async def _mock_invoice(session: AsyncSession, session_id: str) -> Invoice:
    inv = await session.scalar(select(Invoice).where(Invoice.provider_payment_id == session_id))
    if inv is None:
        raise AppError(404, "checkout_not_found", "Checkout session not found")
    return inv


@public_router.get(
    "/mock/checkout/{session_id}", response_model=MockCheckoutOut, summary="Данные тестовой оплаты"
)
async def mock_checkout(
    session_id: str, session: AsyncSession = Depends(get_session)
) -> MockCheckoutOut:
    _require_mock()
    inv = await _mock_invoice(session, session_id)
    ws = await session.get(Workspace, inv.workspace_id)
    assert ws is not None
    out = await _invoice_out(session, inv)
    return MockCheckoutOut(
        session_id=session_id,
        invoice_id=inv.id,
        amount=inv.amount,
        currency=inv.currency,
        status=inv.status.value,
        kind=inv.kind.value,
        workspace_name=ws.name,
        slug=ws.slug,
        lines=out.lines,
    )


@public_router.post(
    "/mock/checkout/{session_id}/pay",
    summary="«Оплатить» тестовой картой: исход придёт вебхуком",
    responses={422: {"model": ErrorResponse, "description": "Неизвестная тестовая карта"}},
)
async def mock_pay(
    session_id: str,
    body: MockPayIn,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    _require_mock()
    inv = await _mock_invoice(session, session_id)
    if normalize_card(body.card_number) not in KNOWN_CARDS:
        raise AppError(422, "invalid_card", "Use a test card: 4242 4242 4242 4242")
    if inv.status == InvoiceStatus.paid:
        raise AppError(409, "already_paid", "Invoice is already paid")
    provider = make_provider(session, await clock.now())
    assert isinstance(provider, MockProvider)
    event_type = await provider.complete_checkout(inv, body.card_number)
    await session.commit()
    return {"status": "processing", "event": event_type, "invoice_id": str(inv.id)}
