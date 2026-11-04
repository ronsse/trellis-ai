"""Boundary rejection recording and the tool-call middleware.

Holds the rejection recorder, the accepted/no-op call notes, and the two middleware
that wrap every tool call: sanitising an uncaught tool error and explaining an
argument rejection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from pydantic import ValidationError

from trellis.errors import PublicReadBusyError
from trellis.mcp.boundary import PUBLIC_READ_BUSY, _exception_detail
from trellis.mcp.wiring import _get_registry
from trellis.ops.write_health import NoopReason, RejectionRecoveryTracker

if TYPE_CHECKING:
    import mcp.types as mcp_types
    from fastmcp import FastMCP
    from fastmcp.tools.base import ToolResult

logger = structlog.get_logger(__name__)


#: Pairs a boundary rejection with the accepted call that follows it, per
#: tool and in memory. See
#: :class:`trellis.ops.write_health.RejectionRecoveryTracker` for what it
#: keeps (a tool name, a count and a time) and why it cannot key by caller.
_REJECTION_RECOVERY = RejectionRecoveryTracker()


def _record_boundary_rejection(
    *,
    tool: str,
    error: Exception | None = None,
    rejections: list[dict[str, str]] | None = None,
    hints: list[str] | None = None,
    payload_chars: int | None = None,
) -> dict[str, Any]:
    """Record a tool-boundary rejection as a ``WRITE_REJECTED`` event.

    The executor audits every stage after a Command exists; this covers
    the stage before one does. Doubly fail-soft — a missing registry or a
    broken event log degrades to classification-only, because telemetry
    must never turn a rejected write into a crashed tool. Returns
    ``{rejections, hints}`` for folding into the raised error.
    """
    from trellis.ops.write_health import record_write_rejection  # noqa: PLC0415

    event_log = None
    try:
        event_log = _get_registry().operational.event_log
    except Exception:  # pragma: no cover - registry bootstrap failure
        logger.warning("write_rejection.registry_unavailable", tool=tool)
    return record_write_rejection(
        event_log,
        tool=tool,
        error=error,
        rejections=rejections,
        hints=hints,
        payload_chars=payload_chars,
        recovery=_REJECTION_RECOVERY,
    )


def _note_accepted_call(tool: str) -> None:
    """Emit ``WRITE_RECOVERED`` when a call is accepted right after rejections.

    Called with the name of a tool whose call just returned without raising.
    A tool that recorded no rejection recently costs one dict lookup. Wrapped
    whole, like the rest of the write-health telemetry: pairing is advisory,
    and an escaped exception here would fail a write that already succeeded.
    """
    try:
        from trellis.ops.write_health import (  # noqa: PLC0415
            record_write_recovery,
            surface_label,
        )

        rejected = _REJECTION_RECOVERY.claim_recovery(surface_label(tool))
        if rejected:
            record_write_recovery(
                _get_registry().operational.event_log,
                tool=tool,
                rejected_attempts=rejected,
                window_seconds=_REJECTION_RECOVERY.window_seconds,
            )
    except Exception:
        logger.warning("write_recovery.pairing_failed", tool=tool, exc_info=True)


def _note_noop_call(tool: str, reason: NoopReason) -> None:
    """Record that ``tool`` answered success without writing anything.

    A duplicate reply leaves no ``MUTATION_EXECUTED``, so without this row the
    call is missing from the attempts a rejection rate is divided by (see
    :func:`trellis.ops.write_health.record_write_noop`). Wrapped whole, like
    :func:`_note_accepted_call`: the reply already exists and is not this
    function's to change, so nothing here may raise, including a registry that
    cannot be built.
    """
    try:
        from trellis.ops.write_health import record_write_noop  # noqa: PLC0415

        record_write_noop(
            _get_registry().operational.event_log, tool=tool, reason=reason
        )
    except Exception:
        logger.warning("write_noop.record_failed", tool=tool, exc_info=True)


class _SanitizeUncaughtToolErrors(Middleware):
    """Render an exception no tool caught through ``_exception_detail`` too.

    FastMCP's own tool dispatch wraps any exception a tool does not catch
    as ``ToolError(f"Error calling tool {name!r}: {e}") from e`` (its
    ``mask_error_details`` defaults to ``False``), embedding ``str(e)`` raw
    — a Postgres DETAIL line or a Neo4j constraint message included. The 14
    sites ``_exception_detail`` already covers (#765) raise an
    already-sanitized ``McpError`` from inside their own ``try/except``, so
    by the time FastMCP wraps it here the embedded text is already safe. A
    tool with no ``try/except`` at all (``save_knowledge``'s and
    ``save_experience``'s own ``executor.execute`` calls) has nothing
    upstream to sanitize it, and the cause reaches this wrapping raw.

    Only one level of ``__cause__`` is inspected, following
    ``fastmcp.server.middleware.error_handling.RetryMiddleware``'s own
    precedent: middleware below this one must not re-wrap with a new
    ``from`` clause, or the real cause is hidden here too. Left untouched,
    by design:

    * ``cause is None`` — a tool (or FastMCP itself) raised ``ToolError``
      directly; its author already chose the message.
    * ``isinstance(cause, McpError)`` — one of the 14 sites above (``McpError``
      is not a ``fastmcp.exceptions.FastMCPError`` subclass, so it reaches
      this same generic wrap already-safe); re-running ``_exception_detail``
      would be redundant and, for a validation message that is the caller's
      own input and was never routed through ``_exception_detail`` at all,
      could mangle text deliberately preserved verbatim.
    * the message does not start with FastMCP's own
      ``"Error calling tool {name!r}:"`` prefix — the two actionable
      messages ``call_tool`` builds for an ``httpx`` rate-limit or timeout
      (chained with ``from e`` too) carry no raw driver text to strip, and
      rewriting them would lose the retry hint they exist to give.

    Everything else is FastMCP's generic wrap of some other uncaught
    exception, Trellis's own (``TrellisError``) or not — exactly
    ``_exception_detail``'s two cases, so it decides what survives.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ToolError as exc:
            cause = exc.__cause__
            name = context.message.name
            prefix = f"Error calling tool {name!r}:"
            if isinstance(cause, PublicReadBusyError):
                raise McpError(
                    ErrorData(
                        code=PUBLIC_READ_BUSY,
                        # The wire carries ``isError`` and this text only (the
                        # SDK renders any handler exception as ``str(exc)``), so
                        # the reason and the retry hint are in the text too.
                        message=(
                            f"public_read_busy: {cause.message} "
                            f"(retry_after_seconds={cause.retry_after})"
                        ),
                        data={
                            "reason": "public_read_busy",
                            "retry_after_seconds": cause.retry_after,
                        },
                    )
                ) from cause
            if (
                cause is None
                or isinstance(cause, McpError)
                or not str(exc).startswith(prefix)
            ):
                raise
            message = f"{prefix} {_exception_detail(cause)}"
            raise ToolError(message) from cause


#: The tools whose argument rejections are recorded as ``WRITE_REJECTED``.
#: Each already records its body-level rejections through
#: ``_record_boundary_rejection``, so ``test_capture_surface_roster.py``
#: already knows how its surface clears (a capture surface with an accept
#: recipe, or ``NON_CAPTURE_SURFACES``). ``record_observation`` and
#: ``execute_mutation`` are deliberately absent: neither records a boundary
#: rejection today, and ``execute_mutation`` stamps accepted writes with the
#: authenticated principal rather than ``mcp:execute_mutation``, so a banner
#: raised under that label could not clear. ``list_api_keys`` and
#: ``revoke_api_key`` are absent for the same reason: admin-scoped, no
#: boundary rejection recorded. A read tool gets the message and no event.
_RECORDED_ARGUMENT_TOOLS = frozenset(
    {"save_experience", "save_knowledge", "save_memory", "record_feedback"}
)


class _ExplainArgumentRejections(Middleware):
    """Tell a caller what a tool accepts when its arguments are wrong.

    FastMCP validates a call's arguments against the tool signature before
    the tool body runs, so a misspelt or mistyped argument never reaches the
    body's own handling. Left alone it comes back as pydantic's text, which
    names no accepted parameter and quotes the value the caller sent; and
    because the body never ran it left no ``WRITE_REJECTED`` event either.

    This rewrites that one failure (a pydantic ``ValidationError`` titled
    ``call[<tool>]``, found as the ``__cause__`` of the exception FastMCP
    raises) from the tool's own JSON schema, through
    :func:`trellis.ops.write_health.describe_argument_rejection`, and records
    it for a tool in :data:`_RECORDED_ARGUMENT_TOOLS`. It never loosens
    anything: the call is still rejected, with the same ``ToolError`` type,
    and an unexpected shape passes through untouched.

    The rewrite is fail-soft as a whole: guidance is advisory, so a failure
    to build it re-raises the original error. It never renders the exception
    itself (``test_exception_text_roster.py``): the message is built from the
    error's structured ``errors()`` rows and the schema.
    """

    def __init__(self, server: FastMCP) -> None:
        self._server = server

    async def _explain(self, tool: str, cause: BaseException | None) -> str | None:
        """The rewritten message, or ``None`` to leave the error as it is."""
        if not isinstance(cause, ValidationError) or not cause.title.startswith(
            "call["
        ):
            return None
        try:
            from trellis.ops.write_health import (  # noqa: PLC0415
                describe_argument_rejection,
            )

            declared = await self._server.get_tool(tool)
            if declared is None:
                return None
            guidance = describe_argument_rejection(tool, declared.parameters, cause)
            if tool in _RECORDED_ARGUMENT_TOOLS:
                _record_boundary_rejection(
                    tool=tool,
                    error=cause,
                    rejections=guidance.rows,
                    hints=guidance.hints,
                )
        except Exception:
            logger.warning("argument_rejection.guidance_failed", tool=tool)
            return None
        return guidance.message

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            result = await call_next(context)
        except Exception as exc:
            message = await self._explain(context.message.name, exc.__cause__)
            if message is None:
                raise
            raise ToolError(message) from exc.__cause__
        # "Accepted" here means the call returned without raising, which is
        # looser than "wrote something": a duplicate reply that stored nothing
        # and a feedback reply that says the event log was down both return.
        # Only the tools in ``_RECORDED_ARGUMENT_TOOLS`` ever have a rejection
        # to pair with, and their body-level rejections do raise;
        # ``record_observation`` and ``execute_mutation`` return some errors as
        # JSON, but neither records a boundary rejection, so none is tracked.
        _note_accepted_call(context.message.name)
        return result
