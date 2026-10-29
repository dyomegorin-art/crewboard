import asyncio
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityLog, Invitation, Membership, OutboxCommand


def W(ws: Any, path: str = "") -> str:
    return f"/api/w/{ws.id}{path}"


async def test_non_member_gets_404_not_403(
    client: AsyncClient, factory: Any, make_user: Any
) -> None:
    ws, _, _ = await factory.workspace()
    _, outsider = await make_user()
    for path in ("", "/members", "/invitations"):
        r = await client.get(W(ws, path), headers=outsider)
        assert r.status_code == 404, path
    # Несуществующий воркспейс неотличим от чужого.
    r = await client.get("/api/w/999999", headers=outsider)
    assert r.status_code == 404 and r.json()["error"]["code"] == "workspace_not_found"


async def test_workspace_detail_has_role_permissions_and_plan(
    client: AsyncClient, factory: Any
) -> None:
    ws, _, owner = await factory.workspace()
    member, mh = await factory.member(ws, "member")
    r = await client.get(W(ws), headers=mh)
    body = r.json()
    assert body["role"] == "member" and body["plan"] == "free" and body["status"] == "active"
    assert "tasks.write" in body["permissions"] and "billing.manage" not in body["permissions"]
    assert (await client.get(W(ws), headers=owner)).json()["paid_seats_used"] == 2


