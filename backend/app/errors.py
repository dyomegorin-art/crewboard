from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Доменная ошибка с кодом, попадающая в единый формат ответа."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        details: object | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        self.details = details


def _body(code: str, message: str, details: object | None = None) -> dict[str, object]:
    err: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            _body(exc.code, exc.message, exc.details),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            _body(f"http_{exc.status_code}", str(exc.detail)),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
        ]
        return JSONResponse(_body("validation_error", "Invalid request", details), status_code=422)
