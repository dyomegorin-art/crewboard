from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.plans import CURRENCY
from app.models import Invoice, InvoiceKind, InvoiceLine, InvoiceStatus


@dataclass
class Line:
    description: str
    quantity: int
    unit_amount: int

    @property
    def amount(self) -> int:
        return self.quantity * self.unit_amount


async def create_invoice(
    session: AsyncSession,
    workspace_id: int,
    kind: InvoiceKind,
    lines: list[Line],
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    billing_period: str | None = None,
    seats: int | None = None,
) -> Invoice:
    inv = Invoice(
        workspace_id=workspace_id,
        kind=kind,
        amount=sum(line.amount for line in lines),
        currency=CURRENCY,
        status=InvoiceStatus.open,
        period_start=period_start,
        period_end=period_end,
        billing_period=billing_period,
        seats=seats,
    )
    session.add(inv)
    await session.flush()
    for line in lines:
        session.add(
            InvoiceLine(
                invoice_id=inv.id,
                description=line.description,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                amount=line.amount,
            )
        )
    await session.flush()
    return inv
