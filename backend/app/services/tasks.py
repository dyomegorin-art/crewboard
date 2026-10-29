import re
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceCtx
from app.billing.plans import DEFAULT_STATUSES
from app.errors import AppError
from app.models import (
    Membership,
    Project,
    ProjectGuest,
    Role,
    Status,
    Task,
)
from app.schemas.projects import TaskOut

POSITION_STEP = 1024


def project_key(name: str, taken: Iterable[str]) -> str:
    words = re.findall(r"[A-Za-z]+", name)
    if not words:
        base = "PRJ"
    elif len(words) == 1:
        base = words[0][:3].upper()
    else:
        base = "".join(w[0] for w in words[:5]).upper()
    base = base if len(base) >= 2 else base + "X"
    taken_set = set(taken)
    key = base
    n = 2
    while key in taken_set:
        key = f"{base}{n}"[:8]
        n += 1
    return key


async def create_default_statuses(session: AsyncSession, project: Project) -> list[Status]:
    rows = [
        Status(
            workspace_id=project.workspace_id,
            project_id=project.id,
            name=name,
            position=(i + 1) * POSITION_STEP,
            is_done=done,
        )
        for i, (name, done) in enumerate(DEFAULT_STATUSES)
    ]
    session.add_all(rows)
    await session.flush()
    return rows


def task_out(task: Task, project_key_: str) -> TaskOut:
    return TaskOut(
        id=task.id,
        project_id=task.project_id,
        status_id=task.status_id,
        number=task.number,
        key=f"{project_key_}-{task.number}",
        title=task.title,
        description=task.description,
        assignee_id=task.assignee_id,
        priority=task.priority,
        due_date=task.due_date,
        position=task.position,
        created_by=task.created_by,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def get_project(ctx: WorkspaceCtx, project_id: int) -> Project:
    """Проект своего воркспейса; чужой и несуществующий неразличимы (404).
    Гость видит только проекты, куда добавлен."""
    project = await ctx.session.scalar(
        select(Project).where(Project.id == project_id, Project.workspace_id == ctx.workspace_id)
    )
    if project is None:
        raise AppError(404, "project_not_found", "Project not found")
    if ctx.role == Role.guest:
        granted = await ctx.session.get(ProjectGuest, (ctx.workspace_id, project_id, ctx.user.id))
        if granted is None:
            raise AppError(404, "project_not_found", "Project not found")
    return project


def ensure_not_archived(project: Project) -> None:
    if project.archived_at is not None:
        raise AppError(409, "project_archived", "Archived projects are read-only")


async def get_task(ctx: WorkspaceCtx, task_id: int) -> tuple[Task, Project]:
    task = await ctx.session.scalar(
        select(Task).where(Task.id == task_id, Task.workspace_id == ctx.workspace_id)
    )
    if task is None:
        raise AppError(404, "task_not_found", "Task not found")
    project = await get_project(ctx, task.project_id)
    return task, project


async def get_status(session: AsyncSession, workspace_id: int, status_id: int) -> Status:
    status = await session.scalar(
        select(Status).where(Status.id == status_id, Status.workspace_id == workspace_id)
    )
    if status is None:
        raise AppError(404, "status_not_found", "Status not found")
    return status


async def validate_assignee(session: AsyncSession, workspace_id: int, user_id: int | None) -> None:
    """Исполнитель — только участник этого воркспейса (не гость). Дублирует составной FK,
    чтобы вернуть понятную ошибку, а не нарушение ограничения."""
    if user_id is None:
        return
    m = await session.get(Membership, (workspace_id, user_id))
    if m is None or m.role == Role.guest:
        raise AppError(422, "assignee_not_member", "Assignee must be a member of the workspace")


async def next_number(session: AsyncSession, project_id: int) -> int:
    value = await session.scalar(
        update(Project)
        .where(Project.id == project_id)
        .values(task_counter=Project.task_counter + 1)
        .returning(Project.task_counter)
    )
    assert value is not None
    return int(value)


async def column_tasks(
    session: AsyncSession, status_id: int, exclude_task_id: int | None = None
) -> list[Task]:
    stmt = select(Task).where(Task.status_id == status_id).order_by(Task.position, Task.id)
    if exclude_task_id is not None:
        stmt = stmt.where(Task.id != exclude_task_id)
    return list((await session.scalars(stmt)).all())


async def renumber(session: AsyncSession, tasks: list[Task]) -> None:
    for i, t in enumerate(tasks, start=1):
        t.position = i * POSITION_STEP
    await session.flush()


def between(prev_pos: int | None, next_pos: int | None) -> int | None:
    """Позиция между соседями; None — зазор исчерпан, колонку нужно перенумеровать."""
    if prev_pos is None and next_pos is None:
        return POSITION_STEP
    if next_pos is None:
        assert prev_pos is not None
        return prev_pos + POSITION_STEP
    low = prev_pos if prev_pos is not None else 0
    if next_pos - low < 2:
        return None
    return (low + next_pos) // 2


async def place_after(
    session: AsyncSession, status_id: int, after_id: int | None, moving_id: int | None
) -> int:
    """Позиция для карточки в колонке `status_id` сразу после `after_id` (None — в начало).

    Позиции — целые с шагом 1024; вставка между соседями — среднее. Зазор исчерпан →
    перенумеровываем колонку в той же транзакции и считаем заново.
    """
    for _ in range(2):
        tasks = await column_tasks(session, status_id, moving_id)
        prev: Task | None = None
        nxt: Task | None = tasks[0] if tasks else None
        if after_id is not None:
            idx = next((i for i, t in enumerate(tasks) if t.id == after_id), None)
            if idx is None:
                raise AppError(422, "invalid_position", "after_id is not in the target column")
            prev = tasks[idx]
            nxt = tasks[idx + 1] if idx + 1 < len(tasks) else None
        pos = between(prev.position if prev else None, nxt.position if nxt else None)
        if pos is not None:
            return pos
        await renumber(session, tasks)
    raise AppError(500, "position_failed", "Could not compute position")  # pragma: no cover


async def max_position(session: AsyncSession, status_id: int) -> int:
    value = await session.scalar(select(func.max(Task.position)).where(Task.status_id == status_id))
    return (value or 0) + POSITION_STEP


def touch(task: Task, now: datetime) -> None:
    task.updated_at = now
