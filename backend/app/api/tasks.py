import base64
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceCtx, require
from app.billing.limits import get_subscription, plan_limits
from app.errors import AppError
from app.models import ActivityLog, Comment, Project, Role, Status, Task, User
from app.schemas.errors import ErrorResponse
from app.schemas.projects import (
    ActivityOut,
    ActivityPage,
    CommentIn,
    CommentOut,
    TaskIn,
    TaskMoveIn,
    TaskOut,
    TaskUpdate,
)
from app.services.audit import audit
from app.services.tasks import (
    ensure_not_archived,
    get_project,
    get_status,
    get_task,
    max_position,
    next_number,
    place_after,
    task_out,
    validate_assignee,
)

router = APIRouter(prefix="/w/{workspace_id}", tags=["tasks"])

_errors: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Не найдено / чужой воркспейс"},
    402: {"model": ErrorResponse, "description": "Воркспейс в read_only"},
}


async def _default_status(session: AsyncSession, project: Project) -> Status:
    first = await session.scalar(
        select(Status).where(Status.project_id == project.id).order_by(Status.position).limit(1)
    )
    if first is None:  # pragma: no cover - у проекта всегда есть колонки
        raise AppError(409, "project_has_no_statuses", "Project has no columns")
    return first


@router.post(
    "/projects/{project_id}/tasks",
    response_model=TaskOut,
    status_code=201,
    summary="Создать задачу (в конец колонки)",
    responses={**_errors, 422: {"model": ErrorResponse}},
)
async def create_task(
    project_id: int,
    body: TaskIn,
    ctx: WorkspaceCtx = Depends(require("tasks.write", write=True)),
) -> TaskOut:
    session = ctx.session
    project = await get_project(ctx, project_id)
    ensure_not_archived(project)
    status = (
        await get_status(session, ctx.workspace_id, body.status_id)
        if body.status_id is not None
        else await _default_status(session, project)
    )
    if status.project_id != project.id:
        raise AppError(422, "status_not_in_project", "Status belongs to another project")
    await validate_assignee(session, ctx.workspace_id, body.assignee_id)
    now = await ctx.now_.now()
    task = Task(
        workspace_id=ctx.workspace_id,
        project_id=project.id,
        status_id=status.id,
        number=await next_number(session, project.id),
        title=body.title.strip(),
        description=body.description,
        assignee_id=body.assignee_id,
        priority=body.priority,
        due_date=body.due_date,
        position=await max_position(session, status.id),
        created_by=ctx.user.id,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    await session.flush()
    await audit(
        session, ctx.workspace_id, ctx.user.id, "task", task.id, "task.created",
        now=now, key=f"{project.key}-{task.number}", title=task.title,
    )  # fmt: skip
    await session.commit()
    await ctx.publish("task.changed", project_id=project.id, task_id=task.id)
    return task_out(task, project.key)


@router.get("/tasks/{task_id}", response_model=TaskOut, summary="Задача", responses=_errors)
async def read_task(task_id: int, ctx: WorkspaceCtx = Depends(require("tasks.read"))) -> TaskOut:
    task, project = await get_task(ctx, task_id)
    return task_out(task, project.key)


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskOut,
    summary="Изменить поля задачи",
    responses={**_errors, 422: {"model": ErrorResponse}},
)
async def update_task(
    task_id: int,
    body: TaskUpdate,
    ctx: WorkspaceCtx = Depends(require("tasks.write", write=True)),
) -> TaskOut:
    task, project = await get_task(ctx, task_id)
    ensure_not_archived(project)
    changes: dict[str, list[object]] = {}
    for field in body.model_fields_set:
        new = getattr(body, field)
        if field in ("title", "priority") and new is None:
            continue
        if field == "assignee_id":
            await validate_assignee(ctx.session, ctx.workspace_id, new)
        old = getattr(task, field)
        if old != new:
            changes[field] = [
                str(old) if old is not None else None,
                str(new) if new is not None else None,
            ]
            setattr(task, field, new.strip() if field == "title" else new)
    if changes:
        now = await ctx.now_.now()
        task.updated_at = now
        await audit(
            ctx.session, ctx.workspace_id, ctx.user.id, "task", task.id, "task.updated",
            now=now, changes=changes,
        )  # fmt: skip
        await ctx.session.commit()
        await ctx.publish("task.changed", project_id=project.id, task_id=task.id)
    return task_out(task, project.key)


