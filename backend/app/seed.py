"""Идемпотентный seed стенда: запускается при каждом старте api, только в local/test.

Создаёт трёх постоянных пользователей и воркспейс «Acme Studio» на Pro — чтобы можно было
сразу проверить роли и биллинг. Данные вымышленные.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.billing.service import start_trial
from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import BillingPeriod, Membership, Role, User
from app.security import hash_password
from app.services.demo import populate_workspace
from app.services.workspaces import create_workspace

log = logging.getLogger("app.seed")

PASSWORD = "crewboard-demo"
ACCOUNTS = (
    ("owner@acme-studio.example.com", "Олег Власов", Role.owner),
    ("admin@acme-studio.example.com", "Анна Седова", Role.admin),
    ("member@acme-studio.example.com", "Игорь Лесной", Role.member),
)


async def seed() -> None:
    if not get_settings().is_local:
        return
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        exists = await session.scalar(
            select(func.count()).select_from(User).where(User.email == ACCOUNTS[0][0])
        )
        if exists:
            return
        pw = await hash_password(PASSWORD)
        users = [
            User(email=email, name=name, password_hash=pw, email_verified_at=now)
            for email, name, _ in ACCOUNTS
        ]
        session.add_all(users)
        await session.flush()
        owner = users[0]
        ws = await create_workspace(session, owner, "Acme Studio", now)
        for user, (_, _, role) in zip(users[1:], ACCOUNTS[1:], strict=True):
            session.add(Membership(workspace_id=ws.id, user_id=user.id, role=role))
        await session.flush()
        await populate_workspace(session, ws, owner, users[1:], now)
        await start_trial(session, ws.id, BillingPeriod.month, owner.id, now)
        await session.commit()
        log.info("seeded Acme Studio")


async def main() -> None:
    try:
        await seed()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
