"""Request-ID and request-timing middleware."""
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import request_id_var

log = logging.getLogger("pka.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Accept a client ID only if it is short and safe; otherwise generate one.
        incoming = request.headers.get("x-request-id", "")
        if 0 < len(incoming) <= 64 and incoming.isascii() and incoming.isprintable():
            rid = incoming
        else:
            rid = uuid.uuid4().hex
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            ms = round((time.perf_counter() - start) * 1000, 1)
            log.info(
                "request",
                extra={"ctx": {"method": request.method, "path": request.url.path, "ms": ms}},
            )
            request_id_var.reset(token)
        response.headers["x-request-id"] = rid
        return response