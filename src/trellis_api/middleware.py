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

* :func:`trellis_error_handler` sits in front of it for the *typed*
  hierarchy in :mod:`trellis.errors` (#459). Those exceptions carry a
  stable ``code`` and a message written for an operator — a damaged
  ``policies.json`` names the file, the specific problem and the recovery
  command — and the catch-all threw all of it away, answering ``500
  internal_error`` for a file the caller's operator can fix in one edit.
  This handler keeps the same envelope keys and fills them with what the
  exception already knows.

Health and version probes pass through the middleware too; the
overhead is a ULID + a contextvar bind, both microsecond-scale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from fastapi.responses import JSONResponse

from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.ids import generate_ulid
from trellis.errors import ConfigError, TrellisError

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


#: Status for a :class:`~trellis.errors.ConfigError` reaching the boundary.
#:
#: 409, and the choice is between this and keeping the 500, not between
#: this and 503 — ``routes/policies.py::_refusal_http_error`` already ruled
#: 503 out for the same file: a damaged config file does not repair itself,
#: so "retry later" is the wrong instruction. What settles it for 409 is
#: that ``GET /api/v1/policies`` **already answers 409** when that very file
#: is damaged, from a *read* route where nothing was being written. One
#: deployment fault answering 409 on one route and 500 on every other is a
#: distinction no client can act on.
#:
#: The obvious objection — a 4xx will not trip a monitor watching 5xx, and
#: this fault darkens every governed write — is answered by #458 rather
#: than by the status code: the outage is visible as a ``WRITE_REJECTED``
#: event under ``config:policy_file``, which ``trellis analyze health`` and
#: the capture-health banner both read. Visibility rides the event log;
#: this handler is only the legibility half.
CONFIG_ERROR_STATUS = 409

#: Fields the typed hierarchy carries that a caller can act on. Read off
#: the exception, so a subclass that adds one is carried without an edit
#: here. ``setting`` names the config key or file, ``path`` the file,
#: ``recovery`` the shell command that clears the state, ``store`` the
#: backend. All are Trellis-authored identifiers rather than exception
#: content — the message is the part that gets sanitized.
_CONTEXT_ATTRS = ("setting", "path", "recovery", "store")


def _error_status(exc: TrellisError) -> int:
    """HTTP status for a typed Trellis error reaching the boundary.

    Deliberately narrow. Only :class:`~trellis.errors.ConfigError` is
    remapped; everything else keeps the 500 it returns today, because
    widening the *status* map is a separate decision from making the
    *body* legible and this change is only licensed to make the second.
    A ``StoreError`` from a driver that fell over is a server fault and
    500 is the honest answer for it — what was wrong was answering
    ``internal_error`` with no code and no message.
    """
    if isinstance(exc, ConfigError):
        return CONFIG_ERROR_STATUS
    return 500


async def trellis_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Answer a typed Trellis failure with what the exception already knows.

    Same envelope as :func:`unhandled_exception_handler` — ``code`` /
    ``message`` / ``request_id`` — so a client that already branches on
    ``code`` needs no new shape; the difference is that ``code`` is the
    exception's own stable identifier (lowercased to match the wire
    vocabulary ``routes/policies.py`` established with ``degraded_store``
    and ``stale_store_write``) and ``message`` is the operator-facing text
    the raiser wrote.

    The message is passed through
    :func:`~trellis.core.error_sanitize.sanitize_error_message`, which is
    what makes handling the whole family safe rather than just
    ``ConfigError``: a driver-raised ``StoreError`` can echo a DSN with
    credentials, and an API response body is exactly the artifact #206
    wrote that guard for. It is not a substitute for reading the logs —
    ``logger.exception`` still records the full traceback under the same
    ``request_id``.

    Registered for :class:`~trellis.errors.TrellisError`, so Starlette's
    MRO lookup routes every subclass here and the catch-all keeps only
    what it should have had all along: genuinely untyped failures.
    """
    request_id = getattr(request.state, "request_id", None)
    trellis_exc = exc if isinstance(exc, TrellisError) else TrellisError(str(exc))
    status_code = _error_status(trellis_exc)
    logger.exception(
        "api_trellis_error",
        path=request.url.path,
        method=request.method,
        request_id=request_id,
        exc_type=type(exc).__name__,
        error_code=trellis_exc.code,
        status_code=status_code,
    )
    content: dict[str, Any] = {
        "code": trellis_exc.code.lower(),
        "message": sanitize_error_message(trellis_exc.message),
        "request_id": request_id,
    }
    for attr in _CONTEXT_ATTRS:
        value = getattr(trellis_exc, attr, None)
        if value is not None:
            content[attr] = value
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={REQUEST_ID_HEADER: request_id} if request_id else {},
    )
