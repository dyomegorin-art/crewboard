"""Автомат статусов подписки: единственное место, где меняется `subscription.status`.

Допустимые переходы заданы явной таблицей; всё остальное — `InvalidTransition`
(вызывающий код пишет `SystemIssue`). Тарифный план (`plan`) меняют сервисные функции рядом.
"""

import enum
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Subscription, SubStatus, SystemIssue


class Event(enum.StrEnum):
    TRIAL_STARTED = "trial_started"
    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_FAILED = "payment_failed"
    DUNNING_EXHAUSTED = "dunning_exhausted"
    DOWNGRADED = "downgraded"


S = SubStatus
TRANSITIONS: dict[tuple[SubStatus, Event], SubStatus] = {
    # Старт trial: с Free (active) или после прошлой отмены (canceled).
    (S.active, Event.TRIAL_STARTED): S.trialing,
    (S.canceled, Event.TRIAL_STARTED): S.trialing,
    # Успешная оплата (апгрейд, продление, повторное списание) всегда ведёт в active.
    (S.active, Event.PAYMENT_SUCCEEDED): S.active,
    (S.canceled, Event.PAYMENT_SUCCEEDED): S.active,
    (S.trialing, Event.PAYMENT_SUCCEEDED): S.active,
    (S.past_due, Event.PAYMENT_SUCCEEDED): S.active,
    (S.read_only, Event.PAYMENT_SUCCEEDED): S.active,
    # Неудачное продление; повторная неудача в past_due статус не меняет.
    (S.active, Event.PAYMENT_FAILED): S.past_due,
    (S.trialing, Event.PAYMENT_FAILED): S.past_due,
    (S.past_due, Event.PAYMENT_FAILED): S.past_due,
    # Dunning исчерпан → только чтение.
    (S.past_due, Event.DUNNING_EXHAUSTED): S.read_only,
    # Конец оплаченного периода после отмены / trial без карты.
    (S.trialing, Event.DOWNGRADED): S.canceled,
    (S.active, Event.DOWNGRADED): S.canceled,
    (S.past_due, Event.DOWNGRADED): S.canceled,
    (S.read_only, Event.DOWNGRADED): S.canceled,
}


class InvalidTransition(Exception):
    def __init__(self, status: SubStatus, event: Event) -> None:
        super().__init__(f"{status.value} --{event.value}--> ?")
        self.status = status
        self.event = event


def next_status(status: SubStatus, event: Event) -> SubStatus:
    try:
        return TRANSITIONS[(status, event)]
    except KeyError:
        raise InvalidTransition(status, event) from None


async def transition(
    session: AsyncSession, sub: Subscription, event: Event, now: datetime
) -> SubStatus:
    """Применяет событие к подписке. Недопустимый переход → SystemIssue + исключение."""
    try:
        new = next_status(sub.status, event)
    except InvalidTransition as exc:
        session.add(
            SystemIssue(
                kind="INVALID_SUBSCRIPTION_TRANSITION",
                detail=f"workspace {sub.workspace_id}: {exc}",
            )
        )
        await session.flush()
        raise
    sub.status = new
    sub.updated_at = now
    return new
