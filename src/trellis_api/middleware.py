"""Request middleware — request-ID correlation and structured error envelope.

Four pieces wired into the FastAPI app from :func:`create_app`:

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

* :func:`request_validation_error_handler` replaces FastAPI's default
  422 handler. The default echoes the rejected value as
  ``detail[].input``, and Starlette renders JSON with
  ``allow_nan=False``, so an error echoing ``NaN`` or ``Infinity`` (both
  of which Python's ``json`` module parses) makes that 422 raise and the
  catch-all answer ``500 internal_error``. This handler writes a
  non-finite float as its token, and undecodable ``bytes`` (a body that
  is not valid UTF-8, routed to a body model with no JSON content type,
  so the ``input`` is the raw bytes rather than a parsed value) with
  ``backslashreplace``, for the same reason: ``jsonable_encoder``'s
  default ``bytes`` encoder is a strict ``.decode()``, which raises
  ``UnicodeDecodeError`` and the catch-all answers ``500`` for what is,
  like a non-finite float, a caller error that belongs at 422. A NUL
  byte or an unpaired UTF-16 surrogate (issue #869 F5) is a further way
  the same ``detail[].input`` echo breaks rendering -- pydantic already
  rejects the value with a ``string_unicode`` error, but that error's
  own ``input`` carries the raw surrogate forward, and
  ``str.encode("utf-8")`` refuses a lone surrogate outright
  (``allow_nan`` does not apply; there is no token to substitute). This
  handler checks for that case first and answers 400 without building
  the 422 body at all, so the unsafe value never reaches a renderer.

Health and version probes pass through the middleware too; the
overhead is a ULID + a contextvar bind, both microsecond-scale.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import structlog
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from trellis.core.error_sanitize import sanitize_error_message
from trellis.core.ids import generate_ulid
from trellis.errors import ConfigError, PublicReadBusyError, TrellisError
from trellis.mutate.handlers import _find_unsafe_jsonb_string
from trellis_wire.feedback_hints import rating_out_of_range_hint

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


#: pydantic v2's ``type`` for a ``Field(ge=...)`` / ``Field(le=...)`` miss.
_RANGE_ERROR_TYPES = frozenset({"greater_than_equal", "less_than_equal"})


def _with_rating_hint(error: dict[str, Any]) -> dict[str, Any]:
    """Append a unit-mismatch guess to a ``rating`` range error's ``msg``.

    Only a ``rating`` field's own ``ge``/``le`` miss qualifies — this is
    not a new check, the ``Field(ge=0.0, le=1.0)`` constraint already
    rejected the value; this only makes its message name the likely fix.
    ``input`` is a bool for a ``True``/``False`` rating (bool subclasses
    int in Python) and never numeric otherwise once this type matched, so
    the bool guard keeps ``rating_out_of_range_hint`` from being asked
    about a value it was not written for.
    """
    loc = error.get("loc") or ()
    value = error.get("input")
    if (
        error.get("type") not in _RANGE_ERROR_TYPES
        or not loc
        or loc[-1] != "rating"
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        return error
    hint = rating_out_of_range_hint(float(value))
    if hint is None:
        return error
    return {**error, "msg": f"{error['msg']}; rating is 0.0-1.0; {hint}"}


def _json_safe_float(value: float) -> float | str:
    """A non-finite float as the token the caller sent; any other unchanged.

    The spellings are the ones Python's ``json`` module parsed, and the
    ones pydantic writes under ``ser_json_inf_nan="strings"``.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _json_safe_bytes(value: bytes) -> str:
    """Undecodable ``bytes`` as a legible string; never raises.

    ``jsonable_encoder``'s own default for ``bytes`` is ``o.decode()`` —
    strict UTF-8 — which raises ``UnicodeDecodeError`` for a body that
    is not valid UTF-8 and ``input`` holds the raw bytes (a body routed
    to a model with no JSON content type, so there was no JSON parse to
    fail on first). ``backslashreplace`` renders every byte, valid or
    not, as an escape the caller can read back as the exact value sent —
    unlike ``errors="replace"``, which collapses any run of bad bytes
    into indistinguishable U+FFFD characters and so cannot tell a caller
    which bytes, or how many, were rejected.
    """
    return value.decode(errors="backslashreplace")


