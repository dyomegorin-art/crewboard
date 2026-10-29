from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.clock import Clock, get_clock
from app.db import get_session
from app.models import Membership, Role, Subscription, User, Workspace
from app.services.workspaces import create_workspace

router = APIRouter(tags=["workspaces"])


class WorkspaceOut(BaseModel):
    id: int
    name: str
    slug: str
    role: Role
    plan: str
    status: str


class WorkspaceCreateIn(BaseModel):
    name: str


async def _out(session: AsyncSession, ws: Workspace, role: Role) -> WorkspaceOut:
    sub = await session.get(Subscription, ws.id)
    assert sub is not None
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        role=role,
        plan=sub.plan.value,
        status=sub.status.value,
    )


@router.get("/workspaces", response_model=list[WorkspaceOut], summary="Воркспейсы пользователя")
async def list_workspaces(
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> list[WorkspaceOut]:
    rows = (
        await session.execute(
            select(Workspace, Membership.role)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Membership.user_id == user.id)
            .order_by(Workspace.id)
        )
    ).all()
    return [await _out(session, ws, role) for ws, role in rows]


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201, summary="Новый воркспейс")
async def create(
    body: WorkspaceCreateIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> WorkspaceOut:
    ws = await create_workspace(
        session, user, body.name.strip()[:120] or "Workspace", await clock.now()
    )
    await session.commit()
    return await _out(session, ws, Role.owner)
