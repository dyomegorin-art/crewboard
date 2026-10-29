from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityLog, Task


def W(ws: Any, path: str = "") -> str:
    return f"/api/w/{ws.id}{path}"


async def mk_project(
    client: AsyncClient, ws: Any, headers: Any, name: str = "Website"
) -> dict[str, Any]:
    r = await client.post(W(ws, "/projects"), json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def board(
    client: AsyncClient, ws: Any, headers: Any, pid: int, **params: Any
) -> dict[str, Any]:
    r = await client.get(W(ws, f"/projects/{pid}/board"), params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def mk_task(
    client: AsyncClient, ws: Any, headers: Any, pid: int, title: str = "Task", **extra: Any
) -> dict[str, Any]:
    r = await client.post(
        W(ws, f"/projects/{pid}/tasks"), json={"title": title, **extra}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_create_project_with_default_columns_and_key(
    client: AsyncClient, factory: Any
) -> None:
    ws, _, oh = await factory.workspace()
    p = await mk_project(client, ws, oh, "Website redesign")
    assert p["key"] == "WR" and p["task_count"] == 0
    b = await board(client, ws, oh, p["id"])
    assert [s["name"] for s in b["statuses"]] == ["To do", "In progress", "Done"]
    assert [s["is_done"] for s in b["statuses"]] == [False, False, True]
    p2 = await mk_project(client, ws, oh, "Website redesign")
    assert p2["key"] == "WR2"
    dup = await client.post(W(ws, "/projects"), json={"name": "Other", "key": "wr"}, headers=oh)
    assert dup.status_code == 409


async def test_task_numbers_are_sequential_per_project(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    a = await mk_project(client, ws, oh, "Alpha")
    b = await mk_project(client, ws, oh, "Beta")
    t1 = await mk_task(client, ws, oh, a["id"])
    t2 = await mk_task(client, ws, oh, a["id"])
    t3 = await mk_task(client, ws, oh, b["id"])
    assert (t1["key"], t2["key"], t3["key"]) == ("ALP-1", "ALP-2", "BET-1")
    assert t2["position"] > t1["position"]
    assert t1["position"] == 1024


async def test_update_task_and_history(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, owner, oh = await factory.workspace()
    p = await mk_project(client, ws, oh)
    t = await mk_task(client, ws, oh, p["id"], "Draft")
    r = await client.patch(
        W(ws, f"/tasks/{t['id']}"),
        json={
            "title": " Final ",
            "priority": "urgent",
            "due_date": "2026-03-01",
            "assignee_id": owner.id,
            "description": "**bold**",
        },
        headers=oh,
    )
    body = r.json()
    assert r.status_code == 200 and body["title"] == "Final" and body["priority"] == "urgent"
    assert body["due_date"] == "2026-03-01" and body["assignee_id"] == owner.id
    # Явный null снимает исполнителя и срок.
    r = await client.patch(
        W(ws, f"/tasks/{t['id']}"), json={"assignee_id": None, "due_date": None}, headers=oh
    )
    assert r.json()["assignee_id"] is None and r.json()["due_date"] is None
    hist = (await client.get(W(ws, f"/tasks/{t['id']}/history"), headers=oh)).json()
    assert [h["action"] for h in hist] == ["task.updated", "task.updated", "task.created"]
    assert "title" in hist[1]["detail"]["changes"]
    # Пустой PATCH ничего не пишет.
    await client.patch(W(ws, f"/tasks/{t['id']}"), json={}, headers=oh)
    assert len((await client.get(W(ws, f"/tasks/{t['id']}/history"), headers=oh)).json()) == 3
    assert (
        await client.patch(W(ws, f"/tasks/{t['id']}"), json={"title": ""}, headers=oh)
    ).status_code == 422


async def test_move_between_columns_and_inside_column(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    p = await mk_project(client, ws, oh)
    b = await board(client, ws, oh, p["id"])
    todo, doing, done = (s["id"] for s in b["statuses"])
    a = await mk_task(client, ws, oh, p["id"], "A")
    bb = await mk_task(client, ws, oh, p["id"], "B")
    c = await mk_task(client, ws, oh, p["id"], "C")

    # C → в начало колонки; позиция — среднее между 0 и первой карточкой.
    r = await client.post(
        W(ws, f"/tasks/{c['id']}/move"), json={"status_id": todo, "after_id": None}, headers=oh
    )
    assert r.json()["position"] == 512
    # B → между C и A.
    r = await client.post(
        W(ws, f"/tasks/{bb['id']}/move"), json={"status_id": todo, "after_id": c["id"]}, headers=oh
    )
    assert r.json()["position"] == (512 + 1024) // 2
    order = [t["title"] for t in (await board(client, ws, oh, p["id"]))["tasks"]]
    assert order == ["C", "B", "A"]
    # Другая колонка.
    r = await client.post(
        W(ws, f"/tasks/{a['id']}/move"), json={"status_id": done, "after_id": None}, headers=oh
    )
    assert r.json()["status_id"] == done and r.json()["position"] == 1024
    assert doing != done


async def test_position_gap_exhaustion_renumbers_column(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, _, oh = await factory.workspace()
    p = await mk_project(client, ws, oh)
    todo = (await board(client, ws, oh, p["id"]))["statuses"][0]["id"]
    a = await mk_task(client, ws, oh, p["id"], "A")
    b = await mk_task(client, ws, oh, p["id"], "B")
    c = await mk_task(client, ws, oh, p["id"], "C")
    # Прижимаем A и B вплотную: между ними зазора нет.
    ta = await session.get(Task, a["id"])
    tb = await session.get(Task, b["id"])
    assert ta and tb
    ta.position, tb.position = 100, 101
    await session.commit()
    r = await client.post(
        W(ws, f"/tasks/{c['id']}/move"), json={"status_id": todo, "after_id": a["id"]}, headers=oh
    )
    assert r.status_code == 200
    final = await board(client, ws, oh, p["id"])
    assert [t["title"] for t in final["tasks"]] == ["A", "C", "B"]
    assert [t["position"] for t in final["tasks"]] == sorted(t["position"] for t in final["tasks"])
    assert len({t["position"] for t in final["tasks"]}) == 3


async def test_move_validation(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    p1 = await mk_project(client, ws, oh, "One")
    p2 = await mk_project(client, ws, oh, "Two")
    t = await mk_task(client, ws, oh, p1["id"])
    other_status = (await board(client, ws, oh, p2["id"]))["statuses"][0]["id"]
    mine = (await board(client, ws, oh, p1["id"]))["statuses"][0]["id"]
    # Статус другого проекта того же воркспейса.
    r = await client.post(
        W(ws, f"/tasks/{t['id']}/move"), json={"status_id": other_status}, headers=oh
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "status_not_in_project"
    r = await client.post(
        W(ws, f"/tasks/{t['id']}/move"), json={"status_id": mine, "after_id": t["id"]}, headers=oh
    )
    assert r.status_code == 422
    r = await client.post(
        W(ws, f"/tasks/{t['id']}/move"), json={"status_id": mine, "after_id": 424242}, headers=oh
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_position"
    r = await client.post(
        W(ws, f"/projects/{p1['id']}/tasks"),
        json={"title": "x", "status_id": other_status},
        headers=oh,
    )
    assert r.status_code == 422


async def test_assignee_must_be_workspace_member(
    client: AsyncClient, factory: Any, make_user: Any
) -> None:
    ws, _, oh = await factory.workspace()
    p = await mk_project(client, ws, oh)
    stranger, _ = await make_user()
    guest, _ = await factory.member(ws, "guest")
    r = await client.post(
        W(ws, f"/projects/{p['id']}/tasks"),
        json={"title": "x", "assignee_id": stranger.id},
        headers=oh,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "assignee_not_member"
    t = await mk_task(client, ws, oh, p["id"])
    for uid in (stranger.id, guest.id):
        r = await client.patch(W(ws, f"/tasks/{t['id']}"), json={"assignee_id": uid}, headers=oh)
        assert r.status_code == 422


async def test_removed_member_tasks_become_unassigned(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    m, _ = await factory.member(ws, "member")
    p = await mk_project(client, ws, oh)
    t = await mk_task(client, ws, oh, p["id"], assignee_id=m.id)
    assert (await client.delete(W(ws, f"/members/{m.id}"), headers=oh)).status_code == 204
    assert (await client.get(W(ws, f"/tasks/{t['id']}"), headers=oh)).json()["assignee_id"] is None


async def test_board_filters_and_my_tasks(client: AsyncClient, factory: Any) -> None:
    ws, owner, oh = await factory.workspace()
    m, mh = await factory.member(ws, "member")
    p = await mk_project(client, ws, oh)
    p2 = await mk_project(client, ws, oh, "Second")
    await mk_task(client, ws, oh, p["id"], "mine-high", assignee_id=m.id, priority="high")
    await mk_task(client, ws, oh, p["id"], "mine-low", assignee_id=m.id, priority="low")
    await mk_task(client, ws, oh, p["id"], "owners", assignee_id=owner.id, priority="high")
    await mk_task(
        client, ws, oh, p2["id"], "other-project", assignee_id=m.id, due_date="2026-02-01"
    )

    titles = lambda b: sorted(t["title"] for t in b["tasks"])  # noqa: E731
    assert titles(await board(client, ws, mh, p["id"], mine="true")) == ["mine-high", "mine-low"]
    assert titles(await board(client, ws, mh, p["id"], priority="high")) == ["mine-high", "owners"]
    assert titles(await board(client, ws, mh, p["id"], assignee_id=owner.id)) == ["owners"]
    mine = (await client.get(W(ws, "/my-tasks"), headers=mh)).json()
    assert [t["title"] for t in mine] == ["other-project", "mine-high", "mine-low"]


async def test_comments(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    _, mh = await factory.member(ws, "member")
    p = await mk_project(client, ws, oh)
    t = await mk_task(client, ws, oh, p["id"])
    r = await client.post(
        W(ws, f"/tasks/{t['id']}/comments"), json={"body": "Looks good"}, headers=mh
    )
    assert r.status_code == 201 and r.json()["author_name"] == "User"
    assert (
        await client.post(W(ws, f"/tasks/{t['id']}/comments"), json={"body": ""}, headers=mh)
    ).status_code == 422
    lst = (await client.get(W(ws, f"/tasks/{t['id']}/comments"), headers=oh)).json()
    assert [c["body"] for c in lst] == ["Looks good"]
    hist = (await client.get(W(ws, f"/tasks/{t['id']}/history"), headers=oh)).json()
    assert hist[0]["action"] == "comment.added"


async def test_delete_task_and_archived_projects_are_read_only(
    client: AsyncClient, factory: Any
) -> None:
    ws, _, oh = await factory.workspace()
    p = await mk_project(client, ws, oh)
    t = await mk_task(client, ws, oh, p["id"])
    assert (await client.post(W(ws, f"/projects/{p['id']}/archive"), headers=oh)).json()[
        "archived"
    ] is True
    for method, path, body in [
        ("POST", f"/projects/{p['id']}/tasks", {"title": "x"}),
        ("PATCH", f"/tasks/{t['id']}", {"title": "x"}),
        ("POST", f"/tasks/{t['id']}/move", {"status_id": t["status_id"]}),
        ("DELETE", f"/tasks/{t['id']}", None),
        ("POST", f"/tasks/{t['id']}/comments", {"body": "x"}),
        ("PATCH", f"/projects/{p['id']}", {"name": "renamed"}),
    ]:
        r = await client.request(method, W(ws, path), json=body, headers=oh)
        assert r.status_code == 409 and r.json()["error"]["code"] == "project_archived", path
    # Чтение работает; из архива можно вернуть.
    assert (await client.get(W(ws, f"/tasks/{t['id']}"), headers=oh)).status_code == 200
    assert (await client.get(W(ws, "/projects"), headers=oh)).json() == []
    assert (
        len(
            (
                await client.get(
                    W(ws, "/projects"), params={"include_archived": "true"}, headers=oh
                )
            ).json()
        )
        == 1
    )
    assert (await client.post(W(ws, f"/projects/{p['id']}/unarchive"), headers=oh)).json()[
        "archived"
    ] is False
    assert (await client.delete(W(ws, f"/tasks/{t['id']}"), headers=oh)).status_code == 204
    assert (await client.get(W(ws, f"/tasks/{t['id']}"), headers=oh)).status_code == 404


async def test_free_plan_project_limit_and_fixed_columns(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    projects = [await mk_project(client, ws, oh, f"P{i}x") for i in range(3)]
    r = await client.post(W(ws, "/projects"), json={"name": "Fourth"}, headers=oh)
    assert r.status_code == 402 and r.json()["error"]["code"] == "plan_limit_projects"
    # Архивация освобождает слот; возврат из архива упирается в лимит.
    await client.post(W(ws, f"/projects/{projects[0]['id']}/archive"), headers=oh)
    assert (
        await client.post(W(ws, "/projects"), json={"name": "Fourth"}, headers=oh)
    ).status_code == 201
    r = await client.post(W(ws, f"/projects/{projects[0]['id']}/unarchive"), headers=oh)
    assert r.status_code == 402
    # Кастомные колонки — только Pro.
    r = await client.post(
        W(ws, f"/projects/{projects[1]['id']}/statuses"), json={"name": "Review"}, headers=oh
    )
    assert r.status_code == 402 and r.json()["error"]["code"] == "plan_limit_statuses"


async def test_pro_custom_statuses_crud(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws)
    p = await mk_project(client, ws, oh)
    r = await client.post(
        W(ws, f"/projects/{p['id']}/statuses"), json={"name": "Review"}, headers=oh
    )
    assert r.status_code == 201
    review = r.json()
    statuses = (await board(client, ws, oh, p["id"]))["statuses"]
    assert [s["name"] for s in statuses] == ["To do", "In progress", "Done", "Review"]

    ids = [statuses[0]["id"], review["id"], statuses[1]["id"], statuses[2]["id"]]
    r = await client.put(
        W(ws, f"/projects/{p['id']}/statuses/order"), json={"ids": ids}, headers=oh
    )
    assert [s["name"] for s in r.json()] == ["To do", "Review", "In progress", "Done"]
    assert (
        await client.put(
            W(ws, f"/projects/{p['id']}/statuses/order"), json={"ids": ids[:2]}, headers=oh
        )
    ).status_code == 422

    assert (
        await client.patch(
            W(ws, f"/statuses/{review['id']}"), json={"name": "QA", "is_done": True}, headers=oh
        )
    ).json()["name"] == "QA"
    t = await mk_task(client, ws, oh, p["id"], status_id=review["id"])
    r = await client.delete(W(ws, f"/statuses/{review['id']}"), headers=oh)
    assert r.status_code == 409 and r.json()["error"]["code"] == "status_not_empty"
    await client.delete(W(ws, f"/tasks/{t['id']}"), headers=oh)
    assert (await client.delete(W(ws, f"/statuses/{review['id']}"), headers=oh)).status_code == 204


async def test_member_cannot_manage_projects_or_statuses(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws)
    _, mh = await factory.member(ws, "member")
    p = await mk_project(client, ws, oh)
    assert (
        await client.post(W(ws, "/projects"), json={"name": "X"}, headers=mh)
    ).status_code == 403
    assert (
        await client.patch(W(ws, f"/projects/{p['id']}"), json={"name": "X"}, headers=mh)
    ).status_code == 403
    assert (await client.post(W(ws, f"/projects/{p['id']}/archive"), headers=mh)).status_code == 403
    assert (
        await client.post(W(ws, f"/projects/{p['id']}/statuses"), json={"name": "X"}, headers=mh)
    ).status_code == 403
    # А задачи и комментарии — могут.
    t = await mk_task(client, ws, mh, p["id"])
    assert (
        await client.post(W(ws, f"/tasks/{t['id']}/comments"), json={"body": "hi"}, headers=mh)
    ).status_code == 201


async def test_guest_sees_only_granted_projects_and_can_only_comment(
    client: AsyncClient, factory: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws)
    g, gh = await factory.member(ws, "guest")
    open_p = await mk_project(client, ws, oh, "Open")
    closed_p = await mk_project(client, ws, oh, "Closed")
    t_open = await mk_task(client, ws, oh, open_p["id"])
    t_closed = await mk_task(client, ws, oh, closed_p["id"])
    assert (await client.get(W(ws, "/projects"), headers=gh)).json() == []
    assert (
        await client.get(W(ws, f"/projects/{open_p['id']}/board"), headers=gh)
    ).status_code == 404

    r = await client.post(
        W(ws, f"/projects/{open_p['id']}/guests"), json={"user_id": g.id}, headers=oh
    )
    assert r.status_code == 204
    assert [p["name"] for p in (await client.get(W(ws, "/projects"), headers=gh)).json()] == [
        "Open"
    ]
    assert (await client.get(W(ws, f"/tasks/{t_open['id']}"), headers=gh)).status_code == 200
    assert (await client.get(W(ws, f"/tasks/{t_closed['id']}"), headers=gh)).status_code == 404
    assert (
        await client.post(W(ws, f"/tasks/{t_open['id']}/comments"), json={"body": "hi"}, headers=gh)
    ).status_code == 201
    assert (
        await client.patch(W(ws, f"/tasks/{t_open['id']}"), json={"title": "x"}, headers=gh)
    ).status_code == 403
    assert (
        await client.post(W(ws, f"/projects/{open_p['id']}/tasks"), json={"title": "x"}, headers=gh)
    ).status_code == 403
    # Выдать доступ можно только гостю.
    m, _ = await factory.member(ws, "member")
    assert (
        await client.post(
            W(ws, f"/projects/{open_p['id']}/guests"), json={"user_id": m.id}, headers=oh
        )
    ).status_code == 422
    assert (
        await client.delete(W(ws, f"/projects/{open_p['id']}/guests/{g.id}"), headers=oh)
    ).status_code == 204
    assert (await client.get(W(ws, f"/tasks/{t_open['id']}"), headers=gh)).status_code == 404


async def test_activity_feed_pagination_and_free_plan_window(
    client: AsyncClient, factory: Any, session: AsyncSession, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    p = await mk_project(client, ws, oh)
    for i in range(4):
        clock.advance(minutes=1)
        await mk_task(client, ws, oh, p["id"], f"T{i}")
    page1 = (await client.get(W(ws, "/activity"), params={"limit": 3}, headers=oh)).json()
    assert len(page1["items"]) == 3 and page1["next_cursor"]
    page2 = (
        await client.get(
            W(ws, "/activity"), params={"limit": 3, "cursor": page1["next_cursor"]}, headers=oh
        )
    ).json()
    ids = [i["id"] for i in page1["items"] + page2["items"]]
    assert len(ids) == len(set(ids)) == 5 and page2["next_cursor"] is None
    assert [i["action"] for i in page1["items"]][0] == "task.created"
    assert (
        await client.get(W(ws, "/activity"), params={"cursor": "garbage"}, headers=oh)
    ).status_code == 422

    # На Free записи старше 7 дней не отдаются, на Pro — отдаются.
    clock.advance(days=8)
    old = (await client.get(W(ws, "/activity"), headers=oh)).json()["items"]
    assert old == []
    await factory.pro(ws, now=clock.current)
    assert len((await client.get(W(ws, "/activity"), headers=oh)).json()["items"]) == 5
    _ = (session, ActivityLog, select, timedelta)


async def test_activity_hides_access_denials_from_members(
    client: AsyncClient, factory: Any
) -> None:
    ws, _, oh = await factory.workspace()
    _, mh = await factory.member(ws, "member")
    await client.post(W(ws, "/projects"), json={"name": "X"}, headers=mh)  # 403 → access_denied
    owner_view = (await client.get(W(ws, "/activity"), headers=oh)).json()["items"]
    member_view = (await client.get(W(ws, "/activity"), headers=mh)).json()["items"]
    assert any(i["action"] == "access_denied" for i in owner_view)
    assert not any(i["action"] == "access_denied" for i in member_view)


async def test_read_only_workspace_blocks_every_write_but_not_reads(
    client: AsyncClient, factory: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws)
    p = await mk_project(client, ws, oh)
    t = await mk_task(client, ws, oh, p["id"])
    await factory.set_status(ws, "read_only")
    writes = [
        ("POST", "/projects", {"name": "n"}),
        ("PATCH", f"/projects/{p['id']}", {"name": "n"}),
        ("POST", f"/projects/{p['id']}/tasks", {"title": "x"}),
        ("PATCH", f"/tasks/{t['id']}", {"title": "x"}),
        ("POST", f"/tasks/{t['id']}/move", {"status_id": t["status_id"]}),
        ("DELETE", f"/tasks/{t['id']}", None),
        ("POST", f"/tasks/{t['id']}/comments", {"body": "x"}),
        ("POST", "/invitations", {"email": "a@example.com", "role": "member"}),
        ("PATCH", "", {"name": "new"}),
    ]
    for method, path, body in writes:
        r = await client.request(method, W(ws, path), json=body, headers=oh)
        assert r.status_code == 402 and r.json()["error"]["code"] == "workspace_read_only", path
    for path in (
        "",
        "/projects",
        f"/projects/{p['id']}/board",
        f"/tasks/{t['id']}",
        "/members",
        "/activity",
        "/my-tasks",
    ):
        assert (await client.get(W(ws, path), headers=oh)).status_code == 200, path