async def request_validation_error_handler(
    request: Request,  # noqa: ARG001 — Starlette's handler signature; the 422 needs nothing from it
    exc: Exception,
) -> JSONResponse:
    """FastAPI's 422, with a non-finite float or undecodable bytes legible.

    The same status and body as FastAPI's
    ``request_validation_exception_handler`` — ``{"detail": [...]}`` from
    ``exc.errors()`` through ``jsonable_encoder`` — with two differences:
    a non-finite float anywhere in the errors, at any depth, is rendered
    as its token, and undecodable ``bytes`` are rendered with
    ``backslashreplace`` instead of raising. A missing-field error echoes
    the whole body as its ``input``, so a non-finite number or a bad byte
    anywhere in a body missing a field reaches the renderer, not only one
    in the field that failed. A finite float or decodable bytes value is
    returned unchanged (decoded the same way ``jsonable_encoder`` would
    decode it), so every such case *with no rating unit-mismatch guess*
    is FastAPI's own response, byte for byte
    (``tests/unit/api/test_non_finite_body.py``).

    A ``rating`` field rejected for being out of ``[0.0, 1.0]`` gains a
    unit-mismatch guess appended to its own ``msg`` (:func:`_with_rating_hint`)
    when the value looks like a ``/10`` or ``/100`` score; every other
    error, and a ``rating`` miss with no such guess, is unchanged.

    A NUL byte or an unpaired UTF-16 surrogate anywhere in ``exc.errors()``
    (issue #869 F5) is checked first and answered as 400 before any 422
    body is built. The two hazards are not the same kind: a lone surrogate
    cannot round-trip through ``str.encode("utf-8")``, so there genuinely
    is no finite-float-style substitute token to write in its place, and
    the value must never reach a response body at all. A NUL byte, by
    contrast, renders in this handler's own JSON response without any
    trouble -- ``json.dumps`` escapes it as ``\u0000`` like any other
    control character, so a NUL alone would not force this 400. It is
    blocked here anyway, for the same reason ``_find_unsafe_jsonb_string``
    blocks it everywhere else it is called (the Apply/Revoke audit probe,
    the classify-candidate dismiss route): a NUL that reaches this far is
    unwritable to a Postgres ``jsonb`` column, and this handler cannot
    know whether a deployment logs, forwards, or otherwise persists a
    validation error's echoed ``input``. Blocking both hazards at the one
    boundary every such value passes through is simpler than auditing
    every downstream consumer for its own NUL guard, so the status for a
    NUL-containing body is 400, not FastAPI's usual 422, as a deliberate
    choice to fail closed here rather than a claim that the NUL itself
    breaks rendering in this handler.
    """
    # Registered only for RequestValidationError; the check narrows the
    # type Starlette's handler signature widens.
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    errors = [_with_rating_hint(error) for error in errors]
    unsafe = _find_unsafe_jsonb_string(errors)
    if unsafe is not None:
        path, _value = unsafe
        logger.warning("api_unsafe_request_body", path=path)
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "request body contains a NUL byte or an unpaired "
                    "UTF-16 surrogate that cannot be represented; the "
                    "value is not echoed"
                )
            },
        )
    detail = jsonable_encoder(
        errors, custom_encoder={float: _json_safe_float, bytes: _json_safe_bytes}
    )
    return JSONResponse(status_code=422, content={"detail": detail})


