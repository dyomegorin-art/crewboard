import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Plan(enum.StrEnum):
    free = "free"
    pro = "pro"


class SubStatus(enum.StrEnum):
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    read_only = "read_only"
    canceled = "canceled"


class BillingPeriod(enum.StrEnum):
    month = "month"
    year = "year"


class InvoiceKind(enum.StrEnum):
    subscription = "subscription"
    proration = "proration"
    renewal = "renewal"
    card_update = "card_update"  # проверочный платёж для смены карты (в trial/active)


class InvoiceStatus(enum.StrEnum):
    draft = "draft"
    open = "open"
    paid = "paid"
    failed = "failed"
    void = "void"


class Subscription(Base):
    __tablename__ = "subscriptions"

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    plan: Mapped[Plan] = mapped_column(Enum(Plan, native_enum=False, length=16))
    status: Mapped[SubStatus] = mapped_column(Enum(SubStatus, native_enum=False, length=16))
    billing_period: Mapped[BillingPeriod] = mapped_column(
        Enum(BillingPeriod, native_enum=False, length=16), default=BillingPeriod.month
    )
    seats: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pending_seats: Mapped[int | None] = mapped_column(Integer)
    # Смена периода (месяц ↔ год) вступает в силу со следующего продления.
    pending_period: Mapped[BillingPeriod | None] = mapped_column(
        Enum(BillingPeriod, native_enum=False, length=16)
    )
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL")
    )
    past_due_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_subscriptions_period_end", "current_period_end"),
        Index("ix_subscriptions_status", "status"),
    )


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_token: Mapped[str] = mapped_column(String(255))
    brand: Mapped[str] = mapped_column(String(32))
    last4: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        # Продление за период создаётся один раз, даже если cron сработал дважды.
        Index(
            "uq_invoices_renewal_period",
            "workspace_id",
            "period_start",
            unique=True,
            postgresql_where=text("kind = 'renewal'"),
        ),
        Index("ix_invoices_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    kind: Mapped[InvoiceKind] = mapped_column(Enum(InvoiceKind, native_enum=False, length=16))
    amount: Mapped[int] = mapped_column(BigInteger)  # копейки
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, native_enum=False, length=16), default=InvoiceStatus.open
    )
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Параметры, которые применяются при оплате: период подписки и число мест после неё.
    billing_period: Mapped[str | None] = mapped_column(String(16))
    seats: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_amount: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[int] = mapped_column(BigInteger)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_webhook_events_provider_event"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_event_id: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawWebhookDelivery(Base):
    __tablename__ = "raw_webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    raw_body: Mapped[bytes] = mapped_column(LargeBinary)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    parse_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    event_id: Mapped[int | None] = mapped_column(ForeignKey("webhook_events.id"))


class OutboxCommand(Base):
    __tablename__ = "outbox_commands"
    __table_args__ = (Index("ix_outbox_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    command: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Ключ дедупликации: повторная постановка той же команды (renewal, retry) не создаёт вторую.
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemIssue(Base):
    __tablename__ = "system_issues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text)
    command_id: Mapped[int | None] = mapped_column(ForeignKey("outbox_commands.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
