from httpx import AsyncClient


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "postgres": "ok", "redis": "ok"}


async def test_unknown_route_uses_error_format(client: AsyncClient) -> None:
    r = await client.get("/api/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "http_404"
