import os
import subprocess
import sys
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

# Тесты идут против настоящих PostgreSQL и Redis, но в отдельных БД.
_base = urlsplit(os.environ["DATABASE_URL"])
TEST_DB = "crewboard_test"
os.environ["DATABASE_URL"] = urlunsplit(_base._replace(path=f"/{TEST_DB}"))
_redis = urlsplit(os.environ["REDIS_URL"])
os.environ["REDIS_URL"] = urlunsplit(_redis._replace(path="/1"))
os.environ["ENV"] = "test"
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["MOCK_WEBHOOK_DUPLICATE_RATE"] = "0"
os.environ["DEV_ENDPOINTS"] = "true"
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret-0123456789abcdef")
os.environ["GITHUB_CLIENT_ID"] = ""
os.environ["GITHUB_CLIENT_SECRET"] = ""
os.environ["RATE_LIMIT_LOGIN_PER_MINUTE"] = "5"
ADMIN_DSN = urlunsplit(_base._replace(scheme="postgresql", path="/postgres"))

import asyncpg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    import asyncio

    async def recreate() -> None:
        conn = await asyncpg.connect(ADMIN_DSN)
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {TEST_DB}")
        await conn.close()

    asyncio.run(recreate())
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


@pytest_asyncio.fixture(autouse=True)
async def _clean() -> AsyncIterator[None]:
    from app.db import engine
    from app.redis import redis_client

    yield
    async with engine.begin() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
        )
        tables = ", ".join(r[0] for r in rows)
        if tables:
            await conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    await redis_client.flushdb()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def session() -> AsyncIterator["AsyncSession"]:
    from app.db import SessionLocal

    async with SessionLocal() as s:
        yield s


@pytest.fixture
def clock():
    """Инжектируемые часы: фиксированное «сейчас», которое тесты двигают сами."""
    from datetime import UTC, datetime

    from app.clock import FakeClock
    from app.clock import get_clock as dep
    from app.main import app

    c = FakeClock(datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

    async def _get() -> FakeClock:
        return c

    app.dependency_overrides[dep] = _get
    yield c
    app.dependency_overrides.pop(dep, None)


@pytest.fixture
def make_user():
    """Пользователь напрямую в БД + заголовки (без медленного argon2 на каждого)."""
    from datetime import UTC, datetime

    from app.db import SessionLocal
    from app.models import User
    from app.security import create_token

    counter = {"n": 0}

    async def _make(
        email: str | None = None, *, verified: bool = True, name: str = "User", now=None
    ) -> tuple["User", dict[str, str]]:
        counter["n"] += 1
        async with SessionLocal() as s:
            u = User(
                email=email or f"user{counter['n']}@example.com",
                password_hash="x",
                name=name,
                email_verified_at=datetime.now(UTC) if verified else None,
            )
            s.add(u)
            await s.commit()
        token = create_token(u.id, "access", now or datetime.now(UTC))
        return u, {"Authorization": f"Bearer {token}"}

    return _make


if False:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession
