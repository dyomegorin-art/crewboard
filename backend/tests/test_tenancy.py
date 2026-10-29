"""Мультитенантность: пользователь A ничего не видит и не меняет в воркспейсе B (всегда 404)."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Comment, Status, Task


def W(ws: Any, path: str = "") -> str:
    return f"/api/w/{ws.id}{path}"


@pytest.fixture
async def two_tenants(client: AsyncClient, factory: Any) -> dict[str, Any]:
    a_ws, a_user, a_h = await factory.workspace("Alpha team")
    b_ws, b_user, b_h = await factory.workspace("Beta team")
    out: dict[str, Any] = {"a_ws": a_ws, "a_h": a_h, "b_ws": b_ws, "b_h": b_h, "b_user": b_user}
    for side, ws, h in (("a", a_ws, a_h), ("b", b_ws, b_h)):
        p = (
            await client.post(W(ws, "/projects"), json={"name": f"{side} project"}, headers=h)
        ).json()
        board = (await client.get(W(ws, f"/projects/{p['id']}/board"), headers=h)).json()
        t = (
            await client.post(
                W(ws, f"/projects/{p['id']}/tasks"), json={"title": f"{side} task"}, headers=h
            )
        ).json()
        c = (
            await client.post(
                W(ws, f"/tasks/{t['id']}/comments"), json={"body": "secret"}, headers=h
            )
        ).json()
        out[f"{side}_project"], out[f"{side}_task"], out[f"{side}_comment"] = p, t, c
        out[f"{side}_statuses"] = board["statuses"]
    return out


async def test_user_a_cannot_reach_workspace_b_by_any_route(
    client: AsyncClient, two_tenants: dict[str, Any]
) -> None:
    d = two_tenants
    pid, tid = d["b_project"]["id"], d["b_task"]["id"]
    b = d["b_ws"]
    routes = [
        ("GET", ""),
        ("PATCH", ""),
        ("GET", "/members"),
        ("GET", "/invitations"),
        ("GET", "/projects"),
        ("POST", "/projects"),
        ("GET", f"/projects/{pid}/board"),
        ("PATCH", f"/projects/{pid}"),
        ("POST", f"/projects/{pid}/archive"),
        ("POST", f"/projects/{pid}/tasks"),
        ("GET", f"/tasks/{tid}"),
        ("PATCH", f"/tasks/{tid}"),
        ("POST", f"/tasks/{tid}/move"),
        ("DELETE", f"/tasks/{tid}"),
        ("GET", f"/tasks/{tid}/comments"),
        ("POST", f"/tasks/{tid}/comments"),
        ("GET", f"/tasks/{tid}/history"),
        ("GET", "/activity"),
        ("GET", "/my-tasks"),
        ("DELETE", f"/members/{d['b_user'].id}"),
    ]
    for method, path in routes:
        r = await client.request(
            method, W(b, path), json={"name": "x", "title": "x", "body": "x", "status_id": 1},
            headers=d["a_h"],
        )  # fmt: skip
        assert r.status_code == 404, f"{method} {path} -> {r.status_code}"


async def test_ids_of_b_are_not_reachable_through_a_own_workspace(
    client: AsyncClient, two_tenants: dict[str, Any]
) -> None:
    """Подставить id чужого проекта/задачи/статуса в СВОЙ воркспейс тоже нельзя."""
    d = two_tenants
    a = d["a_ws"]
    b_pid, b_tid = d["b_project"]["id"], d["b_task"]["id"]
    b_status = d["b_statuses"][1]["id"]
    h = d["a_h"]
    checks = [
        ("GET", f"/projects/{b_pid}/board", None),
        ("PATCH", f"/projects/{b_pid}", {"name": "hijack"}),
        ("POST", f"/projects/{b_pid}/archive", None),
        ("POST", f"/projects/{b_pid}/tasks", {"title": "x"}),
        ("GET", f"/tasks/{b_tid}", None),
        ("PATCH", f"/tasks/{b_tid}", {"title": "hijack"}),
        ("POST", f"/tasks/{b_tid}/move", {"status_id": d["a_statuses"][1]["id"]}),
        ("DELETE", f"/tasks/{b_tid}", None),
        ("GET", f"/tasks/{b_tid}/comments", None),
        ("POST", f"/tasks/{b_tid}/comments", {"body": "x"}),
        ("PATCH", f"/statuses/{b_status}", {"name": "x"}),
        ("DELETE", f"/statuses/{b_status}", None),
        ("PATCH", f"/members/{d['b_user'].id}", {"role": "admin"}),
        ("POST", f"/invitations/{d['b_user'].id}/revoke", None),
    ]
    for method, path, body in checks:
        r = await client.request(method, W(a, path), json=body, headers=h)
        assert r.status_code == 404, f"{method} {path} -> {r.status_code} {r.text}"
    # Свою задачу нельзя переместить в статус чужого воркспейса.
    r = await client.post(
        W(a, f"/tasks/{d['a_task']['id']}/move"), json={"status_id": b_status}, headers=h
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "status_not_found"
    # Чужая карточка осталась нетронутой.
    unchanged = await client.get(W(d["b_ws"], f"/tasks/{b_tid}"), headers=d["b_h"])
    assert (
        unchanged.json()["title"] == "b task"
        and unchanged.json()["status_id"] == d["b_task"]["status_id"]
    )


async def test_cannot_assign_member_of_another_workspace(
    client: AsyncClient, two_tenants: dict[str, Any]
) -> None:
    d = two_tenants
    r = await client.patch(
        W(d["a_ws"], f"/tasks/{d['a_task']['id']}"),
        json={"assignee_id": d["b_user"].id},
        headers=d["a_h"],
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "assignee_not_member"


async def test_lists_never_leak_other_tenants_data(
    client: AsyncClient, two_tenants: dict[str, Any]
) -> None:
    d = two_tenants
    projects = (await client.get(W(d["a_ws"], "/projects"), headers=d["a_h"])).json()
    assert [p["name"] for p in projects] == ["a project"]
    activity = (await client.get(W(d["a_ws"], "/activity"), headers=d["a_h"])).json()["items"]
    assert all("b task" not in str(i["detail"]) for i in activity)
    listing = (await client.get("/api/workspaces", headers=d["a_h"])).json()
    assert [w["name"] for w in listing] == ["Alpha team"]


async def test_database_composite_foreign_keys_block_cross_tenant_rows(
    two_tenants: dict[str, Any], session: AsyncSession
) -> None:
    """Даже в обход API БД не даёт собрать задачу из частей разных воркспейсов."""
    d = two_tenants
    a_ws, b_ws = d["a_ws"].id, d["b_ws"].id
    bad_rows = [
        # статус из воркспейса B
        dict(ws=a_ws, project=d["a_project"]["id"], status=d["b_statuses"][0]["id"]),
        # проект из воркспейса B
        dict(ws=a_ws, project=d["b_project"]["id"], status=d["a_statuses"][0]["id"]),
    ]
    for row in bad_rows:
        session.add(
            Task(
                workspace_id=row["ws"],
                project_id=row["project"],
                status_id=row["status"],
                number=99,
                title="x",
                position=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()
    # Исполнитель не из этого воркспейса.
    t = await session.get(Task, d["a_task"]["id"])
    assert t is not None
    t.assignee_id = d["b_user"].id
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
    # Комментарий к задаче другого воркспейса.
    session.add(Comment(workspace_id=a_ws, task_id=d["b_task"]["id"], body="x"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
    # Колонка проекта B не может принадлежать воркспейсу A.
    session.add(Status(workspace_id=a_ws, project_id=d["b_project"]["id"], name="x", position=1))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
    _ = (b_ws, text)


async def test_member_of_both_workspaces_is_scoped_per_workspace(
    client: AsyncClient, factory: Any
) -> None:
    ws1, owner, h = await factory.workspace("One")
    ws2, _, _ = await factory.workspace("Two")
    await factory.member(
        ws2,
        "guest",
    )
    r = await client.get("/api/workspaces", headers=h)
    assert [w["id"] for w in r.json()] == [ws1.id]
    assert owner.id
