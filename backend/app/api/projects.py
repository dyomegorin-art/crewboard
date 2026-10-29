from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceCtx, require
from app.billing.limits import (
    check_can_create_project,
    get_subscription,
    require_custom_statuses,
)
from app.errors import AppError
from app.models import (
    Membership,
    Priority,
    Project,
    ProjectGuest,
    Role,
    Status,
    Task,
)
from app.schemas.errors import ErrorResponse
from app.schemas.projects import (
    BoardOut,
    GuestIn,
    ProjectIn,
    ProjectOut,
    ProjectRename,
    StatusIn,
    StatusOrderIn,
    StatusOut,
    StatusUpdate,
)
from app.services.audit import audit
from app.services.tasks import (
    POSITION_STEP,
    create_default_statuses,
    ensure_not_archived,
    get_project,
    get_status,
    project_key,
    task_out,
)

router = APIRouter(prefix="/w/{workspace_id}", tags=["projects"])

_errors: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Не найдено / чужой воркспейс"},
    402: {"model": ErrorResponse, "description": "Лимит тарифа или read_only"},
}


def project_out(p: Project, task_count: int) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        name=p.name,
        key=p.key,
        archived=p.archived_at is not None,
        task_count=task_count,
        created_at=p.created_at,
    )


def status_out(s: Status) -> StatusOut:
    return StatusOut(
        id=s.id, project_id=s.project_id, name=s.name, position=s.position, is_done=s.is_done
    )


async def _counts(session: AsyncSession, project_ids: list[int]) -> dict[int, int]:
    if not project_ids:
        return {}
    rows = await session.execute(
        select(Task.project_id, func.count())
        .where(Task.project_id.in_(project_ids))
        .group_by(Task.project_id)
    )
    return {pid: n for pid, n in rows.all()}


@router.get("/projects", response_model=list[ProjectOut], summary="Проекты воркспейса")
async def list_projects(
    include_archived: bool = Query(False),
    ctx: WorkspaceCtx = Depends(require("projects.read")),
) -> list[ProjectOut]:
    stmt = select(Project).where(Project.workspace_id == ctx.workspace_id).order_by(Project.id)
    if not include_archived:
        stmt = stmt.where(Project.archived_at.is_(None))
    if ctx.role == Role.guest:
        stmt = stmt.join(
            ProjectGuest,
            (ProjectGuest.project_id == Project.id) & (ProjectGuest.user_id == ctx.user.id),
        )
    projects = list((await ctx.session.scalars(stmt)).all())
    counts = await _counts(ctx.session, [p.id for p in projects])
    return [project_out(p, counts.get(p.id, 0)) for p in projects]


@router.post(
    "/projects",
    response_model=ProjectOut,
    status_code=201,
    summary="Создать проект",
    responses=_errors,
)
async def create_project(
    body: ProjectIn, ctx: WorkspaceCtx = Depends(require("projects.manage", write=True))
) -> ProjectOut:
    session = ctx.session
    sub = await get_subscription(session, ctx.workspace_id, lock=True)
    await check_can_create_project(session, sub)
    taken = (
        await session.scalars(select(Project.key).where(Project.workspace_id == ctx.workspace_id))
    ).all()
    key = body.key.upper() if body.key else project_key(body.name, taken)
    if key in taken:
        raise AppError(409, "project_key_taken", "Project key is already used")
    project = Project(workspace_id=ctx.workspace_id, name=body.name.strip(), key=key)
    session.add(project)
    await session.flush()
    await create_default_statuses(session, project)
    await audit(
        session, ctx.workspace_id, ctx.user.id, "project", project.id, "project.created",
        now=await ctx.now_.now(), name=project.name,
    )  # fmt: skip
    await session.commit()
    await ctx.publish("project.changed", project_id=project.id)
    return project_out(project, 0)


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectOut,
    summary="Переименовать проект",
    responses=_errors,
)
async def rename_project(
    project_id: int,
    body: ProjectRename,
    ctx: WorkspaceCtx = Depends(require("projects.manage", write=True)),
) -> ProjectOut:
    project = await get_project(ctx, project_id)
    ensure_not_archived(project)
    old, project.name = project.name, body.name.strip()
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "project", project.id, "project.renamed",
        now=await ctx.now_.now(), old=old, new=project.name,
    )  # fmt: skip
    await ctx.session.commit()
    await ctx.publish("project.changed", project_id=project.id)
    return project_out(project, (await _counts(ctx.session, [project.id])).get(project.id, 0))


