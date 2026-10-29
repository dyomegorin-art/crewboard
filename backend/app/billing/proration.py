import calendar
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.models import BillingPeriod


def add_months(dt: datetime, months: int) -> datetime:
    """Календарное сложение месяцев; 31 января + 1 месяц = 28/29 февраля."""
    index = dt.month - 1 + months
    year, month = dt.year + index // 12, index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def period_end(start: datetime, period: BillingPeriod) -> datetime:
    return add_months(start, 1 if period == BillingPeriod.month else 12)


def prorate(price: int, remaining: timedelta, total: timedelta) -> int:
    """Доплата за место: цена × оставшиеся секунды / секунды периода, копейки, ROUND_HALF_UP.

    Только Decimal и целые копейки, никаких float. Время вне периода зажимается в [0, total].
    """
    total_s = int(total.total_seconds())
    if total_s <= 0:
        return 0
    remaining_s = min(max(int(remaining.total_seconds()), 0), total_s)
    amount = Decimal(price) * Decimal(remaining_s) / Decimal(total_s)
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))
