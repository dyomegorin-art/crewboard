from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models import Priority


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    key: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,6}$")


class ProjectRename(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectOut(BaseModel):
    id: int
    name: str
    key: str
    archived: bool
    task_count: int
    created_at: datetime


class StatusOut(BaseModel):
    id: int
    project_id: int
    name: str
    position: int
    is_done: bool


class StatusIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    is_done: bool = False


class StatusUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    is_done: bool | None = None


class StatusOrderIn(BaseModel):
    ids: list[int]


class GuestIn(BaseModel):
    user_id: int


class TaskOut(BaseModel):
    id: int
    project_id: int
    status_id: int
    number: int
    key: str
    title: str
    description: str
    assignee_id: int | None
    priority: Priority
    due_date: date | None
    position: int
    created_by: int | None
    created_at: datetime
    updated_at: datetime


class BoardOut(BaseModel):
    project: ProjectOut
    statuses: list[StatusOut]
    tasks: list[TaskOut]


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20000)
    status_id: int | None = None
    assignee_id: int | None = None
    priority: Priority = Priority.medium
    due_date: date | None = None


class TaskUpdate(BaseModel):
    """PATCH: меняются только переданные поля (в том числе явный null для исполнителя/срока)."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20000)
    assignee_id: int | None = None
    priority: Priority | None = None
    due_date: date | None = None


class TaskMoveIn(BaseModel):
    status_id: int
    after_id: int | None = None  # карточка, под которой встанет задача; null — в начало колонки


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class CommentOut(BaseModel):
    id: int
    task_id: int
    author_id: int | None
    author_name: str | None
    body: str
    created_at: datetime


class ActivityOut(BaseModel):
    id: int
    actor_id: int | None
    actor_name: str | None
    entity: str
    entity_id: int | None
    action: str
    detail: dict[str, object]
    created_at: datetime


class ActivityPage(BaseModel):
    items: list[ActivityOut]
    next_cursor: str | None