@router.post(
    "/projects/{project_id}/archive",
    response_model=ProjectOut,
    summary="Архивировать проект (становится read-only)",
    responses=_errors,
)
async def archive_project(
    project_id: int, ctx: WorkspaceCtx = Depends(require("projects.manage", write=True))
) -> ProjectOut:
    project = await get_project(ctx, project_id)
    now = await ctx.now_.now()
    project.archived_at = project.archived_at or now
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "project", project.id, "project.archived",
        now=now,
    )  # fmt: skip
    await ctx.session.commit()
    await ctx.publish("project.changed", project_id=project.id)
    return project_out(project, (await _counts(ctx.session, [project.id])).get(project.id, 0))


@router.post(
    "/projects/{project_id}/unarchive",
    response_model=ProjectOut,
    summary="Вернуть проект из архива (с учётом лимита тарифа)",
    responses=_errors,
)
async def unarchive_project(
    project_id: int, ctx: WorkspaceCtx = Depends(require("projects.manage", write=True))
) -> ProjectOut:
    project = await get_project(ctx, project_id)
    if project.archived_at is not None:
        sub = await get_subscription(ctx.session, ctx.workspace_id, lock=True)
        await check_can_create_project(ctx.session, sub)
        project.archived_at = None
        await audit(
            ctx.session, ctx.workspace_id, ctx.user.id, "project", project.id, "project.unarchived",
            now=await ctx.now_.now(),
        )  # fmt: skip
        await ctx.session.commit()
        await ctx.publish("project.changed", project_id=project.id)
    return project_out(project, (await _counts(ctx.session, [project.id])).get(project.id, 0))


@router.get(
    "/projects/{project_id}/board",
    response_model=BoardOut,
    summary="Доска: колонки и карточки (фильтры по исполнителю и приоритету)",
    responses=_errors,
)
async def board(
    project_id: int,
    assignee_id: int | None = Query(None),
    priority: Priority | None = Query(None),
    mine: bool = Query(False),
    ctx: WorkspaceCtx = Depends(require("tasks.read")),
) -> BoardOut:
    project = await get_project(ctx, project_id)
    statuses = (
        await ctx.session.scalars(
            select(Status)
            .where(Status.project_id == project.id)
            .order_by(Status.position, Status.id)
        )
    ).all()
    stmt = select(Task).where(Task.project_id == project.id)
    if mine:
        stmt = stmt.where(Task.assignee_id == ctx.user.id)
    elif assignee_id is not None:
        stmt = stmt.where(Task.assignee_id == assignee_id)
    if priority is not None:
        stmt = stmt.where(Task.priority == priority)
    tasks = (await ctx.session.scalars(stmt.order_by(Task.position, Task.id))).all()
    total = (await _counts(ctx.session, [project.id])).get(project.id, 0)
    return BoardOut(
        project=project_out(project, total),
        statuses=[status_out(s) for s in statuses],
        tasks=[task_out(t, project.key) for t in tasks],
    )


# --- Колонки (кастомные — только Pro) ---------------------------------------------------


@router.post(
    "/projects/{project_id}/statuses",
    response_model=StatusOut,
    status_code=201,
    summary="Добавить колонку (Pro)",
    responses=_errors,
)
async def add_status(
    project_id: int,
    body: StatusIn,
    ctx: WorkspaceCtx = Depends(require("statuses.manage", write=True)),
) -> StatusOut:
    project = await get_project(ctx, project_id)
    ensure_not_archived(project)
    require_custom_statuses(await get_subscription(ctx.session, ctx.workspace_id))
    last = await ctx.session.scalar(
        select(func.max(Status.position)).where(Status.project_id == project.id)
    )
    status = Status(
        workspace_id=ctx.workspace_id,
        project_id=project.id,
        name=body.name.strip(),
        position=(last or 0) + POSITION_STEP,
        is_done=body.is_done,
    )
    ctx.session.add(status)
    await ctx.session.flush()
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "status", status.id, "status.created",
        now=await ctx.now_.now(), project_id=project.id, name=status.name,
    )  # fmt: skip
    await ctx.session.commit()
    await ctx.publish("project.changed", project_id=project.id)
    return status_out(status)


async def _status_for_edit(ctx: WorkspaceCtx, status_id: int) -> tuple[Status, Project]:
    status = await get_status(ctx.session, ctx.workspace_id, status_id)
    project = await get_project(ctx, status.project_id)
    ensure_not_archived(project)
    require_custom_statuses(await get_subscription(ctx.session, ctx.workspace_id))
    return status, project


