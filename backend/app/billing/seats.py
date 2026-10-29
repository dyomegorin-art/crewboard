"""Места подписки: учёт, расчёт доплаты и резервирование под приглашение.

Спрос на места = занятые (owner/admin/member) + активные приглашения платных ролей.
Все изменения идут под `SELECT … FOR UPDATE` на строке подписки, поэтому два одновременных
приглашения не посчитают одно и то же свободное место дважды.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.invoices import Line, create_invoice
from app.billing.limits import count_active_invites, count_paid_members
from app.billing.plans import SEAT_PRICE
from app.billing.proration import prorate
from app.errors import AppError
from app.models import (
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    Plan,
    Role,
    Subscription,
    SubStatus,
)
from app.outbox import enqueue


@dataclass
class SeatQuote:
    requires_payment: bool
    amount: int  # копейки
    target_seats: int  # сколько мест оплачено после этого приглашения


async def pending_seat_capacity(session: AsyncSession, sub: Subscription) -> int:
    """Места с учётом уже выставленных, но ещё не оплаченных доплат."""
    unpaid = await session.scalar(
        select(func.max(Invoice.seats)).where(
            Invoice.workspace_id == sub.workspace_id,
            Invoice.kind == InvoiceKind.proration,
            Invoice.status.in_((InvoiceStatus.open, InvoiceStatus.failed)),
        )
    )
    return max(sub.seats, unpaid or 0)


async def quote_seat(
    session: AsyncSession, sub: Subscription, role: Role, now: datetime
) -> SeatQuote:
    """Нужна ли доплата за ещё одного участника роли `role` и сколько. Не меняет данные."""
    if role == Role.guest or sub.plan != Plan.pro:
        return SeatQuote(False, 0, sub.seats)
    if sub.status in (SubStatus.past_due, SubStatus.read_only):
        raise AppError(402, "subscription_past_due", "Pay the outstanding invoice first")
    demand = await count_paid_members(session, sub.workspace_id) + await count_active_invites(
        session, sub.workspace_id, now
    )
    new_demand = demand + 1
    capacity = await pending_seat_capacity(session, sub)
    if sub.status == SubStatus.trialing:
        return SeatQuote(False, 0, max(capacity, new_demand))
    if new_demand <= capacity:
        return SeatQuote(False, 0, capacity)
    assert sub.current_period_start and sub.current_period_end
    amount = prorate(
        SEAT_PRICE[sub.billing_period],
        sub.current_period_end - now,
        sub.current_period_end - sub.current_period_start,
    )
    return SeatQuote(True, amount, new_demand)


async def reserve_seat(
    session: AsyncSession, sub: Subscription, quote: SeatQuote, now: datetime
) -> Invoice | None:
    """Резервирует место под приглашение. При доплате создаёт proration-счёт и команду
    списания; место попадёт в `subscription.seats` только после вебхука об оплате."""
    if not quote.requires_payment:
        if sub.plan == Plan.pro and quote.target_seats > sub.seats:
            sub.seats = quote.target_seats  # trial: места растут вместе с командой
        return None
    assert sub.current_period_end
    invoice = await create_invoice(
        session,
        sub.workspace_id,
        InvoiceKind.proration,
        [
            Line(
                f"Доплата за 1 место до {sub.current_period_end:%d.%m.%Y}",
                1,
                quote.amount,
            )
        ],
        period_start=now,
        period_end=sub.current_period_end,
        seats=quote.target_seats,
    )
    await enqueue(
        session,
        "CHARGE_INVOICE",
        {"workspace_id": sub.workspace_id, "invoice_id": invoice.id},
        now=now,
        dedupe_key=f"charge:{invoice.id}:1",
    )
    return invoice


async def shrink_pending_seats(session: AsyncSession, sub: Subscription, now: datetime) -> None:
    """После ухода участника уменьшаем места со следующего периода (деньги не возвращаем)."""
    if sub.plan != Plan.pro:
        return
    demand = await count_paid_members(session, sub.workspace_id) + await count_active_invites(
        session, sub.workspace_id, now
    )
    target = max(demand, 1)
    if target < sub.seats:
        sub.pending_seats = target
    else:
        sub.pending_seats = None
