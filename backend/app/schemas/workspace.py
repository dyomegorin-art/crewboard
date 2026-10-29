from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models import Role


class WorkspaceDetail(BaseModel):
    id: int
    name: str
    slug: str
    role: Role
    permissions: list[str]
    plan: str
    status: str
    cancel_at_period_end: bool
    trial_end: datetime | None
    current_period_end: datetime | None
    past_due_since: datetime | None
    seats: int
    paid_seats_used: int


class WorkspaceUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MemberOut(BaseModel):
    user_id: int
    name: str
    email: str
    role: Role
    is_owner: bool
    created_at: datetime


class MemberRoleIn(BaseModel):
    role: Role


class TransferIn(BaseModel):
    user_id: int


class InviteIn(BaseModel):
    email: EmailStr
    role: Role = Role.member
    confirm_amount: int | None = None


class InvitePreviewOut(BaseModel):
    requires_payment: bool
    amount: int
    currency: str
    seats_after: int


class InvitationOut(BaseModel):
    id: int
    email: str
    role: Role
    status: str  # pending | awaiting_payment | accepted | revoked | expired
    expires_at: datetime
    invoice_id: int | None
    amount: int | None
    created_at: datetime


class InvitePublicOut(BaseModel):
    workspace_name: str
    email: str
    role: Role
    status: str