@router.patch(
    "/statuses/{status_id}",
    response_model=StatusOut,
    summary="Переименовать колонку (Pro)",
    responses=_errors,
)
async def update_status(
    status_id: int,
    body: StatusUpdate,
    ctx: WorkspaceCtx = Depends(require("statuses.manage", write=True)),
) -> StatusOut:
    status, project = await _status_for_edit(ctx, status_id)
    if body.name is not None:
        status.name = body.name.strip()
    if body.is_done is not None:
        status.is_done = body.is_done
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "status", status.id, "status.updated",
        now=await ctx.now_.now(), name=status.name,
    )  # fmt: skip
    await ctx.session.commit()
    await ctx.publish("project.changed", project_id=project.id)
    return status_out(status)


@router.delete(
    "/statuses/{status_id}",
    status_code=204,
    summary="Удалить пустую колонку (Pro)",
    responses={**_errors, 409: {"model": ErrorResponse, "description": "В колонке есть задачи"}},
)
async def delete_status(
    status_id: int, ctx: WorkspaceCtx = Depends(require("statuses.manage", write=True))
) -> None:
    status, project = await _status_for_edit(ctx, status_id)
    has_tasks = await ctx.session.scalar(
        select(func.count()).select_from(Task).where(Task.status_id == status.id)
    )
    if has_tasks:
        raise AppError(409, "status_not_empty", "Only empty columns can be deleted")
    await ctx.session.delete(status)
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "status", status_id, "status.deleted",
        now=await ctx.now_.now(), name=status.name,
    )  # fmt: skip
    await ctx.session.commit()
    await ctx.publish("project.changed", project_id=project.id)


@router.put(
    "/projects/{project_id}/statuses/order",
    response_model=list[StatusOut],
    summary="Сортировка колонок (Pro)",
    responses=_errors,
)
async def reorder_statuses(
    project_id: int,
    body: StatusOrderIn,
    ctx: WorkspaceCtx = Depends(require("statuses.manage", write=True)),
) -> list[StatusOut]:
    project = await get_project(ctx, project_id)
    ensure_not_archived(project)
    require_custom_statuses(await get_subscription(ctx.session, ctx.workspace_id))
    statuses = {
        s.id: s
        for s in (
            await ctx.session.scalars(select(Status).where(Status.project_id == project.id))
        ).all()
    }
    if sorted(body.ids) != sorted(statuses):
        raise AppError(422, "invalid_order", "ids must list every column of the project once")
    for i, sid in enumerate(body.ids, start=1):
        statuses[sid].position = i * POSITION_STEP
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "project", project.id, "statuses.reordered",
        now=await ctx.now_.now(),
    )  # fmt: skip
    await ctx.session.commit()
    await ctx.publish("project.changed", project_id=project.id)
    return [status_out(statuses[sid]) for sid in body.ids]


# --- Гости проекта ---------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/guests",
    status_code=204,
    summary="Дать гостю доступ к проекту",
    responses=_errors,
)
async def add_guest(
    project_id: int,
    body: GuestIn,
    ctx: WorkspaceCtx = Depends(require("projects.manage", write=True)),
) -> None:
    project = await get_project(ctx, project_id)
    m = await ctx.session.get(Membership, (ctx.workspace_id, body.user_id))
    if m is None or m.role != Role.guest:
        raise AppError(422, "not_a_guest", "User is not a guest of this workspace")
    if await ctx.session.get(ProjectGuest, (ctx.workspace_id, project.id, body.user_id)) is None:
        ctx.session.add(
            ProjectGuest(workspace_id=ctx.workspace_id, project_id=project.id, user_id=body.user_id)
        )
        await audit(
            ctx.session, ctx.workspace_id, ctx.user.id, "project", project.id,
            "project.guest_added", now=await ctx.now_.now(), user_id=body.user_id,
        )  # fmt: skip
    await ctx.session.commit()


@router.delete(
    "/projects/{project_id}/guests/{user_id}",
    status_code=204,
    summary="Закрыть гостю доступ к проекту",
    responses=_errors,
)
async def remove_guest(
    project_id: int,
    user_id: int,
    ctx: WorkspaceCtx = Depends(require("projects.manage", write=True)),
) -> None:
    project = await get_project(ctx, project_id)
    row = await ctx.session.get(ProjectGuest, (ctx.workspace_id, project.id, user_id))
    if row is not None:
        await ctx.session.delete(row)
        await ctx.session.commit()
