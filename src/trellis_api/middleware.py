"""Request middleware — request-ID correlation and structured error envelope.

Two pieces wired into the FastAPI app from :func:`create_app`:

* :func:`request_id_middleware` reads ``X-Request-ID`` from the
  incoming request (or mints a fresh ULID), binds it onto the
  ``structlog`` contextvars so every log line for the request carries
  the same ID, attaches it to ``request.state.request_id`` for route
  handlers, and echoes it back as a response header.

* :func:`unhandled_exception_handler` translates any uncaught
  ``Exception`` to a structured ``{"code", "message", "request_id"}``
  envelope with HTTP 500. The exception is logged with full context
  (``logger.exception``) so the full traceback lands in the structured
  log stream — but the response body never leaks internals.

Health and version probes pass through the middleware too; the
overhead is a ULID + a contextvar bind, both microsecond-scale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi.responses import JSONResponse

from trellis.core.ids import generate_ulid

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request
    from starlette.responses import Response

logger = structlog.get_logger(__name__)

#: Header name used for request-ID propagation. Lowercased on the wire
#: but Starlette / FastAPI normalise headers, so callers can use either
#: case.
REQUEST_ID_HEADER = "X-Request-ID"


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind a request ID to the structlog context for the request's lifetime.

    Reads ``X-Request-ID`` from the inbound request if a caller (load
    balancer, agent client) supplied one — otherwise mints a fresh
    ULID. The same value is echoed back as a response header so the
    caller can grep it across systems.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER) or generate_ulid()
    request.state.request_id = request_id

    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Translate uncaught exceptions to a structured 500 envelope.

    The full traceback is logged via ``logger.exception`` with the
    request ID bound from :func:`request_id_middleware` so it correlates
    with the request access log. The response body is deliberately
    sparse — operators get the request_id and grep their logs; clients
    don't see internal types or messages that could leak schema info.
    """
    request_id = getattr(request.state, "request_id", None)
    logger.exception(
        "api_unhandled_exception",
        path=request.url.path,
        method=request.method,
        request_id=request_id,
        exc_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "internal server error",
            "request_id": request_id,
        },
        headers={REQUEST_ID_HEADER: request_id} if request_id else {},
    )