@router.post(
    "/tasks/{task_id}/move",
    response_model=TaskOut,
    summary="Переместить карточку (между колонками и внутри колонки)",
    responses={**_errors, 422: {"model": ErrorResponse}},
)
async def move_task(
    task_id: int,
    body: TaskMoveIn,
    ctx: WorkspaceCtx = Depends(require("tasks.write", write=True)),
) -> TaskOut:
    session = ctx.session
    task, project = await get_task(ctx, task_id)
    ensure_not_archived(project)
    status = await get_status(session, ctx.workspace_id, body.status_id)
    if status.project_id != project.id:
        raise AppError(422, "status_not_in_project", "Status belongs to another project")
    if body.after_id == task.id:
        raise AppError(422, "invalid_position", "A card cannot be placed after itself")
    old_status_id = task.status_id
    task.position = await place_after(session, status.id, body.after_id, task.id)
    task.status_id = status.id
    now = await ctx.now_.now()
    task.updated_at = now
    if old_status_id != status.id:
        old = await session.get(Status, old_status_id)
        await audit(
            session, ctx.workspace_id, ctx.user.id, "task", task.id, "task.moved",
            now=now, **{"from": old.name if old else None, "to": status.name},
        )  # fmt: skip
    await session.commit()
    await ctx.publish("task.changed", project_id=project.id, task_id=task.id)
    return task_out(task, project.key)


@router.delete("/tasks/{task_id}", status_code=204, summary="Удалить задачу", responses=_errors)
async def delete_task(
    task_id: int, ctx: WorkspaceCtx = Depends(require("tasks.write", write=True))
) -> None:
    task, project = await get_task(ctx, task_id)
    ensure_not_archived(project)
    await ctx.session.delete(task)
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "task", task_id, "task.deleted",
        now=await ctx.now_.now(), key=f"{project.key}-{task.number}", title=task.title,
    )  # fmt: skip
    await ctx.session.commit()
    await ctx.publish("task.changed", project_id=project.id, task_id=task_id)


@router.get("/my-tasks", response_model=list[TaskOut], summary="Мои задачи по всем проектам")
async def my_tasks(ctx: WorkspaceCtx = Depends(require("tasks.read"))) -> list[TaskOut]:
    rows = (
        await ctx.session.execute(
            select(Task, Project.key)
            .join(Project, Project.id == Task.project_id)
            .where(
                Task.workspace_id == ctx.workspace_id,
                Task.assignee_id == ctx.user.id,
                Project.archived_at.is_(None),
            )
            .order_by(Task.due_date.asc().nulls_last(), Task.id)
        )
    ).all()
    return [task_out(t, key) for t, key in rows]


# --- Комментарии и история -------------------------------------------------------------


@router.get(
    "/tasks/{task_id}/comments",
    response_model=list[CommentOut],
    summary="Комментарии задачи",
    responses=_errors,
)
async def list_comments(
    task_id: int, ctx: WorkspaceCtx = Depends(require("tasks.read"))
) -> list[CommentOut]:
    task, _ = await get_task(ctx, task_id)
    rows = (
        await ctx.session.execute(
            select(Comment, User.name)
            .outerjoin(User, User.id == Comment.author_id)
            .where(Comment.task_id == task.id, Comment.workspace_id == ctx.workspace_id)
            .order_by(Comment.created_at, Comment.id)
        )
    ).all()
    return [
        CommentOut(
            id=c.id,
            task_id=c.task_id,
            author_id=c.author_id,
            author_name=name,
            body=c.body,
            created_at=c.created_at,
        )
        for c, name in rows
    ]


