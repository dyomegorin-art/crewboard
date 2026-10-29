import pytest
from httpx import AsyncClient

from app.config import Settings


def make(**kw: object) -> Settings:
    base = {
        "database_url": "postgresql+asyncpg://x",
        "redis_url": "redis://x",
        "jwt_secret": "s" * 40,
        "webhook_secret": "w" * 32,
        "dev_endpoints": False,
    }
    return Settings(_env_file=None, **{**base, **kw})  # type: ignore[arg-type]


def test_dev_endpoints_forbidden_outside_local() -> None:
    make(env="local", dev_endpoints=True).validate_startup()
    make(env="test", dev_endpoints=True).validate_startup()
    for env in ("staging", "production"):
        with pytest.raises(RuntimeError, match="DEV_ENDPOINTS"):
            make(env=env, dev_endpoints=True).validate_startup()
        make(env=env, dev_endpoints=False).validate_startup()


def test_other_startup_checks() -> None:
    with pytest.raises(RuntimeError, match="Unknown ENV"):
        make(env="qa").validate_startup()
    with pytest.raises(RuntimeError, match="PAYMENT_PROVIDER"):
        make(payment_provider="stripe").validate_startup()
    with pytest.raises(RuntimeError, match="YOOKASSA"):
        make(payment_provider="yookassa").validate_startup()
    make(
        payment_provider="yookassa", yookassa_shop_id="1", yookassa_secret_key="k"
    ).validate_startup()
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        make(env="production", jwt_secret="short").validate_startup()
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        make(webhook_secret="").validate_startup()
    assert make(github_client_id="a", github_client_secret="b").github_enabled
    assert not make(github_client_id="a").github_enabled


async def test_advance_clock_moves_time_and_resets(client: AsyncClient) -> None:
    from datetime import datetime

    before = datetime.fromisoformat((await client.get("/api/dev/clock")).json()["now"])
    r = await client.post("/api/dev/advance-clock", json={"days": 2, "hours": 3})
    assert r.json()["offset_seconds"] == 2 * 86400 + 3 * 3600
    after = datetime.fromisoformat((await client.get("/api/dev/clock")).json()["now"])
    assert 183600 <= (after - before).total_seconds() < 183660
    r = await client.post("/api/dev/reset-clock")
    assert r.json()["offset_seconds"] == 0
