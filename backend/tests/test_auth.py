import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActivityLog,
    Membership,
    OutboxCommand,
    RefreshToken,
    Subscription,
    User,
    Workspace,
)

CSRF = {"X-Requested-With": "crewboard"}
PW = "correct-horse-1"


async def register(client: AsyncClient, email: str = "alice@example.com") -> dict[str, Any]:
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": PW, "name": "Alice"}
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def emails(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(OutboxCommand)
        .where(OutboxCommand.command == "SEND_EMAIL")
        .order_by(OutboxCommand.id)
    )
    return [r.payload for r in rows]


def token_from(body: str) -> str:
    m = re.search(r"token=([\w-]+)", body)
    assert m
    return m.group(1)


async def test_register_creates_user_workspace_and_verification_mail(
    client: AsyncClient, session: AsyncSession
) -> None:
    body = await register(client)
    assert body["user"]["email_verified"] is False
    assert body["access_token"]
    assert "crewboard_refresh" in client.cookies

    user = await session.scalar(select(User))
    assert user is not None and user.password_hash and user.password_hash != PW
    ws = await session.scalar(select(Workspace))
    assert ws is not None and ws.owner_id == user.id
    assert (await session.get(Membership, (ws.id, user.id))) is not None
    sub = await session.get(Subscription, ws.id)
    assert sub is not None and sub.plan == "free"
    sent = await emails(session)
    assert len(sent) == 1 and sent[0]["to"] == "alice@example.com"


async def test_refresh_cookie_flags(client: AsyncClient) -> None:
    r = await client.post(
        "/api/auth/register", json={"email": "a@example.com", "password": PW, "name": "A"}
    )
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/api/auth" in cookie


