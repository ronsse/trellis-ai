"""Error shaping for the MCP tool boundary.

The app-layer JSON-RPC error codes and the ``_raise_*`` helpers every tool uses to
reject a call.
"""

from __future__ import annotations

from typing import Any, NoReturn

from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData

from trellis.core.error_sanitize import render_exception_detail, sanitize_error_message
from trellis.mcp.auth import current_clearance
from trellis.mutate import CommandResult, CommandStatus
from trellis.retrieve.pack_builder import RETRIEVAL_PHASES
from trellis.retrieve.sensitivity import is_public_only

# ---------------------------------------------------------------------------
# Custom JSON-RPC error codes (app-layer, -32000..-32099 reserved range)
# ---------------------------------------------------------------------------

#: Caller asked for an entity / resource that does not exist.
RESOURCE_NOT_FOUND = -32001

#: A policy gate denied the operation.
POLICY_DENIED = -32002

#: A governed mutation executed but came back non-success.
MUTATION_FAILED = -32003

#: A read for a key that reads only public documents was refused because the
#: process is at its limit on concurrent public reads
#: (:class:`~trellis.errors.PublicReadBusyError`). The error text
#: carries ``retry_after_seconds=N`` (the wire has the text only); nothing ran.
PUBLIC_READ_BUSY = -32004


def _raise_invalid_params(
    message: str, *, data: dict[str, Any] | None = None
) -> NoReturn:
    """Raise ``MCPError(INVALID_PARAMS, …)`` — short for the common case."""
    raise MCPError.from_error_data(
        ErrorData(code=INVALID_PARAMS, message=message, data=data)
    )


def _check_retrieval_phase(retrieval_phase: str | None) -> None:
    """Refuse a stated-but-unrecognised ``retrieval_phase`` at the tool boundary.

    ``None`` (not stated) always passes. Checked here, before the call
    reaches :class:`PackBuilder`, so an unknown value is refused with
    ``INVALID_PARAMS`` and a clear message rather than surfacing as an
    opaque ``INTERNAL_ERROR`` once the library's own ``ValueError`` is
    caught by the generic exception handler below.
    """
    if retrieval_phase is not None and retrieval_phase not in RETRIEVAL_PHASES:
        _raise_invalid_params(
            f"retrieval_phase must be one of {sorted(RETRIEVAL_PHASES)} or "
            f"omitted; got {retrieval_phase!r}",
            data={"retrieval_phase": retrieval_phase},
        )


def _raise_internal(
    message: str,
    *,
    cause: BaseException | None = None,
    data: dict[str, Any] | None = None,
) -> NoReturn:
    """Raise ``MCPError(INTERNAL_ERROR, …)`` chaining the cause if given.

    Centralising this keeps the ``from exc`` chaining consistent — losing
    the chain hides the original traceback from operator logs.
    """
    err = MCPError.from_error_data(
        ErrorData(code=INTERNAL_ERROR, message=message, data=data)
    )
    if cause is not None:
        raise err from cause
    raise err


def _raise_not_found(message: str, *, data: dict[str, Any] | None = None) -> NoReturn:
    """Raise ``MCPError(RESOURCE_NOT_FOUND, …)`` — app-layer code."""
    raise MCPError.from_error_data(
        ErrorData(code=RESOURCE_NOT_FOUND, message=message, data=data)
    )


def _raise_mutation_failed(
    message: str, *, data: dict[str, Any] | None = None
) -> NoReturn:
    """Raise ``MCPError(MUTATION_FAILED, …)`` — app-layer code."""
    raise MCPError.from_error_data(
        ErrorData(code=MUTATION_FAILED, message=message, data=data)
    )


def _raise_policy_denied(
    message: str, *, data: dict[str, Any] | None = None
) -> NoReturn:
    """Raise ``MCPError(POLICY_DENIED, …)`` — app-layer code.

    For a scope decision made *before* anything reaches the executor —
    today, ``execute_mutation`` refusing a destructive operation without
    the ``admin`` scope. The call never built a ``Command`` and nothing
    ran, which is why this is a different code from
    :func:`_raise_mutation_failed`.
    """
    raise MCPError.from_error_data(
        ErrorData(code=POLICY_DENIED, message=message, data=data)
    )


def _refuse_public_caller(tool: str) -> None:
    """Refuse ``tool`` to a public-only caller (an ``external`` key), or return.

    For a tool that reads something with no sensitivity label (the graph, a
    trace, a precedent, an observation, file context). A ``public`` caller is
    only ever shown a stored document someone labelled ``public``, so these are
    refused with a stated reason rather than filtered to nothing silently. The
    reason is the same on every refused tool and names no content.
    """
    if is_public_only(current_clearance()):
        _raise_policy_denied(
            f"{tool} is not available to an external key: it reads content that "
            "cannot carry a public label. An external key can use get_context, "
            "search, get_sectioned_context, get_objective_context, "
            "get_task_context and get_items, and sees only documents labelled "
            "public.",
            data={"tool": tool, "reason": "external_key"},
        )


def _exception_detail(exc: BaseException) -> str:
    """Render a caught exception for a caller-facing message."""
    return render_exception_detail(exc)


#: A FAILED/REJECTED ``CommandResult.message`` can carry driver or caller
#: text (a store's rejection detail); SUCCESS and DUPLICATE only restate
#: the caller's own request, so they pass through unsanitized, matching
#: ``_results.py``'s ``_SANITIZED_STATUSES`` on the REST boundary (#836).
_SANITIZED_RESULT_STATUSES = frozenset({CommandStatus.FAILED, CommandStatus.REJECTED})


def _result_message(result: CommandResult) -> str:
    """Sanitize ``result.message`` for a caller-facing reply, gated by
    ``result.status`` rather than wrapped unconditionally (#836's
    convention, applied to the MCP tool surface)."""
    if result.status in _SANITIZED_RESULT_STATUSES:
        return sanitize_error_message(result.message)
    return result.message
