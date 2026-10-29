from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceCtx, get_current_user, require
from app.billing.limits import check_can_add_member, get_subscription
from app.billing.plans import CURRENCY
from app.billing.seats import SeatQuote, quote_seat, reserve_seat
from app.clock import Clock, get_clock
from app.db import get_session
from app.errors import AppError
from app.models import (
    PAID_ROLES,
    Invitation,
    Invoice,
    InvoiceStatus,
    Membership,
    Role,
    User,
    Workspace,
)
from app.schemas.errors import ErrorResponse
from app.schemas.workspace import (
    InvitationOut,
    InviteIn,
    InvitePreviewOut,
    InvitePublicOut,
)
from app.security import new_opaque_token, token_hash
from app.services.audit import audit
from app.services.invitations import (
    INVITE_TTL,
    invitation_out,
    invite_status,
    send_invite_email,
)

router = APIRouter(tags=["invitations"])
ws_router = APIRouter(prefix="/w/{workspace_id}/invitations", tags=["invitations"])


def _ensure_verified(ctx: WorkspaceCtx) -> None:
    if ctx.user.email_verified_at is None:
        raise AppError(403, "email_not_verified", "Confirm your email before inviting people")


def _check_invitable(role: Role) -> None:
    if role == Role.owner:
        raise AppError(422, "invalid_role", "Owner cannot be invited; transfer ownership instead")


async def _already_member(session: AsyncSession, workspace_id: int, email: str) -> bool:
    user = await session.scalar(select(User).where(User.email == email.lower()))
    return user is not None and await session.get(Membership, (workspace_id, user.id)) is not None


@ws_router.post(
    "/preview",
    response_model=InvitePreviewOut,
    summary="Предпросмотр доплаты за место перед приглашением",
    responses={402: {"model": ErrorResponse, "description": "Лимит тарифа"}},
)
async def preview(
    body: InviteIn, ctx: WorkspaceCtx = Depends(require("members.invite"))
) -> InvitePreviewOut:
    now = await ctx.now_.now()
    _check_invitable(body.role)
    sub = await get_subscription(ctx.session, ctx.workspace_id)
    await check_can_add_member(ctx.session, sub, body.role, now)
    quote = await quote_seat(ctx.session, sub, body.role, now)
    return InvitePreviewOut(
        requires_payment=quote.requires_payment,
        amount=quote.amount,
        currency=CURRENCY,
        seats_after=quote.target_seats,
    )


@ws_router.post(
    "",
    response_model=InvitationOut,
    status_code=201,
    summary="Создать приглашение (в Pro — с доплатой за место)",
    responses={
        402: {"model": ErrorResponse, "description": "Лимит тарифа / воркспейс read_only"},
        409: {"model": ErrorResponse, "description": "Нужно подтвердить сумму доплаты"},
    },
)
async def create_invitation(
    body: InviteIn, ctx: WorkspaceCtx = Depends(require("members.invite", write=True))
) -> InvitationOut:
    _ensure_verified(ctx)
    _check_invitable(body.role)
    now = await ctx.now_.now()
    email = body.email.lower()
    session = ctx.session
    if await _already_member(session, ctx.workspace_id, email):
        raise AppError(409, "already_member", "This user is already a member")

    # Блокировка строки подписки сериализует параллельные приглашения: каждое видит
    # места, уже зарезервированные предыдущими, и не считает одно место дважды.
    sub = await get_subscription(session, ctx.workspace_id, lock=True)
    existing = await session.scalar(
        select(Invitation).where(
            Invitation.workspace_id == ctx.workspace_id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > now,
        )
    )
    if existing is not None:
        raise AppError(409, "already_invited", "An active invitation for this email exists")
    await check_can_add_member(session, sub, body.role, now)
    quote = await quote_seat(session, sub, body.role, now)
    if quote.requires_payment and body.confirm_amount != quote.amount:
        raise _confirmation_required(quote)

    invoice = await reserve_seat(session, sub, quote, now)
    inv = Invitation(
        workspace_id=ctx.workspace_id,
        email=email,
        role=body.role,
        token_hash=token_hash(new_opaque_token()),
        invited_by=ctx.user.id,
        expires_at=now + INVITE_TTL,
        invoice_id=invoice.id if invoice else None,
    )
    session.add(inv)
    await session.flush()
    ws = await session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    if invoice is None:
        await send_invite_email(session, inv, ws, ctx.user.name, now)
    await audit(
        session, ctx.workspace_id, ctx.user.id, "invitation", inv.id, "invitation.created",
        now=now, email=email, role=body.role.value, amount=quote.amount,
    )  # fmt: skip
    await session.commit()
    return invitation_out(inv, invoice, now)


def _confirmation_required(quote: SeatQuote) -> AppError:
    err = AppError(
        409,
        "payment_confirmation_required",
        f"Confirm the surcharge of {quote.amount} kopecks for the seat",
    )
    err.details = {"amount": quote.amount, "currency": CURRENCY}
    return err


@ws_router.get("", response_model=list[InvitationOut], summary="Приглашения воркспейса")
async def list_invitations(
    ctx: WorkspaceCtx = Depends(require("members.invite")),
) -> list[InvitationOut]:
    now = await ctx.now_.now()
    rows = (
        await ctx.session.execute(
            select(Invitation, Invoice)
            .outerjoin(Invoice, Invoice.id == Invitation.invoice_id)
            .where(Invitation.workspace_id == ctx.workspace_id)
            .order_by(Invitation.id.desc())
        )
    ).all()
    return [invitation_out(i, inv, now) for i, inv in rows]


