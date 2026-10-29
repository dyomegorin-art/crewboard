import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Priority(enum.StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_projects_workspace_id_id"),
        UniqueConstraint("workspace_id", "key", name="uq_projects_workspace_id_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(8))  # префикс карточек: PRJ-12
    task_counter: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Status(Base):
    __tablename__ = "statuses"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_statuses_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["projects.workspace_id", "projects.id"],
            ondelete="CASCADE",
        ),
        Index("ix_statuses_project_position", "project_id", "position"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(60))
    position: Mapped[int] = mapped_column(Integer)
    is_done: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_tasks_workspace_id_id"),
        UniqueConstraint("project_id", "number", name="uq_tasks_project_id_number"),
        # Составные FK не дают сослаться на проект/статус/участника другого воркспейса.
        ForeignKeyConstraint(
            ["workspace_id", "project_id"],
            ["projects.workspace_id", "projects.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "status_id"], ["statuses.workspace_id", "statuses.id"]
        ),
        # Удаление участника обнуляет только assignee_id (workspace_id остаётся).
        ForeignKeyConstraint(
            ["workspace_id", "assignee_id"],
            ["memberships.workspace_id", "memberships.user_id"],
            ondelete="SET NULL (assignee_id)",
        ),
        Index("ix_tasks_board", "project_id", "status_id", "position"),
        Index("ix_tasks_assignee", "workspace_id", "assignee_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(BigInteger)
    project_id: Mapped[int] = mapped_column(BigInteger)
    status_id: Mapped[int] = mapped_column(BigInteger)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    assignee_id: Mapped[int | None] = mapped_column(BigInteger)
    priority: Mapped[Priority] = mapped_column(
        Enum(Priority, native_enum=False, length=16), default=Priority.medium
    )
    due_date: Mapped[date | None] = mapped_column(Date)
    position: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "task_id"], ["tasks.workspace_id", "tasks.id"], ondelete="CASCADE"
        ),
        Index("ix_comments_task", "task_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(BigInteger)
    task_id: Mapped[int] = mapped_column(BigInteger)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActivityLog(Base):
    __tablename__ = "activity_log"
    __table_args__ = (
        Index("ix_activity_workspace_time", "workspace_id", "created_at", "id"),
        Index("ix_activity_entity", "workspace_id", "entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
