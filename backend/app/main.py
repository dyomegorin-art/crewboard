import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.api import health
from app.api.router import api_router
from app.config import get_settings
from app.errors import register_error_handlers
from app.logging import request_id_var, setup_logging

settings = get_settings()
settings.validate_startup()
setup_logging(settings.log_level)
log = logging.getLogger("app")

app = FastAPI(title="Crewboard API", version="0.1.0")
register_error_handlers(app)


@app.middleware("http")
async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request_id_var.set(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    if request.url.path != "/health":  # healthcheck дёргается каждые 3 с — не засоряем лог
        log.info("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response


app.include_router(health.router)
app.include_router(api_router)
