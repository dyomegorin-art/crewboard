from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.plans import PLANS, SEAT_PRICE
from app.billing.proration import add_months, period_end, prorate
from app.billing.state import TRANSITIONS, Event, InvalidTransition, next_status, transition
from app.models import BillingPeriod, Plan, Subscription, SubStatus, SystemIssue

MONTH = timedelta(days=30)
YEAR = timedelta(days=365)


def test_prorate_full_period_costs_full_price() -> None:
    assert prorate(49000, MONTH, MONTH) == 49000
    assert prorate(490000, YEAR, YEAR) == 490000


def test_prorate_first_and_last_second_of_period() -> None:
    first = prorate(49000, MONTH - timedelta(seconds=1), MONTH)
    assert first == 49000  # 48999.98 → округление до копейки вверх
    assert prorate(49000, timedelta(seconds=1), MONTH) == 0  # 0.019 коп. → 0
    assert prorate(49000, timedelta(0), MONTH) == 0


def test_prorate_half_period_and_year() -> None:
    assert prorate(49000, MONTH / 2, MONTH) == 24500
    assert prorate(490000, YEAR / 2, YEAR) in (245000,)
    assert prorate(490000, timedelta(days=73), YEAR) == 98000  # ровно 1/5 года


@pytest.mark.parametrize(
    ("price", "rem", "total", "expected"),
    [
        (1, 1, 2, 1),  # 0.5 → вверх
        (3, 1, 2, 2),  # 1.5 → вверх
        (1, 1, 3, 0),  # 0.33 → вниз
        (2, 2, 3, 1),  # 1.33 → вниз
        (5, 1, 2, 3),  # 2.5 → вверх
        (7, 1, 4, 2),  # 1.75 → вверх
    ],
)
def test_prorate_rounds_half_up(price: int, rem: int, total: int, expected: int) -> None:
    assert prorate(price, timedelta(seconds=rem), timedelta(seconds=total)) == expected


def test_prorate_clamps_out_of_range_values() -> None:
    assert prorate(49000, MONTH * 2, MONTH) == 49000
    assert prorate(49000, -MONTH, MONTH) == 0
    assert prorate(49000, MONTH, timedelta(0)) == 0


def test_prorate_uses_integer_kopecks_only() -> None:
    assert isinstance(prorate(49000, timedelta(seconds=12345), MONTH), int)


def test_add_months_clamps_day_and_handles_leap_years() -> None:
    jan31 = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)
    assert add_months(jan31, 1) == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)
    assert add_months(datetime(2028, 1, 31, tzinfo=UTC), 1).day == 29
    assert add_months(datetime(2026, 12, 15, tzinfo=UTC), 1) == datetime(2027, 1, 15, tzinfo=UTC)
    assert period_end(datetime(2028, 2, 29, tzinfo=UTC), BillingPeriod.year) == datetime(
        2029, 2, 28, tzinfo=UTC
    )


def test_plan_table_is_single_source_of_truth() -> None:
    assert SEAT_PRICE[BillingPeriod.month] == 490_00 and SEAT_PRICE[BillingPeriod.year] == 4900_00
    free, pro = PLANS[Plan.free], PLANS[Plan.pro]
    assert (free.max_members, free.max_projects, free.activity_days) == (3, 3, 7)
    assert not free.guests and not free.custom_statuses and not free.csv_export
    assert pro.max_members is None and pro.max_projects is None and pro.activity_days is None
    assert pro.guests and pro.custom_statuses and pro.csv_export


# --- Автомат статусов --------------------------------------------------------------------


@pytest.mark.parametrize(("key", "target"), list(TRANSITIONS.items()))
def test_every_listed_transition_is_allowed(
    key: tuple[SubStatus, Event], target: SubStatus
) -> None:
    assert next_status(*key) == target


def test_every_unlisted_transition_is_rejected() -> None:
    for status in SubStatus:
        for event in Event:
            if (status, event) in TRANSITIONS:
                continue
            with pytest.raises(InvalidTransition):
                next_status(status, event)


def test_transition_table_key_facts() -> None:
    assert next_status(SubStatus.active, Event.PAYMENT_FAILED) == SubStatus.past_due
    assert next_status(SubStatus.past_due, Event.DUNNING_EXHAUSTED) == SubStatus.read_only
    assert next_status(SubStatus.read_only, Event.PAYMENT_SUCCEEDED) == SubStatus.active
    assert next_status(SubStatus.trialing, Event.DOWNGRADED) == SubStatus.canceled
    # read_only выходит только через оплату или даунгрейд.
    exits = {e for (s, e) in TRANSITIONS if s == SubStatus.read_only}
    assert exits == {Event.PAYMENT_SUCCEEDED, Event.DOWNGRADED}
    assert (SubStatus.active, Event.DUNNING_EXHAUSTED) not in TRANSITIONS
    assert (SubStatus.canceled, Event.PAYMENT_FAILED) not in TRANSITIONS


async def test_invalid_transition_records_system_issue(session: AsyncSession, factory) -> None:  # type: ignore[no-untyped-def]
    ws, _, _ = await factory.workspace()
    sub = await session.get(Subscription, ws.id)
    assert sub is not None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(InvalidTransition):
        await transition(session, sub, Event.DUNNING_EXHAUSTED, now)  # active → read_only нельзя
    await session.commit()
    from sqlalchemy import select

    issue = await session.scalar(select(SystemIssue))
    assert issue is not None and issue.kind == "INVALID_SUBSCRIPTION_TRANSITION"
    assert sub.status == SubStatus.active  # состояние не изменилось
