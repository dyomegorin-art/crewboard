"""SSE: открытая доска второго пользователя обновляется без перезагрузки.

Авторизация. Браузерный EventSource не умеет ставить заголовок Authorization, а access-токен
в query-параметре попадает в логи прокси. Поэтому клиент сначала обменивает access-токен на
одноразовый-по-смыслу «билет» на 60 секунд, привязанный к воркспейсу и пользователю, и уже
его кладёт в query. Утёкший из логов билет бесполезен через минуту.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceCtx, require
from app.clock import Clock, get_clock
from app.db import get_session
from app.errors import AppError
from app.events import channel
from app.models import Membership, Role
from app.redis import get_redis
from app.security import create_token, decode_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/w/{workspace_id}", tags=["events"])
TICKET_TTL = timedelta(seconds=60)
HEARTBEAT_SECONDS = 15


@router.post("/events/ticket", summary="Билет на 60 секунд для SSE-подключения")
async def ticket(
    ctx: WorkspaceCtx = Depends(require("projects.read")),
) -> dict[str, object]:
    token = create_token(
        ctx.user.id, "sse", await ctx.now_.now(), ttl=TICKET_TTL, ws=ctx.workspace_id
    )
    return {"ticket": token, "expires_in": int(TICKET_TTL.total_seconds())}


async def event_stream(
    redis: Redis,
    workspace_id: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    heartbeat: float = HEARTBEAT_SECONDS,
    forward: bool = True,
) -> AsyncIterator[str]:
    """Читает канал воркспейса и отдаёт кадры SSE; раз в `heartbeat` секунд — комментарий."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel(workspace_id))
    try:
        yield "retry: 3000\n\n: connected\n\n"
        while not await is_disconnected():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=heartbeat)
            if msg is None:
                yield ": ping\n\n"
            elif forward:
                data = msg["data"]
                text = data.decode() if isinstance(data, bytes) else str(data)
                yield f"data: {text}\n\n"
            await asyncio.sleep(0)
    finally:
        await pubsub.unsubscribe(channel(workspace_id))
        await pubsub.aclose()  # type: ignore[no-untyped-call]


@router.get("/events", summary="Поток событий воркспейса (SSE)")
async def events(
    workspace_id: int,
    request: Request,
    ticket: str = Query(...),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    clock: Clock = Depends(get_clock),
) -> StreamingResponse:
    try:
        payload = decode_token(ticket, "sse", await clock.now())
        user_id = int(payload["sub"])
        ticket_ws = int(payload["ws"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise AppError(401, "invalid_ticket", "Invalid or expired ticket") from None
    membership = await session.get(Membership, (workspace_id, user_id))
    if ticket_ws != workspace_id or membership is None:
        raise AppError(404, "workspace_not_found", "Workspace not found")
    role = membership.role
    await session.close()  # соединение с БД не нужно на всё время стрима
    # Гость видит только свои проекты, а событие не несёт прав доступа: гостям ничего не шлём.
    stream = event_stream(redis, workspace_id, request.is_disconnected, forward=role != Role.guest)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
