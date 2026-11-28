"""Request-size rules, applied BEFORE FastAPI reads a request body.

Why a route class and not a dependency: FastAPI reads the whole body before it resolves
dependencies, so a dependency could only reject a huge upload after it was already in memory.
The check relies on Content-Length: an HTTP/1.1 server never delivers more bytes than declared.
"""
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute

from app.core.config import Settings
from app.core.exceptions import AppError, InvalidInputError, PayloadTooLargeError

BODY_METHODS = {"POST", "PUT", "PATCH"}
MULTIPART_OVERHEAD = 1024 * 1024  # boundaries and form fields around the file
JSON_OVERHEAD = 64 * 1024
JSON_BYTES_PER_CHAR = 6  # non-ASCII text escaped as \uXXXX


class LengthRequiredError(AppError):
    status_code = 411
    code = "length_required"


def body_limit(content_type: str, settings: Settings) -> int:
    if content_type.lower().startswith("multipart/"):
        return settings.max_upload_bytes + MULTIPART_OVERHEAD
    return settings.max_text_chars * JSON_BYTES_PER_CHAR + JSON_OVERHEAD


def check_body_headers(method: str, headers: Mapping[str, str], settings: Settings) -> None:
    """Refuse oversized or unsized request bodies. Header names must be lower-case."""
    if method.upper() not in BODY_METHODS:
        return
    if "transfer-encoding" in headers:
        raise LengthRequiredError("Send a Content-Length header; chunked uploads are not supported.")
    raw = headers.get("content-length")
    if raw is None:
        return  # HTTP/1.1: no length and no chunking means no body
    if not (raw.isascii() and raw.isdigit()):
        raise InvalidInputError("The Content-Length header is not valid.")
    if int(raw) > body_limit(headers.get("content-type", ""), settings):
        raise PayloadTooLargeError("The request body is too large.")


class BodyLimitedRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            check_body_headers(request.method, request.headers, request.app.state.settings)
            return await original(request)

        return handler