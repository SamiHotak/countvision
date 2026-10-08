"""One error format for the whole API: {"error": {"code": ..., "message": ..., "fields": {...}}}."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


class AppError(Exception):
    """An expected error with an HTTP status, a stable code and a human message."""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.fields = fields or {}


class NotAuthenticated(AppError):
    status = 401
    code = "not_authenticated"


class Forbidden(AppError):
    status = 403
    code = "forbidden"


class NotFound(AppError):
    status = 404
    code = "not_found"


class Conflict(AppError):
    status = 409
    code = "conflict"


class TooManyRequests(AppError):
    status = 429
    code = "too_many_requests"


def _body(code: str, message: str, fields: dict[str, str] | None = None) -> dict:
    error: dict = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return {"error": error}


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers that turn errors into the common JSON format."""

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.fields), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields: dict[str, str] = {}
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
            key = ".".join(loc) or "request"
            msg = str(err.get("msg", "Invalid value")).removeprefix("Value error, ")
            fields.setdefault(key, msg)
        first = next(iter(fields.items()), ("request", "Invalid request"))
        return JSONResponse(_body("invalid", f"{first[0]}: {first[1]}", fields), status_code=422)
