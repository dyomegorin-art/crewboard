from datetime import datetime

from pydantic import BaseModel, Field

from app.models import BillingPeriod


class CardOut(BaseModel):
    brand: str
    last4: str


class BillingOut(BaseModel):
    plan: str
    status: str
    billing_period: BillingPeriod
    pending_period: BillingPeriod | None
    seats_paid: int
    seats_used: int
    seats_reserved: int
    pending_seats: int | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_end: datetime | None
    cancel_at_period_end: bool
    past_due_since: datetime | None
    next_charge_at: datetime | None
    next_charge_amount: int
    card: CardOut | None
    outstanding_invoice_id: int | None
    seat_price_month: int
    seat_price_year: int
    currency: str
    trial_available: bool
    provider: str


class InvoiceLineOut(BaseModel):
    description: str
    quantity: int
    unit_amount: int
    amount: int


class InvoiceOut(BaseModel):
    id: int
    kind: str
    amount: int
    currency: str
    status: str
    period_start: datetime | None
    period_end: datetime | None
    created_at: datetime
    paid_at: datetime | None
    lines: list[InvoiceLineOut]


class InvoiceStatusOut(BaseModel):
    invoice_id: int
    invoice_status: str
    subscription_status: str
    plan: str


class SubscribeIn(BaseModel):
    period: BillingPeriod = BillingPeriod.month
    return_url: str | None = None


class CheckoutOut(BaseModel):
    checkout_url: str
    invoice_id: int


class ReturnUrlIn(BaseModel):
    return_url: str | None = None


class SeatsIn(BaseModel):
    seats: int = Field(ge=1, le=10000)


class PeriodIn(BaseModel):
    period: BillingPeriod


class MockCheckoutOut(BaseModel):
    session_id: str
    invoice_id: int
    amount: int
    currency: str
    status: str
    kind: str
    workspace_name: str
    slug: str
    lines: list[InvoiceLineOut]


class MockPayIn(BaseModel):
    card_number: str = Field(min_length=1, max_length=32)
    cvc: str | None = None
    expiry: str | None = None


class AdvanceClockIn(BaseModel):
    seconds: int = Field(default=0, ge=0)
    days: int = Field(default=0, ge=0)
    hours: int = Field(default=0, ge=0)
