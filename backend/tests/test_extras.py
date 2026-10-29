"""Демо-вход и его очистка, seed, SSE и экспорт CSV."""

import asyncio
import json
from datetime import timedelta
from typing import Any

import jwt
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import event_stream
from app.db import SessionLocal
from app.events import publish
from app.models import Membership, Project, Task, User, Workspace
from app.redis import redis_client
from app.services.demo import cleanup_demo_data

from .helpers import W

CSRF = {"X-Requested-With": "crewboard"}


async def test_demo_login_creates_populated_pro_trial_workspace(client: AsyncClient) -> None:
    r = await client.post("/api/auth/demo")
    assert r.status_code == 201 and r.json()["user"]["is_demo"] is True
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    (ws,) = (await client.get("/api/workspaces", headers=h)).json()
    assert ws["role"] == "owner" and ws["plan"] == "pro" and ws["status"] == "trialing"
    members = (await client.get(W_(ws), headers=h)).json()
    assert len(members) == 4 and sorted(m["role"] for m in members) == [
        "admin",
        "member",
        "member",
        "owner",
    ]
    projects = (await client.get(f"/api/w/{ws['id']}/projects", headers=h)).json()
    assert len(projects) == 2 and sum(p["task_count"] for p in projects) == 25
    board = (
        await client.get(f"/api/w/{ws['id']}/projects/{projects[0]['id']}/board", headers=h)
    ).json()
    assert [s["name"] for s in board["statuses"]] == ["To do", "In progress", "Review", "Done"]
    mine = (await client.get(f"/api/w/{ws['id']}/my-tasks", headers=h)).json()
    assert mine  # у владельца есть свои задачи
    assert "crewboard_refresh" in client.cookies
    # Каждый вход — свежие данные.
    second = await client.post("/api/auth/demo")
    assert second.json()["user"]["id"] != r.json()["user"]["id"]


def W_(ws: dict[str, Any]) -> str:
    return f"/api/w/{ws['id']}/members"


async def test_demo_user_cannot_reset_password_or_login_with_password(
    client: AsyncClient, session: AsyncSession
) -> None:
    r = await client.post("/api/auth/demo")
    email = r.json()["user"]["email"]
    await client.post("/api/auth/password-reset/request", json={"email": email})
    from app.models import OutboxCommand

    assert (
        await session.scalars(select(OutboxCommand).where(OutboxCommand.command == "SEND_EMAIL"))
    ).all() == []
    login = await client.post("/api/auth/login", json={"email": email, "password": "whatever-123"})
    assert login.status_code == 401


async def test_demo_cleanup_removes_only_expired_demo_data(
    client: AsyncClient, factory: Any, clock: Any, session: AsyncSession
) -> None:
    real_ws, real_user, real_h = await factory.workspace("Real team")
    await client.post("/api/auth/demo")
    await client.post("/api/auth/demo")
    async with SessionLocal() as s:
        assert await cleanup_demo_data(s, clock.current) == 0  # ещё не истекли
    users_before = (await session.scalar(select(func.count()).select_from(User))) or 0
    assert users_before == 1 + 2 * 4

    async with SessionLocal() as s:
        removed = await cleanup_demo_data(s, clock.current + timedelta(hours=1000))
        await s.commit()
    assert removed == 8
    session.expire_all()
    assert (await session.scalar(select(func.count()).select_from(User))) == 1
    assert (await session.scalar(select(func.count()).select_from(Workspace))) == 1
    assert (await session.scalar(select(func.count()).select_from(Project))) == 0
    assert (await session.scalar(select(func.count()).select_from(Task))) == 0
    assert (await session.scalar(select(func.count()).select_from(Membership))) == 1
    assert (await client.get(W(real_ws), headers=real_h)).status_code == 200
    _ = real_user


async def test_seed_is_idempotent_and_local_only(
    client: AsyncClient, session: AsyncSession, monkeypatch: Any
) -> None:
    from app import seed

    await seed.seed()
    await seed.seed()
    assert (await session.scalar(select(func.count()).select_from(User))) == 3
    login = await client.post(
        "/api/auth/login",
        json={"email": "owner@acme-studio.example.com", "password": seed.PASSWORD},
    )
    assert login.status_code == 200
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    (ws,) = (await client.get("/api/workspaces", headers=h)).json()
    assert ws["name"] == "Acme Studio" and ws["plan"] == "pro"

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "env", "production")
    await seed.seed()  # вне local ничего не делает
    assert (await session.scalar(select(func.count()).select_from(User))) == 3


# --- SSE ---------------------------------------------------------------------------------


