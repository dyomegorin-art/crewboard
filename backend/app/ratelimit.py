import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.errors import AppError

log = logging.getLogger(__name__)

# Атомарно: INCR + EXPIRE при первом инкременте, чтобы ключ не остался без TTL.
# Возвращает {счётчик, оставшийся TTL}.
SCRIPT = """
local count = redis.call("incr", KEYS[1])
if count == 1 then
    redis.call("expire", KEYS[1], ARGV[1])
end
local ttl = redis.call("ttl", KEYS[1])
if ttl < 0 then
    redis.call("expire", KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


async def check_rate_limit(
    redis: Redis, scope: str, subject: str, limit: int, window: int = 60
) -> None:
    """Фиксированное окно: больше `limit` запросов за `window` секунд → 429 + Retry-After.

    Redis недоступен → пропускаем запрос (fail open): лимит — защита, а не источник истины.
    """
    key = f"rl:{scope}:{subject}"
    try:
        count, ttl = await redis.eval(SCRIPT, 1, key, str(window))  # type: ignore[misc]
    except RedisError:
        log.warning("rate limiter unavailable, skipping", extra={"scope": scope})
        return
    if count > limit:
        raise AppError(
            429,
            "rate_limited",
            "Too many requests, try again later",
            headers={"Retry-After": str(max(int(ttl), 1))},
        )
