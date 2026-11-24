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


class AuthenticationRequiredError(AppError):
    status_code = 401
    code = "authentication_required"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"


class ProviderUnavailableError(AppError):
    status_code = 503
    code = "provider_unavailable"


class InvalidInputError(AppError):
    status_code = 422
    code = "invalid_input"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class IngestionError(AppError):
    """The input cannot be ingested and retrying will not help (bad file, blocked URL...)."""

    status_code = 422
    code = "ingestion_failed"


class UnsafeUrlError(IngestionError):
    status_code = 400
    code = "unsafe_url"


class TransientIngestionError(AppError):
    """A temporary failure (network, timeout, 5xx). The job queue may retry it."""

    status_code = 503
    code = "ingestion_temporarily_failed"


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