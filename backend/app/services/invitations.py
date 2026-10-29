from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Invitation, InvoiceStatus, Workspace
from app.models import Invoice as InvoiceModel
from app.outbox import enqueue
from app.schemas.workspace import InvitationOut
from app.security import new_opaque_token, token_hash

INVITE_TTL = timedelta(days=7)


def invite_status(inv: Invitation, invoice: InvoiceModel | None, now: datetime) -> str:
    if inv.accepted_at is not None:
        return "accepted"
    if inv.revoked_at is not None:
        return "revoked"
    if inv.expires_at <= now:
        return "expired"
    if invoice is not None and invoice.status != InvoiceStatus.paid:
        return "awaiting_payment"
    return "pending"


def invitation_out(inv: Invitation, invoice: InvoiceModel | None, now: datetime) -> InvitationOut:
    return InvitationOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        status=invite_status(inv, invoice, now),
        expires_at=inv.expires_at,
        invoice_id=inv.invoice_id,
        amount=invoice.amount if invoice else None,
        created_at=inv.created_at,
    )


async def send_invite_email(
    session: AsyncSession, inv: Invitation, workspace: Workspace, inviter_name: str, now: datetime
) -> None:
    """Выпускает новый токен (старый нигде не хранится в открытом виде) и ставит письмо в outbox."""
    token = new_opaque_token()
    inv.token_hash = token_hash(token)
    inv.expires_at = now + INVITE_TTL
    link = f"{get_settings().public_url}/invite/{token}"
    await enqueue(
        session,
        "SEND_EMAIL",
        {
            "to": inv.email,
            "subject": f"Приглашение в «{workspace.name}» на Crewboard",
            "body": f"{inviter_name} приглашает вас в «{workspace.name}» ({inv.role.value}).\n"
            f"Принять приглашение (ссылка действует 7 дней): {link}\n",
        },
        now=now,
    )
