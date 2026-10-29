"""Бизнес-логика подписок: trial, апгрейд, места, продления, dunning, отмена.

Платёжный провайдер только проводит платежи; всё остальное считается здесь. Состояние
подписки меняется ТОЛЬКО по событиям провайдера (`apply_provider_event`) — страница
возврата после оплаты лишь опрашивает статус.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.invoices import Line, create_invoice
from app.billing.limits import (
    count_active_invites,
    count_paid_members,
    get_subscription,
)
from app.billing.plans import (
    CARD_UPDATE_AMOUNT,
    DUNNING_RETRY_DAYS,
    READ_ONLY_AFTER_DAYS,
    SEAT_PRICE,
    TRIAL_DAYS,
    TRIAL_REMINDER_BEFORE,
    limits_for,
)
from app.billing.proration import period_end as calc_period_end
from app.billing.providers.base import PaymentProvider, ProviderEvent
from app.billing.state import Event, transition
from app.errors import AppError
from app.models import (
    ActivityLog,
    BillingPeriod,
    Invitation,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    PaymentMethod,
    Plan,
    Project,
    Subscription,
    SubStatus,
    SystemIssue,
    Task,
    User,
    Workspace,
)
from app.outbox import enqueue
from app.services.audit import audit
from app.services.invitations import send_invite_email

log = logging.getLogger("app.billing")

# Настоящие переходы идут через автомат; здесь — только то, что они не покрывают.
SYSTEM_ACTOR: int | None = None


def fmt_money(kopecks: int) -> str:
    rub, kop = divmod(kopecks, 100)
    return f"{rub:,}".replace(",", " ") + (f",{kop:02d}" if kop else "") + " ₽"


async def notify_owner(
    session: AsyncSession, workspace_id: int, subject: str, body: str, now: datetime
) -> None:
    owner = await session.scalar(
        select(User)
        .join(Workspace, Workspace.owner_id == User.id)
        .where(Workspace.id == workspace_id)
    )
    if owner is not None and not owner.is_demo:
        await enqueue(
            session, "SEND_EMAIL", {"to": owner.email, "subject": subject, "body": body}, now=now
        )


async def seat_demand(session: AsyncSession, workspace_id: int, now: datetime) -> int:
    """Места, которые нужны команде: занятые + приглашённые платных ролей (минимум одно)."""
    return max(
        1,
        await count_paid_members(session, workspace_id)
        + await count_active_invites(session, workspace_id, now),
    )


# --- Trial и апгрейд -------------------------------------------------------------------


async def start_trial(
    session: AsyncSession, workspace_id: int, period: BillingPeriod, actor_id: int, now: datetime
) -> Subscription:
    sub = await get_subscription(session, workspace_id, lock=True)
    if sub.plan == Plan.pro:
        raise AppError(409, "already_subscribed", "The workspace is already on Pro")
    used = await session.scalar(
        select(func.count())
        .select_from(ActivityLog)
        .where(
            ActivityLog.workspace_id == workspace_id, ActivityLog.action == "billing.trial_started"
        )
    )
    if used:
        raise AppError(409, "trial_already_used", "The free trial was already used")
    await transition(session, sub, Event.TRIAL_STARTED, now)
    sub.plan = Plan.pro
    sub.billing_period = period
    sub.seats = await seat_demand(session, workspace_id, now)
    sub.pending_seats = None
    sub.trial_end = now + timedelta(days=TRIAL_DAYS)
    sub.current_period_start = now
    sub.current_period_end = sub.trial_end
    sub.cancel_at_period_end = False
    sub.trial_reminder_sent_at = None
    await audit(
        session, workspace_id, actor_id, "billing", None, "billing.trial_started",
        now=now, period=period.value, trial_end=sub.trial_end.isoformat(),
    )  # fmt: skip
    return sub


async def start_upgrade_checkout(
    session: AsyncSession,
    provider: PaymentProvider,
    workspace_id: int,
    period: BillingPeriod,
    return_url: str,
    actor_id: int,
    now: datetime,
) -> tuple[Invoice, str]:
    """Free (или trial) → Pro: счёт на seats × цену и редирект на оплату с сохранением карты."""
    sub = await get_subscription(session, workspace_id, lock=True)
    if sub.plan == Plan.pro and sub.status != SubStatus.trialing:
        raise AppError(409, "already_subscribed", "The workspace is already on Pro")
    seats = await seat_demand(session, workspace_id, now)
    price = SEAT_PRICE[period]
    unit = "мес." if period == BillingPeriod.month else "год"
    invoice = await create_invoice(
        session,
        workspace_id,
        InvoiceKind.subscription,
        [Line(f"Crewboard Pro: место, {unit}", seats, price)],
        period_start=now,
        period_end=calc_period_end(now, period),
        billing_period=period.value,
        seats=seats,
    )
    checkout = await provider.create_checkout(
        invoice=invoice, return_url=return_url, save_method=True
    )
    invoice.provider_payment_id = checkout.id
    await audit(
        session, workspace_id, actor_id, "billing", invoice.id, "billing.checkout_started",
        now=now, kind="subscription", amount=invoice.amount, seats=seats,
    )  # fmt: skip
    return invoice, checkout.url


async def start_card_checkout(
    session: AsyncSession,
    provider: PaymentProvider,
    workspace_id: int,
    return_url: str,
    actor_id: int,
    now: datetime,
) -> tuple[Invoice, str]:
    """Привязка/смена карты: проверочный платёж (в реальной системе возвращается)."""
    sub = await get_subscription(session, workspace_id)
    if sub.plan != Plan.pro:
        raise AppError(409, "not_subscribed", "Upgrade to Pro first")
    invoice = await create_invoice(
        session,
        workspace_id,
        InvoiceKind.card_update,
        [Line("Проверка карты", 1, CARD_UPDATE_AMOUNT)],
    )
    checkout = await provider.create_checkout(
        invoice=invoice, return_url=return_url, save_method=True
    )
    invoice.provider_payment_id = checkout.id
    await audit(
        session, workspace_id, actor_id, "billing", invoice.id, "billing.checkout_started",
        now=now, kind="card_update",
    )  # fmt: skip
    return invoice, checkout.url


async def outstanding_invoice(session: AsyncSession, workspace_id: int) -> Invoice | None:
    return await session.scalar(
        select(Invoice)
        .where(
            Invoice.workspace_id == workspace_id,
            Invoice.kind == InvoiceKind.renewal,
            Invoice.status.in_((InvoiceStatus.failed, InvoiceStatus.open)),
        )
        .order_by(Invoice.id.desc())
        .limit(1)
    )


async def start_outstanding_checkout(
    session: AsyncSession,
    provider: PaymentProvider,
    workspace_id: int,
    return_url: str,
    actor_id: int,
    now: datetime,
) -> tuple[Invoice, str]:
    """Owner оплачивает просроченный счёт новой картой — после успеха сразу active."""
    sub = await get_subscription(session, workspace_id, lock=True)
    invoice = await outstanding_invoice(session, workspace_id)
    if sub.status not in (SubStatus.past_due, SubStatus.read_only) or invoice is None:
        raise AppError(409, "nothing_to_pay", "There is no overdue invoice")
    checkout = await provider.create_checkout(
        invoice=invoice, return_url=return_url, save_method=True
    )
    invoice.provider_payment_id = checkout.id
    invoice.status = InvoiceStatus.open
    await audit(
        session, workspace_id, actor_id, "billing", invoice.id, "billing.checkout_started",
        now=now, kind="overdue", amount=invoice.amount,
    )  # fmt: skip
    return invoice, checkout.url


# --- События провайдера → состояние ----------------------------------------------------


async def find_invoice(session: AsyncSession, ev: ProviderEvent) -> Invoice | None:
    invoice: Invoice | None = None
    if ev.provider_payment_id:
        invoice = await session.scalar(
            select(Invoice)
            .where(Invoice.provider_payment_id == ev.provider_payment_id)
            .with_for_update()
        )
    if invoice is None and ev.invoice_id is not None:
        invoice = await session.scalar(
            select(Invoice).where(Invoice.id == ev.invoice_id).with_for_update()
        )
    return invoice


async def apply_provider_event(
    session: AsyncSession, ev: ProviderEvent, now: datetime, provider_name: str
) -> str:
    """Применяет подтверждённое событие платежа. Возвращает краткий итог для лога."""
    if ev.type not in ("payment.succeeded", "payment.failed"):
        return "ignored_type"
    invoice = await find_invoice(session, ev)
    if invoice is None:
        session.add(
            SystemIssue(
                kind="WEBHOOK_UNKNOWN_INVOICE",
                detail=f"{provider_name} event {ev.event_id}: payment {ev.provider_payment_id}",
            )
        )
        return "unknown_invoice"
    sub = await get_subscription(session, invoice.workspace_id, lock=True)
    if ev.type == "payment.succeeded":
        return await _payment_succeeded(session, sub, invoice, ev, now, provider_name)
    return await _payment_failed(session, sub, invoice, now)


async def _save_method(
    session: AsyncSession, sub: Subscription, ev: ProviderEvent, provider_name: str
) -> None:
    if ev.method is None:
        return
    pm = PaymentMethod(
        workspace_id=sub.workspace_id,
        provider=provider_name,
        provider_token=ev.method.token,
        brand=ev.method.brand,
        last4=ev.method.last4,
    )
    session.add(pm)
    await session.flush()
    sub.payment_method_id = pm.id


async def _payment_succeeded(
    session: AsyncSession,
    sub: Subscription,
    invoice: Invoice,
    ev: ProviderEvent,
    now: datetime,
    provider_name: str,
) -> str:
    if invoice.status == InvoiceStatus.paid:
        return "already_paid"
    if invoice.status == InvoiceStatus.void:
        session.add(
            SystemIssue(
                kind="PAYMENT_FOR_VOID_INVOICE", detail=f"invoice {invoice.id}, event {ev.event_id}"
            )
        )
        return "void_invoice"
    if ev.amount is not None and ev.amount != invoice.amount:
        # Сумма платежа не совпала со счётом: не применяем, пусть разберётся человек.
        session.add(
            SystemIssue(
                kind="PAYMENT_AMOUNT_MISMATCH",
                detail=f"invoice {invoice.id}: expected {invoice.amount}, got {ev.amount}",
            )
        )
        return "amount_mismatch"
    invoice.status = InvoiceStatus.paid
    invoice.paid_at = now
    await _save_method(session, sub, ev, provider_name)
    ws_id = sub.workspace_id

    if invoice.kind in (InvoiceKind.subscription, InvoiceKind.renewal):
        await transition(session, sub, Event.PAYMENT_SUCCEEDED, now)
        sub.plan = Plan.pro
        sub.billing_period = BillingPeriod(invoice.billing_period or sub.billing_period.value)
        sub.seats = invoice.seats or sub.seats
        sub.pending_seats = None
        if sub.pending_period == sub.billing_period:
            sub.pending_period = None
        sub.current_period_start = invoice.period_start
        sub.current_period_end = invoice.period_end
        sub.trial_end = None
        sub.past_due_since = None
        if invoice.kind == InvoiceKind.subscription:
            sub.cancel_at_period_end = False
    elif invoice.kind == InvoiceKind.proration:
        sub.seats = max(sub.seats, invoice.seats or 0)
        invites = (
            await session.scalars(select(Invitation).where(Invitation.invoice_id == invoice.id))
        ).all()
        for inv in invites:
            if inv.revoked_at is None and inv.accepted_at is None:
                ws = await session.get(Workspace, ws_id)
                inviter = await session.get(User, inv.invited_by) if inv.invited_by else None
                assert ws is not None
                await send_invite_email(
                    session, inv, ws, inviter.name if inviter else "Crewboard", now
                )
    elif invoice.kind == InvoiceKind.card_update and sub.status in (
        SubStatus.past_due,
        SubStatus.read_only,
    ):
        pass  # карта обновлена; просроченный счёт оплачивается отдельным действием
    await audit(
        session, ws_id, SYSTEM_ACTOR, "billing", invoice.id, "billing.payment_succeeded",
        now=now, kind=invoice.kind.value, amount=invoice.amount,
    )  # fmt: skip
    return "paid"


async def _payment_failed(
    session: AsyncSession, sub: Subscription, invoice: Invoice, now: datetime
) -> str:
    if invoice.status != InvoiceStatus.open:
        return "ignored_stale_failure"  # поздний отказ по уже оплаченному/закрытому счёту
    invoice.status = InvoiceStatus.failed
    ws_id = sub.workspace_id
    await audit(
        session, ws_id, SYSTEM_ACTOR, "billing", invoice.id, "billing.payment_failed",
        now=now, kind=invoice.kind.value, amount=invoice.amount, attempt=invoice.attempts,
    )  # fmt: skip
    if invoice.kind != InvoiceKind.renewal:
        return "failed"
    first_failure = sub.status != SubStatus.past_due
    if sub.status in (SubStatus.active, SubStatus.trialing, SubStatus.past_due):
        await transition(session, sub, Event.PAYMENT_FAILED, now)
        sub.past_due_since = sub.past_due_since or now
    if first_failure:
        await notify_owner(
            session, ws_id, "Не удалось списать оплату Crewboard",
            f"Списание {fmt_money(invoice.amount)} не прошло. "
            "Мы повторим попытку через 1, 3 и 5 дней. "
            "Обновите карту в разделе «Биллинг», чтобы сохранить доступ.",
            now,
        )  # fmt: skip
        await audit(session, ws_id, SYSTEM_ACTOR, "billing", None, "billing.past_due", now=now)
    return "failed"


# --- Списание, продление, dunning ------------------------------------------------------


async def charge_invoice(
    session: AsyncSession, provider: PaymentProvider, invoice: Invoice, now: datetime
) -> str:
    """Списание с сохранённого метода. Итог придёт вебхуком (или сразу failed)."""
    if invoice.status in (InvoiceStatus.paid, InvoiceStatus.void):
        return "skipped"
    sub = await get_subscription(session, invoice.workspace_id)
    method = (
        await session.get(PaymentMethod, sub.payment_method_id) if sub.payment_method_id else None
    )
    invoice.attempts += 1
    invoice.status = InvoiceStatus.open
    if method is None:
        session.add(SystemIssue(kind="NO_PAYMENT_METHOD", detail=f"invoice {invoice.id}"))
        await _payment_failed(session, sub, invoice, now)
        return "no_method"
    if invoice.kind == InvoiceKind.renewal and invoice.period_start:
        base = f"renewal:{invoice.workspace_id}:{invoice.period_start.isoformat()}"
        key = base if invoice.attempts == 1 else f"{base}:retry{invoice.attempts - 1}"
    else:
        key = f"{invoice.kind.value}:{invoice.id}:{invoice.attempts}"
    result = await provider.charge_saved_method(invoice=invoice, method=method, idempotency_key=key)
    if result.provider_payment_id:
        invoice.provider_payment_id = result.provider_payment_id
    if result.status == "failed":
        await _payment_failed(session, sub, invoice, now)
        return "failed"
    return "pending"


async def renew_subscription(
    session: AsyncSession,
    provider: PaymentProvider,
    workspace_id: int,
    period_end_iso: str,
    now: datetime,
) -> str:
    """RENEW_SUBSCRIPTION: применить pending_seats/pending_period, создать счёт и списать.

    Повтор не создаёт второй счёт: UNIQUE(workspace_id, period_start) для renewal-счетов.
    """
    sub = await get_subscription(session, workspace_id, lock=True)
    if (
        sub.plan != Plan.pro
        or sub.status not in (SubStatus.active, SubStatus.trialing)
        or sub.current_period_end is None
        or sub.current_period_end.isoformat() != period_end_iso
        or sub.cancel_at_period_end
    ):
        return "not_due"
    start = sub.current_period_end
    existing = await session.scalar(
        select(Invoice.id).where(
            Invoice.workspace_id == workspace_id,
            Invoice.kind == InvoiceKind.renewal,
            Invoice.period_start == start,
        )
    )
    if existing is not None:
        return "already_invoiced"
    period = sub.pending_period or sub.billing_period
    seats = max(
        sub.pending_seats if sub.pending_seats is not None else sub.seats,
        await seat_demand(session, workspace_id, now),
    )
    invoice = await create_invoice(
        session,
        workspace_id,
        InvoiceKind.renewal,
        [Line(f"Crewboard Pro: продление, {seats} мест", seats, SEAT_PRICE[period])],
        period_start=start,
        period_end=calc_period_end(start, period),
        billing_period=period.value,
        seats=seats,
    )
    await audit(
        session, workspace_id, SYSTEM_ACTOR, "billing", invoice.id, "billing.renewal_invoiced",
        now=now, amount=invoice.amount, seats=seats,
    )  # fmt: skip
    await charge_invoice(session, provider, invoice, now)
    return "invoiced"


async def downgrade_to_free(
    session: AsyncSession, workspace_id: int, now: datetime, reason: str
) -> int:
    """Конец оплаченного периода: план Free. Данные не удаляются.

    Лишние проекты (кроме 3 последних изменённых) архивируются и становятся read-only;
    лишние участники остаются, но приглашать новых нельзя, пока их больше лимита.
    """
    sub = await get_subscription(session, workspace_id, lock=True)
    await transition(session, sub, Event.DOWNGRADED, now)
    sub.plan = Plan.free
    sub.cancel_at_period_end = False
    sub.pending_seats = None
    sub.pending_period = None
    sub.seats = 0
    sub.trial_end = None
    sub.past_due_since = None
    limit = limits_for(Plan.free).max_projects or 0
    last_touch = func.coalesce(func.max(Task.updated_at), Project.created_at)
    rows = (
        await session.execute(
            select(Project.id, last_touch.label("touched"))
            .outerjoin(Task, Task.project_id == Project.id)
            .where(Project.workspace_id == workspace_id, Project.archived_at.is_(None))
            .group_by(Project.id)
            .order_by(last_touch.desc(), Project.id.desc())
        )
    ).all()
    to_archive = [r[0] for r in rows[limit:]]
    for pid in to_archive:
        project = await session.get(Project, pid)
        assert project is not None
        project.archived_at = now
    await audit(
        session, workspace_id, SYSTEM_ACTOR, "billing", None, "billing.downgraded",
        now=now, reason=reason, archived_projects=to_archive,
    )  # fmt: skip
    await notify_owner(
        session, workspace_id, "Crewboard: воркспейс переведён на Free",
        "Платная подписка закончилась. Данные сохранены; проекты сверх лимита Free "
        "архивированы и доступны только для чтения. Вернуться на Pro можно в разделе «Биллинг».",
        now,
    )  # fmt: skip
    return len(to_archive)


async def scan_renewals(session: AsyncSession, now: datetime) -> dict[str, int]:
    """Раз в минуту: напоминания о конце trial, продления и закрытие периодов."""
    stats = {"reminders": 0, "renewals": 0, "downgrades": 0}
    # 1. Письмо за 3 дня до конца trial.
    soon = (
        await session.scalars(
            select(Subscription).where(
                Subscription.status == SubStatus.trialing,
                Subscription.trial_end.is_not(None),
                Subscription.trial_end - TRIAL_REMINDER_BEFORE <= now,
                Subscription.trial_end > now,
                Subscription.trial_reminder_sent_at.is_(None),
            )
        )
    ).all()
    for sub in soon:
        sub.trial_reminder_sent_at = now
        has_card = sub.payment_method_id is not None
        await notify_owner(
            session, sub.workspace_id, "Пробный период Crewboard Pro заканчивается через 3 дня",
            "Карта привязана — оплата спишется автоматически."
            if has_card
            else "Карта не привязана: без неё воркспейс вернётся на тариф Free. "
            "Привяжите карту в разделе «Биллинг».",
            now,
        )  # fmt: skip
        stats["reminders"] += 1
    # 2. Периоды, которые закончились.
    due = (
        await session.scalars(
            select(Subscription)
            .where(
                Subscription.plan == Plan.pro,
                Subscription.status.in_((SubStatus.active, SubStatus.trialing, SubStatus.past_due)),
                Subscription.current_period_end <= now,
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    for sub in due:
        assert sub.current_period_end is not None
        if sub.cancel_at_period_end:
            await downgrade_to_free(session, sub.workspace_id, now, "canceled")
            stats["downgrades"] += 1
        elif sub.status == SubStatus.past_due:
            continue  # ждёт dunning
        elif sub.status == SubStatus.trialing and sub.payment_method_id is None:
            await downgrade_to_free(session, sub.workspace_id, now, "trial_ended_without_card")
            stats["downgrades"] += 1
        else:
            queued = await enqueue(
                session,
                "RENEW_SUBSCRIPTION",
                {
                    "workspace_id": sub.workspace_id,
                    "period_end": sub.current_period_end.isoformat(),
                },
                now=now,
                dedupe_key=f"renew:{sub.workspace_id}:{sub.current_period_end.isoformat()}",
            )
            stats["renewals"] += int(queued)
    return stats


async def scan_dunning(session: AsyncSession, now: datetime) -> dict[str, int]:
    """Повторные списания через 1, 3 и 5 дней после первой неудачи; через 7 дней — read_only."""
    stats = {"retries": 0, "read_only": 0}
    subs = (
        await session.scalars(
            select(Subscription)
            .where(
                Subscription.status == SubStatus.past_due, Subscription.past_due_since.is_not(None)
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    for sub in subs:
        assert sub.past_due_since is not None
        elapsed = now - sub.past_due_since
        if elapsed >= timedelta(days=READ_ONLY_AFTER_DAYS):
            await transition(session, sub, Event.DUNNING_EXHAUSTED, now)
            await audit(
                session,
                sub.workspace_id,
                SYSTEM_ACTOR,
                "billing",
                None,
                "billing.read_only",
                now=now,
            )
            await notify_owner(
                session, sub.workspace_id, "Crewboard: воркспейс переведён в режим чтения",
                "Оплата не прошла в течение 7 дней. Данные доступны для чтения; "
                "чтобы вернуть редактирование, обновите карту и оплатите счёт в разделе «Биллинг».",
                now,
            )  # fmt: skip
            stats["read_only"] += 1
            continue
        invoice = await outstanding_invoice(session, sub.workspace_id)
        if invoice is None or invoice.status != InvoiceStatus.failed:
            continue
        retries_done = invoice.attempts - 1
        if retries_done < len(DUNNING_RETRY_DAYS) and elapsed >= timedelta(
            days=DUNNING_RETRY_DAYS[retries_done]
        ):
            queued = await enqueue(
                session,
                "CHARGE_INVOICE",
                {"workspace_id": sub.workspace_id, "invoice_id": invoice.id},
                now=now,
                dedupe_key=f"charge:{invoice.id}:{invoice.attempts + 1}",
            )
            stats["retries"] += int(queued)
    return stats


# --- Отмена, места, период -------------------------------------------------------------


async def cancel_subscription(
    session: AsyncSession, workspace_id: int, actor_id: int, now: datetime
) -> Subscription:
    sub = await get_subscription(session, workspace_id, lock=True)
    if sub.plan != Plan.pro:
        raise AppError(409, "not_subscribed", "There is no active subscription")
    if not sub.cancel_at_period_end:
        sub.cancel_at_period_end = True
        await audit(
            session, workspace_id, actor_id, "billing", None, "billing.cancel_requested", now=now
        )
    return sub


async def resume_subscription(
    session: AsyncSession, workspace_id: int, actor_id: int, now: datetime
) -> Subscription:
    sub = await get_subscription(session, workspace_id, lock=True)
    if sub.plan != Plan.pro or not sub.cancel_at_period_end:
        raise AppError(409, "not_canceled", "The subscription is not scheduled for cancellation")
    sub.cancel_at_period_end = False
    await audit(session, workspace_id, actor_id, "billing", None, "billing.cancel_revoked", now=now)
    return sub


async def set_seats(
    session: AsyncSession, workspace_id: int, seats: int, actor_id: int, now: datetime
) -> Subscription:
    """Уменьшение мест — со следующего периода, деньги не возвращаются. Рост — через приглашения."""
    sub = await get_subscription(session, workspace_id, lock=True)
    if sub.plan != Plan.pro:
        raise AppError(409, "not_subscribed", "There is no active subscription")
    demand = await seat_demand(session, workspace_id, now)
    if seats < demand:
        raise AppError(422, "seats_below_usage", f"{demand} seats are in use or reserved")
    if seats > sub.seats:
        raise AppError(422, "seats_increase_via_invites", "Seats grow when you invite people")
    sub.pending_seats = None if seats == sub.seats else seats
    await audit(
        session, workspace_id, actor_id, "billing", None, "billing.seats_scheduled",
        now=now, seats=seats,
    )  # fmt: skip
    return sub


async def set_period(
    session: AsyncSession, workspace_id: int, period: BillingPeriod, actor_id: int, now: datetime
) -> Subscription:
    """Смена месяц ↔ год вступает в силу со следующего периода."""
    sub = await get_subscription(session, workspace_id, lock=True)
    if sub.plan != Plan.pro:
        raise AppError(409, "not_subscribed", "There is no active subscription")
    sub.pending_period = None if period == sub.billing_period else period
    await audit(
        session, workspace_id, actor_id, "billing", None, "billing.period_scheduled",
        now=now, period=period.value,
    )  # fmt: skip
    return sub


def next_charge(sub: Subscription, demand: int) -> tuple[datetime | None, int]:
    """Когда и сколько спишется при продлении (для экрана биллинга)."""
    if sub.plan != Plan.pro or sub.cancel_at_period_end or sub.current_period_end is None:
        return None, 0
    period = sub.pending_period or sub.billing_period
    seats = max(sub.pending_seats if sub.pending_seats is not None else sub.seats, demand)
    return sub.current_period_end, seats * SEAT_PRICE[period]