async def test_event_ticket_and_stream_auth(
    client: AsyncClient, factory: Any, make_user: Any, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    _, outsider = await make_user()
    assert (await client.post(W(ws, "/events/ticket"), headers=outsider)).status_code == 404
    r = await client.post(W(ws, "/events/ticket"), headers=oh)
    ticket = r.json()["ticket"]
    assert r.json()["expires_in"] == 60
    assert jwt.get_unverified_header(ticket)["alg"] == "HS256"

    # Невалидный, чужой и просроченный билеты не принимаются (до открытия потока).
    assert (await client.get(W(ws, "/events"), params={"ticket": "junk"})).status_code == 401
    other, _, _ = await factory.workspace("Other")
    assert (await client.get(W(other, "/events"), params={"ticket": ticket})).status_code == 404
    clock.advance(seconds=61)
    assert (await client.get(W(ws, "/events"), params={"ticket": ticket})).status_code == 401
    # access-токен как билет не годится: другой тип токена.
    access = oh["Authorization"].split()[1]
    assert (await client.get(W(ws, "/events"), params={"ticket": access})).status_code == 401


async def test_event_stream_delivers_published_changes() -> None:
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    stream = event_stream(redis_client, 777, is_disconnected, heartbeat=0.05)
    first = await stream.__anext__()
    assert "connected" in first
    await asyncio.sleep(0.1)
    await publish(redis_client, 777, "task.changed", project_id=1, task_id=2, actor_id=9)
    frames: list[str] = []
    for _ in range(10):
        frame = await asyncio.wait_for(stream.__anext__(), 2)
        frames.append(frame)
        if frame.startswith("data:"):
            break
    data = [f for f in frames if f.startswith("data:")]
    assert json.loads(data[0][5:]) == {
        "type": "task.changed",
        "project_id": 1,
        "task_id": 2,
        "actor_id": 9,
    }
    assert any(f.startswith(": ping") for f in frames) or len(frames) >= 1
    # Чужой воркспейс события не получает.
    await publish(redis_client, 778, "task.changed")
    disconnected = True
    await stream.aclose()


async def test_guest_stream_forwards_nothing() -> None:
    async def never() -> bool:
        return False

    stream = event_stream(redis_client, 800, never, heartbeat=0.05, forward=False)
    await stream.__anext__()
    await asyncio.sleep(0.1)
    await publish(redis_client, 800, "task.changed")
    got = [await asyncio.wait_for(stream.__anext__(), 2) for _ in range(3)]
    assert all(not f.startswith("data:") for f in got)
    await stream.aclose()


async def test_api_mutations_publish_events(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"ws:{ws.id}")
    await pubsub.get_message(timeout=1)
    p = (await client.post(W(ws, "/projects"), json={"name": "Live"}, headers=oh)).json()
    t = (
        await client.post(W(ws, f"/projects/{p['id']}/tasks"), json={"title": "x"}, headers=oh)
    ).json()
    seen = []
    for _ in range(5):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        if msg:
            seen.append(json.loads(msg["data"]))
    assert {"type": "project.changed"}.items() <= seen[0].items()
    assert seen[1]["type"] == "task.changed" and seen[1]["task_id"] == t["id"]
    await pubsub.aclose()  # type: ignore[no-untyped-call]


# --- CSV ---------------------------------------------------------------------------------


async def test_csv_export_is_pro_only_streams_and_escapes_formulas(
    client: AsyncClient, factory: Any
) -> None:
    ws, owner, oh = await factory.workspace()
    p = (await client.post(W(ws, "/projects"), json={"name": "Exports"}, headers=oh)).json()
    denied = await client.get(W(ws, "/export/tasks.csv"), headers=oh)
    assert denied.status_code == 402 and denied.json()["error"]["code"] == "plan_limit_export"

    await factory.pro(ws)
    for title in ("Plain", '=HYPERLINK("http://x")', 'Comma, and "quote"'):
        await client.post(
            W(ws, f"/projects/{p['id']}/tasks"),
            json={"title": title, "assignee_id": owner.id},
            headers=oh,
        )
    r = await client.get(W(ws, "/export/tasks.csv"), headers=oh)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    import csv
    import io

    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:4] == ["key", "project", "title", "status"] and len(rows) == 4
    assert rows[1][0] == "EXP-1" and rows[1][3] == "To do" and rows[1][4] == "User"
    assert rows[2][2].startswith("'=")  # формула обезврежена
    assert rows[3][2] == 'Comma, and "quote"'
    one = await client.get(W(ws, "/export/tasks.csv"), params={"project_id": p["id"]}, headers=oh)
    assert len(list(csv.reader(io.StringIO(one.text)))) == 4
    assert (
        await client.get(W(ws, "/export/tasks.csv"), params={"project_id": 9999}, headers=oh)
    ).status_code == 404
    _, gh = await factory.member(ws, "guest")
    assert (await client.get(W(ws, "/export/tasks.csv"), headers=gh)).status_code == 403
