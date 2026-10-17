"""Centralized error model: every error becomes {"error": {code, message, request_id}}."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import request_id_var

log = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 400
    code = "app_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"


class ProviderUnavailableError(AppError):
    status_code = 503
    code = "provider_unavailable"


def _body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_body(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Report field locations only, not submitted values (may be personal data).
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        return JSONResponse(status_code=422, content=_body("validation_error", f"Invalid: {fields}"))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", exc_info=exc)
        return JSONResponse(status_code=500, content=_body("internal_error", "Internal server error"))