async def _get_invitation(
    ctx: WorkspaceCtx, invitation_id: int
) -> tuple[Invitation, Invoice | None]:
    inv = await ctx.session.get(Invitation, invitation_id)
    if inv is None or inv.workspace_id != ctx.workspace_id:
        raise AppError(404, "invitation_not_found", "Invitation not found")
    invoice = await ctx.session.get(Invoice, inv.invoice_id) if inv.invoice_id else None
    return inv, invoice


@ws_router.post("/{invitation_id}/revoke", status_code=204, summary="Отозвать приглашение")
async def revoke(
    invitation_id: int, ctx: WorkspaceCtx = Depends(require("members.invite", write=True))
) -> None:
    now = await ctx.now_.now()
    inv, invoice = await _get_invitation(ctx, invitation_id)
    if inv.accepted_at is not None:
        raise AppError(409, "invitation_accepted", "Invitation is already accepted")
    inv.revoked_at = inv.revoked_at or now
    if invoice is not None and invoice.status in (InvoiceStatus.open, InvoiceStatus.failed):
        invoice.status = InvoiceStatus.void  # доплата за место больше не нужна
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "invitation", inv.id, "invitation.revoked",
        now=now, email=inv.email,
    )  # fmt: skip
    await ctx.session.commit()


@ws_router.post(
    "/{invitation_id}/resend",
    response_model=InvitationOut,
    summary="Отправить приглашение повторно (новый токен и срок)",
    responses={409: {"model": ErrorResponse, "description": "Ожидает оплаты места"}},
)
async def resend(
    invitation_id: int, ctx: WorkspaceCtx = Depends(require("members.invite", write=True))
) -> InvitationOut:
    _ensure_verified(ctx)
    now = await ctx.now_.now()
    inv, invoice = await _get_invitation(ctx, invitation_id)
    status = invite_status(inv, invoice, now)
    if status == "awaiting_payment":
        raise AppError(409, "invitation_awaiting_payment", "The seat is not paid yet")
    if status in ("accepted", "revoked"):
        raise AppError(409, f"invitation_{status}", f"Invitation is {status}")
    ws = await ctx.session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    await send_invite_email(ctx.session, inv, ws, ctx.user.name, now)
    await ctx.session.commit()
    return invitation_out(inv, invoice, now)


# --- Принятие приглашения по ссылке ---------------------------------------------------


async def _by_token(session: AsyncSession, token: str) -> tuple[Invitation, Invoice | None]:
    inv = await session.scalar(select(Invitation).where(Invitation.token_hash == token_hash(token)))
    if inv is None:
        raise AppError(404, "invitation_not_found", "Invitation not found")
    invoice = await session.get(Invoice, inv.invoice_id) if inv.invoice_id else None
    return inv, invoice


@router.get(
    "/invitations/{token}",
    response_model=InvitePublicOut,
    summary="Данные приглашения по токену из письма",
)
async def public_invitation(
    token: str,
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> InvitePublicOut:
    inv, invoice = await _by_token(session, token)
    ws = await session.get(Workspace, inv.workspace_id)
    assert ws is not None
    return InvitePublicOut(
        workspace_name=ws.name,
        email=inv.email,
        role=inv.role,
        status=invite_status(inv, invoice, await clock.now()),
    )


@router.post(
    "/invitations/{token}/accept",
    summary="Принять приглашение (только залогиненный пользователь с тем же email)",
    responses={
        403: {"model": ErrorResponse, "description": "Приглашение выписано на другой email"},
        410: {"model": ErrorResponse, "description": "Приглашение истекло или отозвано"},
    },
)
async def accept(
    token: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, object]:
    now = await clock.now()
    inv, invoice = await _by_token(session, token)
    if inv.email.lower() != user.email.lower():
        raise AppError(
            403, "invitation_email_mismatch", "This invitation was sent to another email"
        )
    status = invite_status(inv, invoice, now)
    if status in ("revoked", "expired"):
        raise AppError(410, f"invitation_{status}", f"Invitation is {status}")
    if status == "awaiting_payment":
        raise AppError(409, "invitation_awaiting_payment", "The seat is not paid yet")
    ws = await session.get(Workspace, inv.workspace_id)
    assert ws is not None
    already = await session.get(Membership, (inv.workspace_id, user.id))
    if status == "accepted" or already is not None:
        raise AppError(409, "already_member", "You are already a member")

    sub = await get_subscription(session, inv.workspace_id, lock=True)
    if inv.role in PAID_ROLES:
        # Место зарезервировано при создании, но воркспейс могли понизить до Free.
        inv.accepted_at = now  # временно исключаем из счётчика активных приглашений
        await check_can_add_member(session, sub, inv.role, now)
    inv.accepted_at = now
    session.add(Membership(workspace_id=inv.workspace_id, user_id=user.id, role=inv.role))
    # Получить письмо на этот адрес и пройти по ссылке — значит владеть ящиком.
    user.email_verified_at = user.email_verified_at or now
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError(409, "already_member", "You are already a member") from None
    await audit(
        session, inv.workspace_id, user.id, "member", user.id, "member.joined",
        now=now, role=inv.role.value,
    )  # fmt: skip
    await session.commit()
    return {"workspace_id": ws.id, "slug": ws.slug}