@router.post(
    "/tasks/{task_id}/comments",
    response_model=CommentOut,
    status_code=201,
    summary="Добавить комментарий (гость тоже может)",
    responses=_errors,
)
async def add_comment(
    task_id: int,
    body: CommentIn,
    ctx: WorkspaceCtx = Depends(require("comments.write", write=True)),
) -> CommentOut:
    task, project = await get_task(ctx, task_id)
    ensure_not_archived(project)
    now = await ctx.now_.now()
    comment = Comment(
        workspace_id=ctx.workspace_id,
        task_id=task.id,
        author_id=ctx.user.id,
        body=body.body,
        created_at=now,
    )
    ctx.session.add(comment)
    await ctx.session.flush()
    await audit(
        ctx.session, ctx.workspace_id, ctx.user.id, "task", task.id, "comment.added",
        now=now, comment_id=comment.id,
    )  # fmt: skip
    await ctx.session.commit()
    await ctx.publish("comment.added", project_id=project.id, task_id=task.id)
    return CommentOut(
        id=comment.id,
        task_id=task.id,
        author_id=ctx.user.id,
        author_name=ctx.user.name,
        body=comment.body,
        created_at=comment.created_at,
    )


def _activity_out(row: ActivityLog, actor_name: str | None) -> ActivityOut:
    return ActivityOut(
        id=row.id,
        actor_id=row.actor_id,
        actor_name=actor_name,
        entity=row.entity,
        entity_id=row.entity_id,
        action=row.action,
        detail=row.detail,
        created_at=row.created_at,
    )


@router.get(
    "/tasks/{task_id}/history",
    response_model=list[ActivityOut],
    summary="История изменений задачи",
    responses=_errors,
)
async def task_history(
    task_id: int, ctx: WorkspaceCtx = Depends(require("tasks.read"))
) -> list[ActivityOut]:
    task, _ = await get_task(ctx, task_id)
    rows = (
        await ctx.session.execute(
            select(ActivityLog, User.name)
            .outerjoin(User, User.id == ActivityLog.actor_id)
            .where(
                ActivityLog.workspace_id == ctx.workspace_id,
                ActivityLog.entity == "task",
                ActivityLog.entity_id == task.id,
            )
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        )
    ).all()
    return [_activity_out(r, name) for r, name in rows]


def _encode_cursor(row: ActivityLog) -> str:
    return base64.urlsafe_b64encode(f"{row.created_at.isoformat()}|{row.id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        ts, raw_id = base64.urlsafe_b64decode(cursor.encode()).decode().rsplit("|", 1)
        return ts, int(raw_id)
    except (ValueError, UnicodeDecodeError):
        raise AppError(422, "invalid_cursor", "Invalid cursor") from None


@router.get(
    "/activity",
    response_model=ActivityPage,
    summary="Лог активности воркспейса (курсорная пагинация; на Free — последние 7 дней)",
)
async def activity(
    cursor: str | None = Query(None),
    limit: int = Query(30, ge=1, le=100),
    ctx: WorkspaceCtx = Depends(require("activity.view")),
) -> ActivityPage:
    from datetime import datetime

    now = await ctx.now_.now()
    sub = await get_subscription(ctx.session, ctx.workspace_id)
    stmt = (
        select(ActivityLog, User.name)
        .outerjoin(User, User.id == ActivityLog.actor_id)
        .where(ActivityLog.workspace_id == ctx.workspace_id)
    )
    days = plan_limits(sub).activity_days
    if days is not None:  # ограничение тарифа применяется на сервере, а не в UI
        stmt = stmt.where(ActivityLog.created_at >= now - timedelta(days=days))
    if ctx.role not in (Role.owner, Role.admin):
        stmt = stmt.where(ActivityLog.entity != "permission")
    if cursor:
        ts, last_id = _decode_cursor(cursor)
        stamp = datetime.fromisoformat(ts)
        stmt = stmt.where(
            or_(
                ActivityLog.created_at < stamp,
                and_(ActivityLog.created_at == stamp, ActivityLog.id < last_id),
            )
        )
    rows = (
        await ctx.session.execute(
            stmt.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc()).limit(limit + 1)
        )
    ).all()
    page = rows[:limit]
    return ActivityPage(
        items=[_activity_out(r, name) for r, name in page],
        next_cursor=_encode_cursor(page[-1][0]) if len(rows) > limit and page else None,
    )