async def test_register_duplicate_email_is_409_case_insensitive(client: AsyncClient) -> None:
    await register(client, "bob@example.com")
    r = await client.post(
        "/api/auth/register", json={"email": "BOB@example.com", "password": PW, "name": "B"}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"


async def test_register_race_two_requests_one_wins(
    client: AsyncClient, session: AsyncSession
) -> None:
    payload = {"email": "race@example.com", "password": PW, "name": "R"}
    rs = await asyncio.gather(*[client.post("/api/auth/register", json=payload) for _ in range(2)])
    assert sorted(r.status_code for r in rs) == [201, 409]
    assert len((await session.scalars(select(User))).all()) == 1


async def test_register_validation(client: AsyncClient) -> None:
    r = await client.post(
        "/api/auth/register", json={"email": "bad", "password": "short", "name": ""}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


async def test_login_success_and_failures(client: AsyncClient) -> None:
    await register(client)
    ok = await client.post("/api/auth/login", json={"email": "ALICE@example.com", "password": PW})
    assert ok.status_code == 200 and ok.json()["access_token"]
    bad = await client.post(
        "/api/auth/login", json={"email": "alice@example.com", "password": "x" * 9}
    )
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_credentials"
    nobody = await client.post("/api/auth/login", json={"email": "no@example.com", "password": PW})
    assert nobody.status_code == 401


async def test_login_rate_limit(client: AsyncClient) -> None:
    await register(client)
    codes = [
        (
            await client.post(
                "/api/auth/login", json={"email": "alice@example.com", "password": "wrong-pass-1"}
            )
        ).status_code
        for _ in range(7)
    ]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]


async def test_me_requires_token(client: AsyncClient) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (
        await client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
    ).status_code == 401
    body = await register(client)
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200 and me.json()["email"] == "alice@example.com"


async def test_refresh_rotates_token(client: AsyncClient, session: AsyncSession) -> None:
    await register(client)
    first = client.cookies["crewboard_refresh"]
    r = await client.post("/api/auth/refresh", headers=CSRF)
    assert r.status_code == 200 and r.json()["access_token"]
    second = client.cookies["crewboard_refresh"]
    assert first != second

    rows = (await session.scalars(select(RefreshToken).order_by(RefreshToken.id))).all()
    assert len(rows) == 2
    assert rows[0].revoked_at is not None and rows[0].replaced_by_id == rows[1].id
    assert rows[1].revoked_at is None and rows[0].family_id == rows[1].family_id
    # В БД только хэш, не сам токен.
    assert first not in {r.token_hash for r in rows}


async def test_refresh_requires_csrf_header_and_cookie(client: AsyncClient) -> None:
    await register(client)
    assert (await client.post("/api/auth/refresh")).status_code == 403
    client.cookies.clear()
    r = await client.post("/api/auth/refresh", headers=CSRF)
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_refresh_token"


async def test_refresh_reuse_detection_revokes_family(
    client: AsyncClient, session: AsyncSession
) -> None:
    await register(client)
    stolen = client.cookies["crewboard_refresh"]
    assert (await client.post("/api/auth/refresh", headers=CSRF)).status_code == 200
    legit_new = client.cookies["crewboard_refresh"]

    # Злоумышленник предъявляет уже использованный (отозванный) токен.
    client.cookies.clear()
    client.cookies.set("crewboard_refresh", stolen, path="/api/auth")
    r = await client.post("/api/auth/refresh", headers=CSRF)
    assert r.status_code == 401 and r.json()["error"]["code"] == "refresh_token_reused"

    rows = (await session.scalars(select(RefreshToken))).all()
    assert rows and all(row.revoked_at is not None for row in rows)

    # Легитимный новый токен из того же семейства тоже больше не работает.
    client.cookies.clear()
    client.cookies.set("crewboard_refresh", legit_new, path="/api/auth")
    assert (await client.post("/api/auth/refresh", headers=CSRF)).status_code == 401


async def test_refresh_expired_token(client: AsyncClient, clock: Any) -> None:
    await register(client)
    clock.advance(days=31)
    r = await client.post("/api/auth/refresh", headers=CSRF)
    assert r.status_code == 401


async def test_access_token_expires(client: AsyncClient, clock: Any) -> None:
    body = await register(client)
    h = {"Authorization": f"Bearer {body['access_token']}"}
    assert (await client.get("/api/auth/me", headers=h)).status_code == 200
    clock.advance(minutes=16)
    assert (await client.get("/api/auth/me", headers=h)).status_code == 401


async def test_logout_revokes_current_session_only(
    client: AsyncClient, session: AsyncSession
) -> None:
    await register(client)
    cookie = client.cookies["crewboard_refresh"]
    assert (await client.post("/api/auth/logout")).status_code == 403  # без CSRF-заголовка
    assert (await client.post("/api/auth/logout", headers=CSRF)).status_code == 204
    client.cookies.set("crewboard_refresh", cookie, path="/api/auth")
    # Токен отозван → refresh отвечает 401 (reuse-ветка).
    assert (await client.post("/api/auth/refresh", headers=CSRF)).status_code == 401


async def test_logout_all_revokes_every_session(client: AsyncClient, session: AsyncSession) -> None:
    body = await register(client)
    await client.post("/api/auth/login", json={"email": "alice@example.com", "password": PW})
    h = {"Authorization": f"Bearer {body['access_token']}", **CSRF}
    assert (await client.post("/api/auth/logout-all", headers=h)).status_code == 204
    rows = (await session.scalars(select(RefreshToken))).all()
    assert len(rows) == 2 and all(r.revoked_at is not None for r in rows)


async def test_verify_email_flow(client: AsyncClient, session: AsyncSession) -> None:
    body = await register(client)
    token = token_from((await emails(session))[0]["body"])
    r = await client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200 and r.json()["email_verified"] is True
    # Токен одноразовый.
    again = await client.post("/api/auth/verify-email", json={"token": token})
    assert again.status_code == 400
    bogus = await client.post("/api/auth/verify-email", json={"token": "nope"})
    assert bogus.status_code == 400
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.json()["email_verified"] is True


async def test_resend_verification(client: AsyncClient, session: AsyncSession) -> None:
    body = await register(client)
    h = {"Authorization": f"Bearer {body['access_token']}"}
    assert (await client.post("/api/auth/resend-verification", headers=h)).status_code == 202
    assert len(await emails(session)) == 2


async def test_password_reset_flow_revokes_sessions(
    client: AsyncClient, session: AsyncSession
) -> None:
    await register(client)
    r = await client.post("/api/auth/password-reset/request", json={"email": "alice@example.com"})
    assert r.status_code == 202
    token = token_from((await emails(session))[-1]["body"])

    new_pw = "brand-new-pass-2"
    ok = await client.post(
        "/api/auth/password-reset/confirm", json={"token": token, "password": new_pw}
    )
    assert ok.status_code == 204
    assert (
        await client.post(
            "/api/auth/password-reset/confirm", json={"token": token, "password": new_pw}
        )
    ).status_code == 400

    rows = (await session.scalars(select(RefreshToken))).all()
    assert rows and all(row.revoked_at is not None for row in rows)
    old = await client.post("/api/auth/login", json={"email": "alice@example.com", "password": PW})
    assert old.status_code == 401
    new = await client.post(
        "/api/auth/login", json={"email": "alice@example.com", "password": new_pw}
    )
    assert new.status_code == 200


async def test_password_reset_response_is_same_for_unknown_email(
    client: AsyncClient, session: AsyncSession
) -> None:
    await register(client)
    known = await client.post(
        "/api/auth/password-reset/request", json={"email": "alice@example.com"}
    )
    unknown = await client.post(
        "/api/auth/password-reset/request", json={"email": "ghost@example.com"}
    )
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    # Письмо ушло только существующему адресу.
    assert [e["to"] for e in await emails(session)] == ["alice@example.com", "alice@example.com"]


async def test_reset_token_expires_after_30_minutes(
    client: AsyncClient, clock: Any, session: AsyncSession
) -> None:
    await register(client)
    await client.post("/api/auth/password-reset/request", json={"email": "alice@example.com"})
    token = token_from((await emails(session))[-1]["body"])
    clock.advance(minutes=31)
    r = await client.post(
        "/api/auth/password-reset/confirm", json={"token": token, "password": "another-pass-3"}
    )
    assert r.status_code == 400


async def test_verify_token_cannot_be_used_for_reset(
    client: AsyncClient, session: AsyncSession
) -> None:
    await register(client)
    token = token_from((await emails(session))[0]["body"])
    r = await client.post(
        "/api/auth/password-reset/confirm", json={"token": token, "password": "another-pass-3"}
    )
    assert r.status_code == 400


# --- GitHub OAuth -----------------------------------------------------------------------


@pytest.fixture
def github_on(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "github_client_id", "cid")
    monkeypatch.setattr(s, "github_client_secret", "secret")


async def test_github_disabled_by_default(client: AsyncClient) -> None:
    assert (await client.get("/api/auth/providers")).json()["github"] is False
    assert (await client.get("/api/auth/github/login")).status_code == 404
    assert (await client.get("/api/auth/github/callback?code=x&state=y")).status_code == 404


async def _start_github(client: AsyncClient) -> str:
    r = await client.get("/api/auth/github/login", follow_redirects=False)
    assert r.status_code == 302 and "github.com/login/oauth/authorize" in r.headers["location"]
    return re.search(r"state=([\w-]+)", r.headers["location"]).group(1)  # type: ignore[union-attr]


def _profile(monkeypatch: pytest.MonkeyPatch, email: str, gid: str = "42") -> None:
    from app.services import github

    async def fake(code: str) -> github.GithubProfile:
        return github.GithubProfile(id=gid, email=email, name="Octo")

    monkeypatch.setattr(github, "fetch_profile", fake)


async def test_github_creates_new_verified_user(
    client: AsyncClient, github_on: None, monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    assert (await client.get("/api/auth/providers")).json()["github"] is True
    _profile(monkeypatch, "octo@example.com")
    state = await _start_github(client)
    r = await client.get(
        f"/api/auth/github/callback?code=abc&state={state}", follow_redirects=False
    )
    assert r.status_code == 302 and r.headers["location"].endswith("/auth/callback")
    user = await session.scalar(select(User).where(User.email == "octo@example.com"))
    assert user is not None and user.email_verified_at is not None and user.password_hash is None
    assert (await client.post("/api/auth/refresh", headers=CSRF)).status_code == 200
    # Повторный вход — тот же пользователь.
    state = await _start_github(client)
    await client.get(f"/api/auth/github/callback?code=abc&state={state}", follow_redirects=False)
    assert len((await session.scalars(select(User))).all()) == 1


async def test_github_links_to_verified_local_account(
    client: AsyncClient,
    github_on: None,
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    make_user: Any,
) -> None:
    user, _ = await make_user("octo@example.com", verified=True)
    _profile(monkeypatch, "octo@example.com", "7")
    state = await _start_github(client)
    r = await client.get(
        f"/api/auth/github/callback?code=abc&state={state}", follow_redirects=False
    )
    assert r.headers["location"].endswith("/auth/callback")
    from app.models import OAuthAccount

    acc = await session.scalar(select(OAuthAccount))
    assert acc is not None and acc.user_id == user.id


async def test_github_refuses_unverified_local_account(
    client: AsyncClient,
    github_on: None,
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    make_user: Any,
) -> None:
    await make_user("octo@example.com", verified=False)
    _profile(monkeypatch, "octo@example.com")
    state = await _start_github(client)
    r = await client.get(
        f"/api/auth/github/callback?code=abc&state={state}", follow_redirects=False
    )
    assert "error=email_unverified" in r.headers["location"]
    from app.models import OAuthAccount

    assert await session.scalar(select(OAuthAccount)) is None


async def test_github_rejects_bad_state(client: AsyncClient, github_on: None) -> None:
    await _start_github(client)
    r = await client.get("/api/auth/github/callback?code=abc&state=forged", follow_redirects=False)
    assert "error=invalid_state" in r.headers["location"]
    client.cookies.clear()
    r = await client.get("/api/auth/github/callback?code=abc&state=forged", follow_redirects=False)
    assert "error=invalid_state" in r.headers["location"]


async def test_github_failure_redirects_with_error(
    client: AsyncClient, github_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import github

    async def boom(code: str) -> github.GithubProfile:
        raise github.GithubError("x")

    monkeypatch.setattr(github, "fetch_profile", boom)
    state = await _start_github(client)
    r = await client.get(
        f"/api/auth/github/callback?code=abc&state={state}", follow_redirects=False
    )
    assert "error=github_failed" in r.headers["location"]


async def test_workspaces_list_and_create(client: AsyncClient) -> None:
    body = await register(client)
    h = {"Authorization": f"Bearer {body['access_token']}"}
    lst = await client.get("/api/workspaces", headers=h)
    assert (
        len(lst.json()) == 1
        and lst.json()[0]["role"] == "owner"
        and lst.json()[0]["plan"] == "free"
    )
    created = await client.post("/api/workspaces", json={"name": "Second team"}, headers=h)
    assert created.status_code == 201 and created.json()["slug"] == "second-team"
    assert len((await client.get("/api/workspaces", headers=h)).json()) == 2


_ = (ActivityLog, UTC, datetime, timedelta)
