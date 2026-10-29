import csv
import io
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import WorkspaceCtx, require
from app.billing.limits import get_subscription, require_csv_export
from app.db import SessionLocal
from app.models import Project, Status, Task, User
from app.services.tasks import get_project

router = APIRouter(prefix="/w/{workspace_id}", tags=["export"])

HEADER = ["key", "project", "title", "status", "assignee", "priority"]
HEADER += ["due_date", "created_at", "updated_at"]
BATCH = 500


def _safe(value: object) -> str:
    """Защита от CSV-инъекций: ячейка не должна начинаться с символа формулы."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/export/tasks.csv", summary="Экспорт задач в CSV (Pro), потоковая выдача")
async def export_tasks(
    project_id: int | None = Query(None),
    ctx: WorkspaceCtx = Depends(require("export.csv")),
) -> StreamingResponse:
    require_csv_export(await get_subscription(ctx.session, ctx.workspace_id))
    workspace_id = ctx.workspace_id
    if project_id is not None:
        await get_project(ctx, project_id)  # 404 для чужого проекта

    async def rows() -> AsyncIterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(HEADER)
        yield buf.getvalue()
        last_id = 0
        # Свою сессию открываем внутри генератора: она живёт, пока идёт выдача.
        async with SessionLocal() as session:
            while True:
                stmt = (
                    select(Task, Project.key, Project.name, Status.name, User.name)
                    .join(Project, Project.id == Task.project_id)
                    .join(Status, Status.id == Task.status_id)
                    .outerjoin(User, User.id == Task.assignee_id)
                    .where(Task.workspace_id == workspace_id, Task.id > last_id)
                    .order_by(Task.id)
                    .limit(BATCH)
                )
                if project_id is not None:
                    stmt = stmt.where(Task.project_id == project_id)
                batch = (await session.execute(stmt)).all()
                if not batch:
                    return
                buf = io.StringIO()
                writer = csv.writer(buf)
                for task, key, pname, sname, aname in batch:
                    writer.writerow(
                        [
                            _safe(f"{key}-{task.number}"),
                            _safe(pname),
                            _safe(task.title),
                            _safe(sname),
                            _safe(aname),
                            task.priority.value,
                            task.due_date.isoformat() if task.due_date else "",
                            task.created_at.isoformat(),
                            task.updated_at.isoformat(),
                        ]
                    )
                    last_id = task.id
                yield buf.getvalue()

    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="tasks.csv"'},
    )
