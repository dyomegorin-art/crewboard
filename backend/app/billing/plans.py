"""Тарифы и лимиты — единственное место, где они описаны. Проверки живут в billing/limits.py."""

from dataclasses import dataclass
from datetime import timedelta

from app.models import BillingPeriod, Plan

CURRENCY = "RUB"
TRIAL_DAYS = 14
TRIAL_REMINDER_BEFORE = timedelta(days=3)
# Цена места, копейки.
SEAT_PRICE: dict[BillingPeriod, int] = {
    BillingPeriod.month: 490_00,
    BillingPeriod.year: 4_900_00,
}
# Проверочный платёж для привязки/смены карты (в реальности возвращается), копейки.
CARD_UPDATE_AMOUNT = 100

# Dunning: повторные списания через N дней после первой неудачи; через 7 дней — read_only.
DUNNING_RETRY_DAYS = (1, 3, 5)
READ_ONLY_AFTER_DAYS = 7

DEFAULT_STATUSES: tuple[tuple[str, bool], ...] = (
    ("To do", False),
    ("In progress", False),
    ("Done", True),
)


@dataclass(frozen=True)
class PlanLimits:
    max_members: int | None  # owner + admin + member; None — без лимита
    max_projects: int | None  # активных
    guests: bool
    custom_statuses: bool
    activity_days: int | None  # None — полная история
    csv_export: bool


PLANS: dict[Plan, PlanLimits] = {
    Plan.free: PlanLimits(
        max_members=3,
        max_projects=3,
        guests=False,
        custom_statuses=False,
        activity_days=7,
        csv_export=False,
    ),
    Plan.pro: PlanLimits(
        max_members=None,
        max_projects=None,
        guests=True,
        custom_statuses=True,
        activity_days=None,
        csv_export=True,
    ),
}


def limits_for(plan: Plan) -> PlanLimits:
    return PLANS[plan]
