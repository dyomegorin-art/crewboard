"""Демо-данные: свежий воркспейс для «Попробовать без регистрации» и seed для стенда."""

import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import start_trial
from app.config import get_settings
from app.models import (
    BillingPeriod,
    Comment,
    Membership,
    Priority,
    Project,
    Role,
    Status,
    Task,
    User,
    Workspace,
)
from app.services.audit import audit
from app.services.tasks import POSITION_STEP, create_default_statuses
from app.services.workspaces import create_workspace

BOTS = (
    ("Алина Ветрова", Role.admin),
    ("Максим Орлов", Role.member),
    ("Дарья Север", Role.member),
)

# (колонка, заголовок, приоритет, исполнитель: 0 — владелец, 1..3 — остальные, дней до срока)
_WEB = [
    (0, "Редизайн главной страницы", Priority.high, 1, 5),
    (0, "Подобрать фотографии для героя", Priority.low, 2, None),
    (0, "Согласовать тексты с командой", Priority.medium, 3, 7),
    (0, "Настроить аналитику событий", Priority.medium, None, None),
    (1, "Вёрстка каталога услуг", Priority.high, 1, 3),
    (1, "Форма обратной связи", Priority.medium, 2, 4),
    (1, "Адаптив для мобильных", Priority.urgent, 0, 2),
    (1, "Страница «О нас»", Priority.low, 3, 10),
    (2, "Проверка в Safari и Firefox", Priority.medium, 2, 1),
    (2, "Исправить отступы в футере", Priority.low, 1, None),
    (3, "Лендинг акции", Priority.high, 0, -2),
    (3, "Подключить домен и HTTPS", Priority.urgent, 1, -5),
    (3, "Карта сайта и robots.txt", Priority.low, 2, -7),
]
_APP = [
    (0, "Экран онбординга", Priority.medium, 2, 6),
    (0, "Push-уведомления", Priority.high, None, None),
    (0, "Тёмная тема", Priority.low, 3, 14),
    (1, "Авторизация по коду", Priority.urgent, 1, 2),
    (1, "Профиль пользователя", Priority.medium, 0, 5),
    (2, "Ревью API для списка задач", Priority.medium, 1, 1),
    (2, "Тесты на оффлайн-режим", Priority.high, 3, 3),
    (3, "Иконка и сплэш-экран", Priority.low, 2, -3),
    (3, "Сборка для TestFlight", Priority.medium, 0, -1),
    (3, "Политика конфиденциальности", Priority.low, None, -10),
    (0, "Аналитика воронки", Priority.medium, 1, 9),
    (1, "Синхронизация в фоне", Priority.high, 2, 4),
]
_COMMENTS = {
    "Редизайн главной страницы": ["Макет готов, жду отзывы.", "Поправила отступы под сетку 8px."],
    "Адаптив для мобильных": [
        "Канбан на 375px скроллится горизонтально — проверю на реальном устройстве."
    ],
    "Авторизация по коду": ["Нужен лимит на число попыток входа."],
}


async def populate_workspace(
    session: AsyncSession, ws: Workspace, owner: User, bots: list[User], now: datetime
) -> None:
    """~25 задач в двух проектах: разные колонки, исполнители, сроки, комментарии."""
    people = [owner, *bots]
    projects = []
    for name, key, tasks in (("Сайт компании", "WEB", _WEB), ("Мобильное приложение", "APP", _APP)):
        project = Project(workspace_id=ws.id, name=name, key=key, created_at=now)
        session.add(project)
        await session.flush()
        statuses = await create_default_statuses(session, project)
        # Демо идёт на Pro (trial): добавим колонку Review между работой и готовым.
        review = Status(
            workspace_id=ws.id,
            project_id=project.id,
            name="Review",
            position=2 * POSITION_STEP + 512,
        )
        session.add(review)
        await session.flush()
        columns = [statuses[0], statuses[1], review, statuses[2]]
        counters: dict[int, int] = {}
        for col, title, prio, who, due in tasks:
            status = columns[col]
            pos = counters.get(status.id, 0) + POSITION_STEP
            counters[status.id] = pos
            project.task_counter += 1
            created = now - timedelta(hours=len(counters) * 3 + project.task_counter)
            task = Task(
                workspace_id=ws.id,
                project_id=project.id,
                status_id=status.id,
                number=project.task_counter,
                title=title,
                assignee_id=people[who % len(people)].id if who is not None else None,
                priority=prio,
                due_date=(date.fromtimestamp(now.timestamp()) + timedelta(days=due))
                if due is not None
                else None,
                position=pos,
                created_by=owner.id,
                created_at=created,
                updated_at=created,
            )
            session.add(task)
            await session.flush()
            for i, text in enumerate(_COMMENTS.get(title, [])):
                author = bots[i % len(bots)] if bots else owner
                session.add(
                    Comment(
                        workspace_id=ws.id,
                        task_id=task.id,
                        author_id=author.id,
                        body=text,
                        created_at=created + timedelta(minutes=30 * (i + 1)),
                    )
                )
        projects.append(project)
        await audit(
            session, ws.id, owner.id, "project", project.id, "project.created", now=now, name=name
        )
    await session.flush()


async def create_demo_workspace(session: AsyncSession, now: datetime) -> User:
    suffix = secrets.token_hex(6)
    owner = User(
        email=f"demo-{suffix}@demo.example.com",
        name="Гость-демо",
        is_demo=True,
        email_verified_at=now,
        created_at=now,
    )
    session.add(owner)
    bots = [
        User(
            email=f"bot{i}-{suffix}@demo.example.com",
            name=name,
            is_demo=True,
            email_verified_at=now,
            created_at=now,
        )
        for i, (name, _) in enumerate(BOTS, start=1)
    ]
    session.add_all(bots)
    await session.flush()
    ws = await create_workspace(session, owner, "Демо-команда", now)
    for bot, (_, role) in zip(bots, BOTS, strict=True):
        session.add(Membership(workspace_id=ws.id, user_id=bot.id, role=role))
    await session.flush()
    await populate_workspace(session, ws, owner, bots, now)
    await start_trial(session, ws.id, BillingPeriod.month, owner.id, now)
    return owner


async def cleanup_demo_data(session: AsyncSession, now: datetime) -> int:
    """Удаляет демо-воркспейсы и демо-пользователей старше TTL. Реальные данные не трогает."""
    cutoff = now - timedelta(hours=get_settings().demo_ttl_hours)
    old_users = (
        await session.scalars(
            select(User.id).where(User.is_demo.is_(True), User.created_at < cutoff)
        )
    ).all()
    if not old_users:
        return 0
    # Воркспейсы демо-владельцев уходят каскадом вместе с содержимым.
    await session.execute(delete(Workspace).where(Workspace.owner_id.in_(old_users)))
    await session.execute(delete(User).where(User.id.in_(old_users)))
    return len(old_users)
