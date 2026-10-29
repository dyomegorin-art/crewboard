import asyncio
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import (
    ActivityLog,
    Invitation,
    Invoice,
    InvoiceStatus,
    Membership,
    OutboxCommand,
    PaymentMethod,
    Project,
    RawWebhookDelivery,
    SubStatus,
    SystemIssue,
    Task,
    WebhookEvent,
)

from .helpers import CARD_DECLINED, CARD_LATER, CARD_OK, Stack, W, invoices_of, issues, sub_of


@pytest.fixture
def stack(client: AsyncClient, clock: Any) -> Stack:
    return Stack(client, clock)


async def upgrade(
    client: AsyncClient,
    stack: Stack,
    ws: Any,
    headers: Any,
    card: str = CARD_OK,
    period: str = "month",
) -> dict[str, Any]:
    r = await client.post(W(ws, "/billing/checkout"), json={"period": period}, headers=headers)
    assert r.status_code == 200, r.text
    await stack.pay(r.json()["checkout_url"], card)
    return r.json()  # type: ignore[no-any-return]


# --- Апгрейд -------------------------------------------------------------------------


async def test_upgrade_changes_state_only_by_webhook(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, owner, oh = await factory.workspace()
    await factory.member(ws, "member")
    r = await client.post(W(ws, "/billing/checkout"), json={"period": "month"}, headers=oh)
    inv_id, url = r.json()["invoice_id"], r.json()["checkout_url"]
    assert "/mock-checkout/mockpay_" in url
    (invoice,) = await invoices_of(ws)
    assert invoice.amount == 2 * 490_00 and invoice.status == InvoiceStatus.open  # 2 места

    # Пока вебхука нет, подписка Free; страница возврата видит «ожидаем».
    st = (await client.get(W(ws, f"/billing/invoices/{inv_id}/status"), headers=oh)).json()
    assert (st["invoice_status"], st["plan"]) == ("open", "free")

    page = (await client.get(f"/api/mock/checkout/{url.rsplit('/', 1)[1]}")).json()
    assert (
        page["amount"] == 98000
        and page["workspace_name"] == "Acme"
        and page["lines"][0]["quantity"] == 2
    )
    pay = await client.post(
        f"/api/mock/checkout/{url.rsplit('/', 1)[1]}/pay", json={"card_number": CARD_OK}
    )
    assert pay.json()["event"] == "payment.succeeded"
    assert (await sub_of(ws)).plan == "free"  # платёж подтверждён, но вебхук ещё в outbox
    await stack.outbox()

    sub = await sub_of(ws)
    assert (sub.plan, sub.status, sub.seats, sub.billing_period) == ("pro", "active", 2, "month")
    assert (
        sub.current_period_start == clock.current
        and sub.current_period_end == clock.current.replace(month=2)
    )
    st = (await client.get(W(ws, f"/billing/invoices/{inv_id}/status"), headers=oh)).json()
    assert (st["invoice_status"], st["subscription_status"], st["plan"]) == (
        "paid",
        "active",
        "pro",
    )

    async with SessionLocal() as s:
        pm = await s.get(PaymentMethod, sub.payment_method_id)
        assert (
            pm is not None
            and (pm.brand, pm.last4) == ("Visa", "4242")
            and pm.provider_token.startswith("mock_pm_ok_")
        )
        actions = (
            await s.scalars(select(ActivityLog.action).where(ActivityLog.action.like("billing.%")))
        ).all()
    assert "billing.payment_succeeded" in actions and "billing.checkout_started" in actions

    overview = (await client.get(W(ws, "/billing"), headers=oh)).json()
    assert overview["card"] == {"brand": "Visa", "last4": "4242"}
    assert overview["seats_paid"] == 2 and overview["next_charge_amount"] == 98000
    # Нельзя оформить повторно.
    again = await client.post(W(ws, "/billing/checkout"), json={"period": "year"}, headers=oh)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_subscribed"


async def test_yearly_upgrade_charges_year_price(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh, period="year")
    sub = await sub_of(ws)
    assert sub.billing_period == "year" and sub.current_period_end == clock.current.replace(
        year=2027
    )
    assert (await invoices_of(ws))[0].amount == 4900_00


async def test_declined_card_keeps_workspace_on_free(
    client: AsyncClient, factory: Any, stack: Stack
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh, card=CARD_DECLINED)
    sub = await sub_of(ws)
    assert sub.plan == "free" and sub.status == "active" and sub.payment_method_id is None
    assert (await invoices_of(ws))[0].status == InvoiceStatus.failed
    # Можно попробовать снова с другой картой.
    await upgrade(client, stack, ws, oh)
    assert (await sub_of(ws)).plan == "pro"


async def test_mock_checkout_validation(client: AsyncClient, factory: Any, stack: Stack) -> None:
    ws, _, oh = await factory.workspace()
    r = await client.post(W(ws, "/billing/checkout"), json={"period": "month"}, headers=oh)
    sid = r.json()["checkout_url"].rsplit("/", 1)[1]
    assert (await client.get("/api/mock/checkout/nope")).status_code == 404
    assert (
        await client.post(
            f"/api/mock/checkout/{sid}/pay", json={"card_number": "1111 1111 1111 1111"}
        )
    ).status_code == 422
    await stack.pay(r.json()["checkout_url"])
    assert (
        await client.post(f"/api/mock/checkout/{sid}/pay", json={"card_number": CARD_OK})
    ).status_code == 409


async def test_billing_permissions(client: AsyncClient, factory: Any, make_user: Any) -> None:
    ws, _, oh = await factory.workspace()
    _, ah = await factory.member(ws, "admin")
    _, mh = await factory.member(ws, "member")
    _, outsider = await make_user()
    assert (await client.get(W(ws, "/billing"), headers=mh)).status_code == 403
    assert (
        await client.get(W(ws, "/billing"), headers=ah)
    ).status_code == 200  # admin только читает
    assert (await client.get(W(ws, "/billing/invoices"), headers=ah)).status_code == 200
    for path in ("/billing/checkout", "/billing/trial", "/billing/cancel", "/billing/resume"):
        r = await client.post(W(ws, path), json={"period": "month"}, headers=ah)
        assert r.status_code == 403, path
    assert (await client.get(W(ws, "/billing"), headers=outsider)).status_code == 404


async def test_unverified_owner_cannot_pay(
    client: AsyncClient, factory: Any, make_user: Any
) -> None:
    owner = await make_user(verified=False)
    ws, _, oh = await factory.workspace(owner=owner)
    for path in ("/billing/checkout", "/billing/trial"):
        r = await client.post(W(ws, path), json={"period": "month"}, headers=oh)
        assert r.status_code == 403 and r.json()["error"]["code"] == "email_not_verified"


# --- Trial -----------------------------------------------------------------------------


async def test_trial_without_card_then_downgrade_archives_extra_projects(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, owner, oh = await factory.workspace()
    r = await client.post(W(ws, "/billing/trial"), json={"period": "month"}, headers=oh)
    assert r.status_code == 200 and r.json()["status"] == "trialing" and r.json()["plan"] == "pro"
    sub = await sub_of(ws)
    assert (
        sub.trial_end == clock.current + timedelta(days=14)
        and sub.current_period_end == sub.trial_end
    )
    again = await client.post(W(ws, "/billing/trial"), json={"period": "month"}, headers=oh)
    assert again.status_code == 409

    # Во время trial доступны возможности Pro: 5 проектов.
    pids = []
    for i in range(5):
        clock.advance(minutes=1)
        p = await client.post(W(ws, "/projects"), json={"name": f"Proj{i}x"}, headers=oh)
        assert p.status_code == 201
        pids.append(p.json()["id"])
        await client.post(
            W(ws, f"/projects/{p.json()['id']}/tasks"), json={"title": "t"}, headers=oh
        )
    # Свежее движение в самом первом проекте: он должен остаться среди «3 последних изменённых».
    clock.advance(minutes=1)
    await client.post(W(ws, f"/projects/{pids[0]}/tasks"), json={"title": "fresh"}, headers=oh)

    # За 3 дня до конца trial — письмо (один раз).
    clock.current = sub.trial_end - timedelta(days=3, minutes=-1)
    await stack.tick()
    await stack.tick()
    reminders = [e for e in stack.emails if "заканчивается" in e[1]]
    assert len(reminders) == 1 and "Карта не привязана" in reminders[0][2]

    clock.current = sub.trial_end + timedelta(minutes=1)
    stats = await stack.tick()
    assert stats["downgrades"] == 1
    sub = await sub_of(ws)
    assert (sub.plan, sub.status, sub.seats) == ("free", "canceled", 0)

    async with SessionLocal() as s:
        projects = (await s.scalars(select(Project).order_by(Project.id))).all()
        archived = {p.id for p in projects if p.archived_at is not None}
        assert len(projects) == 5 and len(archived) == 2  # ничего не удалено
        assert pids[0] not in archived and pids[4] not in archived and pids[3] not in archived
        assert await s.scalar(select(func.count()).select_from(Task)) == 6  # задачи на месте
    # Архивные проекты читаются, но не редактируются.
    assert (await client.get(W(ws, f"/projects/{pids[1]}/board"), headers=oh)).status_code == 200
    r = await client.post(W(ws, f"/projects/{pids[1]}/tasks"), json={"title": "x"}, headers=oh)
    assert r.status_code == 409 and r.json()["error"]["code"] == "project_archived"
    assert any("Free" in e[1] for e in stack.emails)


async def test_downgrade_keeps_extra_members_but_blocks_new_invites(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws, seats=5, now=clock.current)
    for _ in range(4):
        await factory.member(ws, "member")
    await factory.member(ws, "guest")
    assert (await client.post(W(ws, "/billing/cancel"), headers=oh)).json()[
        "cancel_at_period_end"
    ] is True
    clock.advance(days=31)
    await stack.tick()
    assert (await sub_of(ws)).plan == "free"
    async with SessionLocal() as s:
        count = await s.scalar(
            select(func.count()).select_from(Membership).where(Membership.workspace_id == ws.id)
        )
    assert count == 6  # никого не удалили
    for role in ("member", "guest"):
        r = await client.post(
            W(ws, "/invitations"), json={"email": f"{role}@example.com", "role": role}, headers=oh
        )
        assert r.status_code == 402 and r.json()["error"]["code"] in (
            "plan_limit_members",
            "plan_limit_guests",
        )
    assert (await client.get(W(ws, "/members"), headers=oh)).status_code == 200


async def test_trial_with_card_is_charged_at_the_end(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await client.post(W(ws, "/billing/trial"), json={"period": "month"}, headers=oh)
    r = await client.post(W(ws, "/billing/card/checkout"), json={}, headers=oh)
    await stack.pay(r.json()["checkout_url"])
    sub = await sub_of(ws)
    assert sub.status == "trialing" and sub.payment_method_id is not None  # карта есть, trial идёт
    clock.current = sub.trial_end + timedelta(minutes=1)
    await stack.tick()
    sub = await sub_of(ws)
    assert (sub.status, sub.plan) == ("active", "pro") and sub.trial_end is None
    assert sub.current_period_start == (await sub_of(ws)).current_period_start
    renewals = [i for i in await invoices_of(ws) if i.kind == "renewal"]
    assert (
        len(renewals) == 1
        and renewals[0].status == InvoiceStatus.paid
        and renewals[0].amount == 490_00
    )


async def test_pay_early_during_trial_converts_to_paid(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await client.post(W(ws, "/billing/trial"), json={"period": "month"}, headers=oh)
    clock.advance(days=2)
    await upgrade(client, stack, ws, oh)
    sub = await sub_of(ws)
    assert (sub.status, sub.trial_end) == (
        "active",
        None,
    ) and sub.current_period_start == clock.current


# --- Места и доплата -------------------------------------------------------------------


async def test_adding_a_seat_mid_period_charges_proration_and_unlocks_invite(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh)  # 1 место, период 15 янв — 15 фев (31 день)
    clock.advance(days=10)
    remaining = timedelta(days=21)
    expected = round(490_00 * remaining / timedelta(days=31))

    prev = (
        await client.post(
            W(ws, "/invitations/preview"), json={"email": "new@example.com"}, headers=oh
        )
    ).json()
    assert prev == {
        "requires_payment": True,
        "amount": expected,
        "currency": "RUB",
        "seats_after": 2,
    }
    r = await client.post(
        W(ws, "/invitations"),
        json={"email": "new@example.com", "confirm_amount": expected},
        headers=oh,
    )
    assert r.json()["status"] == "awaiting_payment"
    assert not any("Приглашение" in s for _, s, _ in stack.emails)

    await stack.outbox()  # списание с сохранённой карты → вебхук → место оплачено
    assert (await sub_of(ws)).seats == 2
    inv = [i for i in await invoices_of(ws) if i.kind == "proration"][0]
    assert inv.status == InvoiceStatus.paid and inv.amount == expected
    assert any("Приглашение" in s and to == "new@example.com" for to, s, _ in stack.emails)
    listing = (await client.get(W(ws, "/invitations"), headers=oh)).json()
    assert listing[0]["status"] == "pending"

    # Приглашённый принимает по ссылке из письма.
    link = (
        [b for to, _, b in stack.emails if to == "new@example.com"][-1]
        .split("/invite/")[1]
        .split()[0]
    )
    from .conftest import Factory  # noqa: F401

    async with SessionLocal() as s:
        from datetime import UTC, datetime

        from app.models import User
        from app.security import create_token

        u = User(
            email="new@example.com",
            name="New",
            password_hash="x",
            email_verified_at=datetime.now(UTC),
        )
        s.add(u)
        await s.commit()
        token = create_token(u.id, "access", datetime.now(UTC))
    ok = await client.post(
        f"/api/invitations/{link}/accept", headers={"Authorization": f"Bearer {token}"}
    )
    assert ok.status_code == 200
    assert (await client.get(W(ws, "/billing"), headers=oh)).json()["seats_used"] == 2


async def test_failed_seat_payment_leaves_invitation_awaiting(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh, card=CARD_LATER)  # 9995: дальше автосписания отклоняются
    amount = (
        await client.post(
            W(ws, "/invitations/preview"), json={"email": "n@example.com"}, headers=oh
        )
    ).json()["amount"]
    await client.post(
        W(ws, "/invitations"), json={"email": "n@example.com", "confirm_amount": amount}, headers=oh
    )
    await stack.outbox()
    assert (await sub_of(ws)).seats == 1  # место не добавлено
    inv = [i for i in await invoices_of(ws) if i.kind == "proration"][0]
    assert inv.status == InvoiceStatus.failed
    assert (await client.get(W(ws, "/invitations"), headers=oh)).json()[0][
        "status"
    ] == "awaiting_payment"
    assert (await sub_of(ws)).status == "active"  # сорванная доплата не вводит dunning
    # Отзыв приглашения закрывает счёт.
    iid = (await client.get(W(ws, "/invitations"), headers=oh)).json()[0]["id"]
    await client.post(W(ws, f"/invitations/{iid}/revoke"), headers=oh)
    assert [i.status for i in await invoices_of(ws) if i.kind == "proration"] == [
        InvoiceStatus.void
    ]


async def test_removing_a_member_shrinks_seats_from_next_period_without_refund(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws, seats=3, now=clock.current)
    m, _ = await factory.member(ws, "member")
    await factory.member(ws, "member")
    assert (await client.delete(W(ws, f"/members/{m.id}"), headers=oh)).status_code == 204
    sub = await sub_of(ws)
    assert sub.seats == 3 and sub.pending_seats == 2  # оплачено до конца периода, дальше — 2
    overview = (await client.get(W(ws, "/billing"), headers=oh)).json()
    assert overview["pending_seats"] == 2 and overview["next_charge_amount"] == 2 * 490_00

    clock.current = sub.current_period_end + timedelta(minutes=1)
    await stack.tick()
    sub = await sub_of(ws)
    assert sub.seats == 2 and sub.pending_seats is None
    renewal = [i for i in await invoices_of(ws) if i.kind == "renewal"][0]
    assert renewal.amount == 2 * 490_00 and renewal.status == InvoiceStatus.paid


async def test_set_seats_endpoint(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await factory.pro(ws, seats=4, now=clock.current)
    await factory.member(ws, "member")
    below = await client.post(W(ws, "/billing/seats"), json={"seats": 1}, headers=oh)
    assert below.status_code == 422 and below.json()["error"]["code"] == "seats_below_usage"
    up = await client.post(W(ws, "/billing/seats"), json={"seats": 9}, headers=oh)
    assert up.status_code == 422 and up.json()["error"]["code"] == "seats_increase_via_invites"
    ok = await client.post(W(ws, "/billing/seats"), json={"seats": 2}, headers=oh)
    assert ok.json()["pending_seats"] == 2
    same = await client.post(W(ws, "/billing/seats"), json={"seats": 4}, headers=oh)
    assert same.json()["pending_seats"] is None


# --- Продление ---------------------------------------------------------------------------


async def test_renewal_is_idempotent(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh)
    first_end = (await sub_of(ws)).current_period_end
    clock.current = first_end + timedelta(minutes=1)

    s1 = await stack.tick()
    s2 = await stack.tick()  # cron сработал ещё раз до/после обработки
    assert s1["renewals"] == 1 and s2["renewals"] == 0
    renewals = [i for i in await invoices_of(ws) if i.kind == "renewal"]
    assert len(renewals) == 1 and renewals[0].status == InvoiceStatus.paid
    sub = await sub_of(ws)
    assert sub.current_period_start == first_end and sub.current_period_end > first_end

    # Даже прямой повторный вызов обработчика не создаёт второй счёт...
    from app.billing.providers import make_provider
    from app.billing.service import renew_subscription

    async with SessionLocal() as s:
        res = await renew_subscription(
            s, make_provider(s, clock.current), ws.id, first_end.isoformat(), clock.current
        )
        await s.commit()
    assert res == "not_due"
    # ...а база не даёт вставить дубль счёта за тот же период.
    async with SessionLocal() as s:
        s.add(
            Invoice(
                workspace_id=ws.id, kind="renewal", amount=1, period_start=first_end, status="open"
            )
        )
        with pytest.raises(Exception, match="uq_invoices_renewal_period"):
            await s.commit()
    assert [r.status for r in await invoices_of(ws) if r.kind == "renewal"] == [InvoiceStatus.paid]


async def test_renewal_command_dedupe(factory: Any, stack: Stack, clock: Any) -> None:
    ws, _, _ = await factory.workspace()
    await factory.pro(ws, now=clock.current)
    clock.advance(days=40)
    async with SessionLocal() as s:
        from app.billing.service import scan_renewals

        a = await scan_renewals(s, clock.current)
        b = await scan_renewals(s, clock.current)
        await s.commit()
    assert (a["renewals"], b["renewals"]) == (1, 0)
    async with SessionLocal() as s:
        cmds = (
            await s.scalars(
                select(OutboxCommand).where(OutboxCommand.command == "RENEW_SUBSCRIPTION")
            )
        ).all()
    assert len(cmds) == 1


async def test_period_change_applies_at_next_renewal(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh)
    r = await client.post(W(ws, "/billing/period"), json={"period": "year"}, headers=oh)
    assert r.json()["pending_period"] == "year" and r.json()["billing_period"] == "month"
    assert r.json()["next_charge_amount"] == 4900_00
    clock.current = (await sub_of(ws)).current_period_end + timedelta(minutes=1)
    await stack.tick()
    sub = await sub_of(ws)
    assert sub.billing_period == "year" and sub.pending_period is None
    assert sub.current_period_end - sub.current_period_start >= timedelta(days=365)
    # Возврат к текущему периоду снимает отложенную смену.
    r = await client.post(W(ws, "/billing/period"), json={"period": "month"}, headers=oh)
    assert r.json()["pending_period"] == "month"
    r = await client.post(W(ws, "/billing/period"), json={"period": "year"}, headers=oh)
    assert r.json()["pending_period"] is None


# --- Dunning -----------------------------------------------------------------------------


async def test_dunning_chain_to_read_only_and_recovery(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, owner, oh = await factory.workspace()
    _, mh = await factory.member(ws, "member")
    await upgrade(client, stack, ws, oh, card=CARD_LATER)
    p = (await client.post(W(ws, "/projects"), json={"name": "Site"}, headers=oh)).json()
    end = (await sub_of(ws)).current_period_end
    assert end is not None

    clock.current = end + timedelta(minutes=1)
    await stack.tick()
    sub = await sub_of(ws)
    assert sub.status == "past_due" and sub.past_due_since == clock.current
    fail_time = clock.current
    assert any(to == owner.email and "Не удалось списать" in s for to, s, _ in stack.emails)
    # Баннер виден всем участникам: статус приходит в данных воркспейса.
    assert (await client.get(W(ws), headers=mh)).json()["status"] == "past_due"
    assert (
        await client.post(
            W(ws, f"/projects/{p['id']}/tasks"), json={"title": "still writable"}, headers=mh
        )
    ).status_code == 201

    # Повторы через 1, 3 и 5 дней — ровно по одному.
    attempts = []
    for days in (0.5, 1, 2, 3, 4, 5, 6):
        clock.current = fail_time + timedelta(days=days)
        await stack.tick()
        (renewal,) = [i for i in await invoices_of(ws) if i.kind == "renewal"]
        attempts.append((days, renewal.attempts))
    assert attempts == [(0.5, 1), (1, 2), (2, 2), (3, 3), (4, 3), (5, 4), (6, 4)]
    assert (await sub_of(ws)).status == "past_due"
    assert (
        sum(1 for to, s, _ in stack.emails if "Не удалось списать" in s) == 1
    )  # письмо — один раз

    clock.current = fail_time + timedelta(days=7)
    stats = await stack.tick()
    assert stats["read_only"] == 1
    assert (await sub_of(ws)).status == "read_only"
    assert any("режим чтения" in s for _, s, _ in stack.emails)
    # Любое изменение — 402, чтение работает.
    blocked = await client.post(
        W(ws, f"/projects/{p['id']}/tasks"), json={"title": "x"}, headers=mh
    )
    assert blocked.status_code == 402 and blocked.json()["error"]["code"] == "workspace_read_only"
    assert (await client.get(W(ws, f"/projects/{p['id']}/board"), headers=mh)).status_code == 200

    # Owner обновляет карту и оплачивает сразу → active, редактирование вернулось.
    overview = (await client.get(W(ws, "/billing"), headers=oh)).json()
    assert overview["status"] == "read_only" and overview["outstanding_invoice_id"]
    r = await client.post(W(ws, "/billing/pay-outstanding"), json={}, headers=oh)
    assert r.status_code == 200
    await stack.pay(r.json()["checkout_url"], CARD_OK)
    sub = await sub_of(ws)
    assert sub.status == "active" and sub.past_due_since is None
    assert sub.current_period_start == end  # период продлён с даты просроченного счёта
    ok = await client.post(W(ws, f"/projects/{p['id']}/tasks"), json={"title": "back"}, headers=mh)
    assert ok.status_code == 201
    # С новой картой следующий цикл проходит без проблем.
    pm = await SessionLocal().__aenter__()
    try:
        method = await pm.get(PaymentMethod, sub.payment_method_id)
        assert method is not None and "ok" in method.provider_token
    finally:
        await pm.close()


async def test_dunning_retry_succeeds_with_fixed_card(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh, card=CARD_LATER)
    clock.current = (await sub_of(ws)).current_period_end + timedelta(minutes=1)
    await stack.tick()
    assert (await sub_of(ws)).status == "past_due"
    # Владелец привязал рабочую карту, пока шёл dunning.
    r = await client.post(W(ws, "/billing/card/checkout"), json={}, headers=oh)
    await stack.pay(r.json()["checkout_url"], CARD_OK)
    assert (await sub_of(ws)).status == "past_due"  # одной сменой карты не лечится
    clock.advance(days=1, minutes=1)
    await stack.tick()  # плановая повторная попытка с новой картой
    sub = await sub_of(ws)
    assert sub.status == "active" and sub.past_due_since is None


# --- Отмена ----------------------------------------------------------------------------


async def test_cancel_resume_and_end_of_period(
    client: AsyncClient, factory: Any, stack: Stack, clock: Any
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh)
    r = await client.post(W(ws, "/billing/cancel"), headers=oh)
    assert r.json()["cancel_at_period_end"] is True and r.json()["next_charge_amount"] == 0
    assert (await client.get(W(ws), headers=oh)).json()[
        "plan"
    ] == "pro"  # до конца периода всё работает
    r = await client.post(W(ws, "/billing/resume"), headers=oh)
    assert r.json()["cancel_at_period_end"] is False
    assert (await client.post(W(ws, "/billing/resume"), headers=oh)).status_code == 409

    await client.post(W(ws, "/billing/cancel"), headers=oh)
    clock.current = (await sub_of(ws)).current_period_end + timedelta(minutes=1)
    stats = await stack.tick()
    assert stats["downgrades"] == 1 and stats["renewals"] == 0
    sub = await sub_of(ws)
    assert (sub.plan, sub.status) == ("free", "canceled")
    assert [i.kind for i in await invoices_of(ws)] == ["subscription"]  # продления не было
    # Можно вернуться на Pro.
    await upgrade(client, stack, ws, oh)
    assert (await sub_of(ws)).status == "active"


async def test_cancel_without_subscription_is_rejected(client: AsyncClient, factory: Any) -> None:
    ws, _, oh = await factory.workspace()
    for path, body in (
        ("/billing/cancel", None),
        ("/billing/seats", {"seats": 1}),
        ("/billing/period", {"period": "year"}),
        ("/billing/card/checkout", {}),
        ("/billing/pay-outstanding", {}),
    ):
        r = await client.post(W(ws, path), json=body, headers=oh)
        assert r.status_code == 409, path


async def test_billing_changes_are_logged_in_activity(
    client: AsyncClient, factory: Any, stack: Stack
) -> None:
    ws, _, oh = await factory.workspace()
    await upgrade(client, stack, ws, oh)
    await factory.pro(ws)  # no-op для чтения лога на Pro
    feed = (await client.get(W(ws, "/activity"), headers=oh)).json()["items"]
    actions = [i["action"] for i in feed]
    assert "billing.payment_succeeded" in actions and "billing.checkout_started" in actions
    _ = (
        issues,
        Invitation,
        RawWebhookDelivery,
        SubStatus,
        SystemIssue,
        WebhookEvent,
        asyncio,
        AsyncSession,
    )