#: Status for a :class:`~trellis.errors.ConfigError` reaching the boundary.
#:
#: 409, and the choice is between this and keeping the 500, not between
#: this and 503 — ``routes/policies.py::_refusal_http_error`` already ruled
#: 503 out for the same file: a damaged config file does not repair itself,
#: so "retry later" is the wrong instruction. What settles it for 409 is
#: that ``GET /api/v1/policies/{policy_id}`` **already answers 409** when
#: that very file is damaged, from a *read* route where nothing was being
#: written. One deployment fault answering 409 on the one route that
#: escalates it and 500 on every governed write is a distinction no client
#: can act on.
#:
#: The precedent is that single-item read and **not** the collection route:
#: ``GET /api/v1/policies`` answers **200** on a damaged file, serving what
#: parsed with a ``store_degradation`` block beside it. That is deliberate
#: and stays — display degrades, enforcement fails closed, and the two
#: routes read the file through different loaders (``PolicyStore`` is
#: lenient, ``mutate/policy_source`` is strict). So this handler does not
#: make one status universal for the fault; it makes every *governed
#: mutation* answer the same way, which is the population a client
#: retries.
#:
#: The obvious objection — a 4xx will not trip a monitor watching 5xx, and
#: this fault darkens every governed write — is answered by #458 rather
#: than by the status code: the outage is visible as a ``WRITE_REJECTED``
#: event under ``config:policy_file``, which ``trellis analyze health`` and
#: the capture-health banner both read. Visibility rides the event log;
#: this handler is only the legibility half.
#:
#: It is also not the only channel, and the second one is stronger than
#: the argument above: this handler logs at ``error`` on **every** typed
#: failure including the 409s, so an operator alerting on ``error``-level
#: log lines sees ``api_trellis_error`` once per affected request, with the
#: exception's type, code and message. If a deployment would rather have
#: the 5xx rate move, the whole decision is one line — ``CONFIG_ERROR_STATUS
#: = 500`` — and nothing else changes: the body stays legible, because the
#: legibility lives in this handler and not in the catch-all.
CONFIG_ERROR_STATUS = 409

#: A public-only read (an ``external`` key) refused at the process's limit on
#: concurrent public reads (:class:`~trellis.errors.PublicReadBusyError`). The
#: refusal is immediate and carries ``Retry-After``.
PUBLIC_READ_BUSY_STATUS = 429

#: Fields the typed hierarchy carries that a caller can act on. Read off
#: the exception, so a subclass that adds one is carried without an edit
#: here. ``setting`` names the config key or file, ``path`` the file,
#: ``recovery`` the shell command that clears the state, ``store`` the
#: backend.
#:
#: They are Trellis-authored *names*, but their values are not: ``path``
#: and ``recovery`` are built from a resolved filesystem path, so they are
#: exception content and go through the same #206 guard the message does.
#: Sanitizing the message and shipping the identical text in a sibling key
#: is not a leak guard — it is one, defeated, in the same response body.
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
    if isinstance(exc, PublicReadBusyError):
        return PUBLIC_READ_BUSY_STATUS
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
    wrote that guard for. The log line under the same ``request_id``
    records the exception's type, code and message, and no traceback: a
    store that maps a driver failure raises a type-only ``StoreError``
    chained from the driver's exception, because a server's text can carry
    query text and values (#702, #713), and a rendered traceback would
    print that chained cause.

    Registered for :class:`~trellis.errors.TrellisError`, so Starlette's
    MRO lookup routes every subclass here and the catch-all keeps only
    what it should have had all along: genuinely untyped failures.
    """
    request_id = getattr(request.state, "request_id", None)
    trellis_exc = exc if isinstance(exc, TrellisError) else TrellisError(str(exc))
    status_code = _error_status(trellis_exc)
    # No traceback on purpose; see the docstring. An untyped failure still
    # reaches ``unhandled_exception_handler``, which logs its traceback.
    logger.error(
        "api_trellis_error",
        path=request.url.path,
        method=request.method,
        request_id=request_id,
        exc_type=type(exc).__name__,
        error=trellis_exc.message,
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
        if value is None:
            continue
        # Same guard as the message: these carry a resolved filesystem
        # path, and a clean one passes through untouched (the sanitizer's
        # token heuristic is broken by ``/`` and ``.`` on purpose).
        content[attr] = (
            sanitize_error_message(value) if isinstance(value, str) else value
        )
    headers = {REQUEST_ID_HEADER: request_id} if request_id else {}
    if isinstance(trellis_exc, PublicReadBusyError):
        headers["Retry-After"] = str(trellis_exc.retry_after)
    return JSONResponse(status_code=status_code, content=content, headers=headers)
