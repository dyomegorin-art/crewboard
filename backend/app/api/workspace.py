from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import PERMISSIONS, WorkspaceCtx, require
from app.billing.limits import count_paid_members, get_subscription
from app.billing.seats import shrink_pending_seats
from app.errors import AppError
from app.models import Membership, Plan, Role, SubStatus, User, Workspace
from app.schemas.errors import ErrorResponse
from app.schemas.workspace import (
    MemberOut,
    MemberRoleIn,
    TransferIn,
    WorkspaceDetail,
    WorkspaceUpdateIn,
)
from app.services.audit import audit

router = APIRouter(prefix="/w/{workspace_id}", tags=["workspace"])

_not_found: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Не участник воркспейса"}
}


@router.get("", response_model=WorkspaceDetail, summary="Воркспейс, роль, права и статус подписки")
async def detail(
    ctx: WorkspaceCtx = Depends(require("projects.read")),
) -> WorkspaceDetail:
    ws = await ctx.session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    sub = await get_subscription(ctx.session, ctx.workspace_id)
    return WorkspaceDetail(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        role=ctx.role,
        permissions=sorted(PERMISSIONS[ctx.role]),
        plan=sub.plan.value,
        status=sub.status.value,
        cancel_at_period_end=sub.cancel_at_period_end,
        trial_end=sub.trial_end,
        current_period_end=sub.current_period_end,
        past_due_since=sub.past_due_since,
        seats=sub.seats,
        paid_seats_used=await count_paid_members(ctx.session, ctx.workspace_id),
    )


@router.patch("", response_model=WorkspaceUpdateIn, summary="Переименовать воркспейс")
async def rename(
    body: WorkspaceUpdateIn, ctx: WorkspaceCtx = Depends(require("workspace.update", write=True))
) -> WorkspaceUpdateIn:
    ws = await ctx.session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    old, ws.name = ws.name, body.name
    await audit(
        ctx.session, ws.id, ctx.user.id, "workspace", ws.id, "workspace.renamed",
        now=await ctx.now_.now(), old=old, new=body.name,
    )  # fmt: skip
    await ctx.session.commit()
    return body


@router.delete(
    "",
    status_code=204,
    summary="Удалить воркспейс (owner)",
    responses={409: {"model": ErrorResponse, "description": "Сначала отмените подписку"}},
)
async def delete_workspace(ctx: WorkspaceCtx = Depends(require("workspace.delete"))) -> None:
    sub = await get_subscription(ctx.session, ctx.workspace_id)
    if sub.plan == Plan.pro and sub.status != SubStatus.canceled:
        raise AppError(409, "subscription_active", "Cancel the subscription first")
    ws = await ctx.session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    await ctx.session.delete(ws)
    await ctx.session.commit()


@router.get("/members", response_model=list[MemberOut], summary="Участники")
async def list_members(ctx: WorkspaceCtx = Depends(require("members.view"))) -> list[MemberOut]:
    ws = await ctx.session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    rows = (
        await ctx.session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.workspace_id == ctx.workspace_id)
            .order_by(Membership.created_at, User.id)
        )
    ).all()
    return [
        MemberOut(
            user_id=u.id,
            name=u.name,
            email=u.email,
            role=m.role,
            is_owner=u.id == ws.owner_id,
            created_at=m.created_at,
        )
        for m, u in rows
    ]


async def _target(ctx: WorkspaceCtx, user_id: int) -> Membership:
    m = await ctx.session.get(Membership, (ctx.workspace_id, user_id))
    if m is None:
        raise AppError(404, "member_not_found", "Member not found")
    return m


@router.patch(
    "/members/{user_id}",
    response_model=MemberOut,
    summary="Сменить роль участника",
    responses={**_not_found, 422: {"model": ErrorResponse}},
)
async def change_role(
    user_id: int,
    body: MemberRoleIn,
    ctx: WorkspaceCtx = Depends(require("members.manage", write=True)),
) -> MemberOut:
    target = await _target(ctx, user_id)
    if target.role == Role.owner or body.role == Role.owner:
        raise AppError(403, "forbidden", "Use ownership transfer to change the owner")
    if (target.role == Role.guest) != (body.role == Role.guest):
        # Гость не занимает место; переход требует доплаты — делается через новое приглашение.
        raise AppError(
            422, "role_change_not_supported", "Invite the user again to switch to or from guest"
        )
    old = target.role
    target.role = body.role
    user = await ctx.session.get(User, user_id)
    assert user is not None
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "member", user_id, "member.role_changed",
        now=await ctx.now_.now(), old=old.value, new=body.role.value,
    )  # fmt: skip
    await ctx.session.commit()
    return MemberOut(
        user_id=user.id,
        name=user.name,
        email=user.email,
        role=target.role,
        is_owner=False,
        created_at=target.created_at,
    )


@router.delete("/members/{user_id}", status_code=204, summary="Удалить участника или выйти")
async def remove_member(
    user_id: int, ctx: WorkspaceCtx = Depends(require("projects.read", write=True))
) -> None:
    """Выйти может любой не-owner; удалять других — только с правом members.manage."""
    now = await ctx.now_.now()
    target = await _target(ctx, user_id)
    if target.role == Role.owner:
        raise AppError(409, "cannot_remove_owner", "Transfer ownership first")
    if user_id != ctx.user.id:
        if "members.manage" not in PERMISSIONS[ctx.role]:
            await audit(
                ctx.session, ctx.workspace_id, ctx.user.id, "permission", None, "access_denied",
                now=now, permission="members.manage", role=ctx.role.value,
            )  # fmt: skip
            await ctx.session.commit()
            raise AppError(403, "forbidden", "Insufficient permissions")
    # Задачи остаются без исполнителя (ON DELETE SET NULL (assignee_id)), место освобождается
    # со следующего периода.
    await ctx.session.delete(target)
    await ctx.session.flush()
    sub = await get_subscription(ctx.session, ctx.workspace_id, lock=True)
    await shrink_pending_seats(ctx.session, sub, now)
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "member", user_id, "member.removed",
        now=now, role=target.role.value,
    )  # fmt: skip
    await ctx.session.commit()


@router.post(
    "/ownership-transfer",
    status_code=204,
    summary="Передать владение (owner)",
    responses={**_not_found, 422: {"model": ErrorResponse}},
)
async def transfer_ownership(
    body: TransferIn, ctx: WorkspaceCtx = Depends(require("workspace.transfer", write=True))
) -> None:
    target = await _target(ctx, body.user_id)
    if target.role == Role.guest:
        raise AppError(422, "invalid_new_owner", "A guest cannot become the owner")
    if target.user_id == ctx.user.id:
        raise AppError(422, "invalid_new_owner", "You already own this workspace")
    ws = await ctx.session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    ws.owner_id = target.user_id
    target.role = Role.owner
    ctx.membership.role = Role.admin
    await audit(
        ctx.session, ws.id, ctx.user.id, "workspace", ws.id, "workspace.ownership_transferred",
        now=await ctx.now_.now(), to=target.user_id,
    )  # fmt: skip
    await ctx.session.commit()