async def test_member_cannot_do_what_admin_can_and_denial_is_logged(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, _, _ = await factory.workspace()
    await factory.pro(ws, seats=5)
    _, mh = await factory.member(ws, "member")
    _, ah = await factory.member(ws, "admin")
    r = await client.post(
        W(ws, "/invitations"), json={"email": "new@example.com", "role": "member"}, headers=mh
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    denied = (
        await session.scalars(select(ActivityLog).where(ActivityLog.action == "access_denied"))
    ).all()
    assert len(denied) == 1
    assert denied[0].detail["permission"] == "members.invite" and denied[0].workspace_id == ws.id
    ok = await client.post(
        W(ws, "/invitations"), json={"email": "new@example.com", "role": "member"}, headers=ah
    )
    assert ok.status_code == 201


async def test_rename_and_delete_only_owner(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    _, ah = await factory.member(ws, "admin")
    assert (await client.patch(W(ws), json={"name": "X"}, headers=ah)).status_code == 403
    assert (await client.patch(W(ws), json={"name": "Renamed"}, headers=oh)).status_code == 200
    assert (await client.delete(W(ws), headers=ah)).status_code == 403
    assert (await client.delete(W(ws), headers=oh)).status_code == 204
    assert (await client.get(W(ws), headers=oh)).status_code == 404


async def test_delete_blocked_while_pro_active(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws)
    r = await client.delete(W(ws), headers=oh)
    assert r.status_code == 409 and r.json()["error"]["code"] == "subscription_active"


async def test_members_list_and_role_change(client: AsyncClient, factory: Any) -> None:
    ws, owner, oh = await factory.workspace()
    m, _ = await factory.member(ws, "member")
    listing = (await client.get(W(ws, "/members"), headers=oh)).json()
    assert [x["role"] for x in listing] == ["owner", "member"] and listing[0]["is_owner"]
    r = await client.patch(W(ws, f"/members/{m.id}"), json={"role": "admin"}, headers=oh)
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # Роль owner меняется только передачей владения.
    r = await client.patch(W(ws, f"/members/{owner.id}"), json={"role": "member"}, headers=oh)
    assert r.status_code == 403
    r = await client.patch(W(ws, f"/members/{m.id}"), json={"role": "owner"}, headers=oh)
    assert r.status_code == 403
    r = await client.patch(W(ws, f"/members/{m.id}"), json={"role": "guest"}, headers=oh)
    assert r.status_code == 422 and r.json()["error"]["code"] == "role_change_not_supported"
    assert (
        await client.patch(W(ws, "/members/9999"), json={"role": "admin"}, headers=oh)
    ).status_code == 404


async def test_remove_member_rules(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, owner, oh = await factory.workspace()
    a, ah = await factory.member(ws, "member")
    b, bh = await factory.member(ws, "member")
    # Участник не удаляет других, но может выйти сам.
    assert (await client.delete(W(ws, f"/members/{b.id}"), headers=ah)).status_code == 403
    assert (await client.delete(W(ws, f"/members/{a.id}"), headers=ah)).status_code == 204
    assert (await client.delete(W(ws, f"/members/{owner.id}"), headers=oh)).status_code == 409
    assert (await client.delete(W(ws, f"/members/{b.id}"), headers=oh)).status_code == 204
    left = (await session.scalars(select(Membership).where(Membership.workspace_id == ws.id))).all()
    assert [m.user_id for m in left] == [owner.id]
    # Бывший участник теперь «чужой».
    assert (await client.get(W(ws), headers=ah)).status_code == 404


async def test_ownership_transfer(client: AsyncClient, factory: Any) -> None:
    ws, owner, oh = await factory.workspace()
    m, mh = await factory.member(ws, "member")
    g, _ = await factory.member(ws, "guest")
    assert (
        await client.post(W(ws, "/ownership-transfer"), json={"user_id": m.id}, headers=mh)
    ).status_code == 403
    assert (
        await client.post(W(ws, "/ownership-transfer"), json={"user_id": g.id}, headers=oh)
    ).status_code == 422
    assert (
        await client.post(W(ws, "/ownership-transfer"), json={"user_id": owner.id}, headers=oh)
    ).status_code == 422
    assert (
        await client.post(W(ws, "/ownership-transfer"), json={"user_id": m.id}, headers=oh)
    ).status_code == 204
    roles = {
        x["user_id"]: x["role"] for x in (await client.get(W(ws, "/members"), headers=mh)).json()
    }
    assert roles[m.id] == "owner" and roles[owner.id] == "admin"


# --- Приглашения ---------------------------------------------------------------------


async def invite(
    client: AsyncClient, ws: Any, headers: Any, email: str, role: str = "member", **extra: Any
):  # type: ignore[no-untyped-def]
    return await client.post(
        W(ws, "/invitations"), json={"email": email, "role": role, **extra}, headers=headers
    )


async def last_invite_link(session: AsyncSession) -> str:
    row = await session.scalars(
        select(OutboxCommand)
        .where(OutboxCommand.command == "SEND_EMAIL")
        .order_by(OutboxCommand.id.desc())
    )
    body: str = row.first().payload["body"]  # type: ignore[union-attr]
    return body.split("/invite/")[1].split()[0]


async def test_invite_accept_flow(
    client: AsyncClient, factory: Any, session: AsyncSession, make_user: Any
) -> None:
    ws, _, oh = await factory.workspace()
    r = await invite(client, ws, oh, "Newbie@Example.com")
    assert (
        r.status_code == 201
        and r.json()["status"] == "pending"
        and r.json()["email"] == "newbie@example.com"
    )
    token = await last_invite_link(session)

    pub = await client.get(f"/api/invitations/{token}")
    assert pub.json()["workspace_name"] == "Acme" and pub.json()["status"] == "pending"
    assert (await client.get("/api/invitations/bogus")).status_code == 404

    # Принять может только пользователь с тем же email.
    _, other = await make_user("other@example.com")
    assert (await client.post(f"/api/invitations/{token}/accept", headers=other)).status_code == 403
    assert (await client.post(f"/api/invitations/{token}/accept")).status_code == 401
    _, nh = await make_user("newbie@example.com", verified=False)
    r = await client.post(f"/api/invitations/{token}/accept", headers=nh)
    assert r.status_code == 200 and r.json()["workspace_id"] == ws.id
    assert (await client.get(W(ws), headers=nh)).json()["role"] == "member"
    # Повторное принятие.
    assert (await client.post(f"/api/invitations/{token}/accept", headers=nh)).status_code == 409


async def test_invite_requires_verified_inviter(
    client: AsyncClient, factory: Any, make_user: Any
) -> None:
    owner = await make_user(verified=False)
    ws, _, oh = await factory.workspace(owner=owner)
    r = await invite(client, ws, oh, "x@example.com")
    assert r.status_code == 403 and r.json()["error"]["code"] == "email_not_verified"


async def test_invite_revoke_resend_and_expiry(
    client: AsyncClient, factory: Any, session: AsyncSession, make_user: Any, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    _, nh = await make_user("n@example.com")
    r = await invite(client, ws, oh, "n@example.com")
    inv_id = r.json()["id"]
    old = await last_invite_link(session)
    assert (await invite(client, ws, oh, "n@example.com")).status_code == 409

    resent = await client.post(W(ws, f"/invitations/{inv_id}/resend"), headers=oh)
    assert resent.status_code == 200
    new = await last_invite_link(session)
    assert new != old
    assert (await client.post(f"/api/invitations/{old}/accept", headers=nh)).status_code == 404

    clock.advance(days=8)
    assert (await client.get(f"/api/invitations/{new}")).json()["status"] == "expired"
    assert (await client.post(f"/api/invitations/{new}/accept", headers=nh)).status_code == 410
    listing = (await client.get(W(ws, "/invitations"), headers=oh)).json()
    assert listing[0]["status"] == "expired"
    # Повторная отправка продлевает срок.
    assert (
        await client.post(W(ws, f"/invitations/{inv_id}/resend"), headers=oh)
    ).status_code == 200
    token = await last_invite_link(session)
    assert (await client.post(f"/api/invitations/{token}/accept", headers=nh)).status_code == 200


async def test_revoked_invitation_cannot_be_accepted(
    client: AsyncClient, factory: Any, session: AsyncSession, make_user: Any
) -> None:
    ws, _, oh = await factory.workspace()
    _, nh = await make_user("n@example.com")
    inv_id = (await invite(client, ws, oh, "n@example.com")).json()["id"]
    token = await last_invite_link(session)
    assert (
        await client.post(W(ws, f"/invitations/{inv_id}/revoke"), headers=oh)
    ).status_code == 204
    assert (await client.post(f"/api/invitations/{token}/accept", headers=nh)).status_code == 410
    assert (
        await client.post(W(ws, f"/invitations/{inv_id}/resend"), headers=oh)
    ).status_code == 409
    # Приглашение чужого воркспейса не видно.
    ws2, _, oh2 = await factory.workspace("Other")
    assert (
        await client.post(W(ws2, f"/invitations/{inv_id}/revoke"), headers=oh2)
    ).status_code == 404


async def test_invite_existing_member_and_owner_role_rejected(
    client: AsyncClient, factory: Any
) -> None:
    ws, owner, oh = await factory.workspace()
    assert (await invite(client, ws, oh, owner.email)).status_code == 409
    assert (await invite(client, ws, oh, "x@example.com", "owner")).status_code == 422


async def test_free_plan_limits_members_and_guests(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    await factory.member(ws, "member")
    assert (
        await invite(client, ws, oh, "a@example.com")
    ).status_code == 201  # 3-е место занято приглашением
    r = await invite(client, ws, oh, "b@example.com")
    assert r.status_code == 402 and r.json()["error"]["code"] == "plan_limit_members"
    r = await invite(client, ws, oh, "g@example.com", "guest")
    assert r.status_code == 402 and r.json()["error"]["code"] == "plan_limit_guests"
    prev = await client.post(
        W(ws, "/invitations/preview"), json={"email": "b@example.com"}, headers=oh
    )
    assert prev.status_code == 402


async def test_pro_invite_free_seat_needs_no_payment(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws, seats=3)
    prev = await client.post(
        W(ws, "/invitations/preview"), json={"email": "a@example.com"}, headers=oh
    )
    assert prev.json() == {
        "requires_payment": False,
        "amount": 0,
        "currency": "RUB",
        "seats_after": 3,
    }
    r = await invite(client, ws, oh, "a@example.com")
    assert (
        r.status_code == 201 and r.json()["status"] == "pending" and r.json()["invoice_id"] is None
    )


async def test_pro_invite_beyond_seats_requires_confirmed_prorated_payment(
    client: AsyncClient, factory: Any, session: AsyncSession
) -> None:
    from datetime import UTC, datetime

    ws, _, oh = await factory.workspace()
    await factory.pro(ws, seats=1)  # занято 1 (owner) из 1
    prev = (
        await client.post(
            W(ws, "/invitations/preview"), json={"email": "a@example.com"}, headers=oh
        )
    ).json()
    assert prev["requires_payment"] and 0 < prev["amount"] <= 490_00 and prev["seats_after"] == 2

    # Без подтверждения суммы приглашение не создаётся.
    r = await invite(client, ws, oh, "a@example.com")
    assert r.status_code == 409 and r.json()["error"]["code"] == "payment_confirmation_required"
    assert r.json()["error"]["details"]["amount"] == prev["amount"]
    assert (await invite(client, ws, oh, "a@example.com", confirm_amount=1)).status_code == 409

    r = await invite(client, ws, oh, "a@example.com", confirm_amount=prev["amount"])
    assert (
        r.status_code == 201
        and r.json()["status"] == "awaiting_payment"
        and r.json()["amount"] == prev["amount"]
    )
    # Письмо с приглашением не отправлено, пока место не оплачено; доплата поставлена на списание.
    cmds = [
        c.command
        for c in (await session.scalars(select(OutboxCommand).order_by(OutboxCommand.id))).all()
    ]
    assert cmds == ["CHARGE_INVOICE"]
    inv = await session.scalar(select(Invitation))
    assert inv is not None and inv.invoice_id == r.json()["invoice_id"]
    _ = datetime, UTC


async def test_concurrent_invites_never_share_one_free_seat(
    client: AsyncClient, factory: Any, session: AsyncSession, clock: Any
) -> None:
    from app.models import Invoice

    ws, _, oh = await factory.workspace()
    await factory.pro(ws, seats=2, now=clock.current)  # owner + одно свободное место
    amount = (
        await client.post(
            W(ws, "/invitations/preview"), json={"email": "x@example.com"}, headers=oh
        )
    ).json()
    assert not amount["requires_payment"]
    price = 490_00
    full = price  # начало периода: остаток равен всему периоду
    results = await asyncio.gather(
        *[invite(client, ws, oh, f"u{i}@example.com", confirm_amount=full) for i in range(3)]
    )
    assert [r.status_code for r in results] == [201, 201, 201]
    invoices = (await session.scalars(select(Invoice).order_by(Invoice.id))).all()
    # Ровно одно приглашение заняло свободное место; остальные получили по счёту за новое.
    assert sorted(i.seats for i in invoices if i.seats) == [3, 4]
    free = [r for r in results if r.json()["invoice_id"] is None]
    assert len(free) == 1


async def test_concurrent_paid_invites_are_priced_sequentially(
    client: AsyncClient, factory: Any, session: AsyncSession, clock: Any
) -> None:
    from app.models import Invoice

    ws, _, oh = await factory.workspace()
    await factory.pro(ws, seats=1, now=clock.current)
    amount = (
        await client.post(
            W(ws, "/invitations/preview"), json={"email": "p@example.com"}, headers=oh
        )
    ).json()["amount"]
    results = await asyncio.gather(
        *[invite(client, ws, oh, f"p{i}@example.com", confirm_amount=amount) for i in range(3)]
    )
    assert sorted(r.status_code for r in results).count(201) == 3
    invoices = (await session.scalars(select(Invoice).order_by(Invoice.id))).all()
    # Каждое приглашение — свой счёт на своё место: 2-е, 3-е и 4-е (seats_after = 2, 3, 4).
    assert sorted(i.seats for i in invoices if i.seats) == [2, 3, 4]
