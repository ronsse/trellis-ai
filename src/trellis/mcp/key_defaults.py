"""Fill a key's server-side defaults into an MCP tool call that omitted them.

One seam for the whole MCP surface: a middleware reads the caller's defaults
block off its bearer token (:func:`trellis.mcp.auth.current_key_defaults`) and
fills each defaultable argument the call left out before the tool runs. A field
the call set is never replaced, and a key that set no defaults (every key minted
before this module existed) takes an early return that touches nothing, so its
request, pack and rendered text are exactly what they were.

Which argument a default feeds is an explicit table, :data:`TOOL_DEFAULT_FIELDS`,
not a scan of tool signatures, so a new tool taking ``domain`` or ``session_id``
is a visible decision; ``tests/unit/mcp/test_key_defaults.py`` fails when the
table and the registered tools disagree. No MCP tool takes ``agent_id`` or
``team`` today (the server derives the caller's identity from the key), so those
two defaults have no MCP consumer yet.

``session_id`` is filled with ``<prefix>-<random>`` when the key sets a
``session_id_prefix``. A fixed key-wide session would make unrelated
conversations share a dedup window and withhold items from each other; a fresh
id per call dedups nothing, which is what an omitted ``session_id`` already
means, and a caller that wants dedup across calls passes its own.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware

from trellis.auth import fill_omitted
from trellis.mcp.auth import current_key_defaults

if TYPE_CHECKING:
    from fastmcp.server.middleware import CallNext, MiddlewareContext

#: Tool name -> the arguments a key default may fill.
TOOL_DEFAULT_FIELDS: dict[str, tuple[str, ...]] = {
    "get_context": ("domain", "session_id"),
    "get_objective_context": ("domain", "session_id"),
    "get_task_context": ("domain", "session_id"),
    "get_sectioned_context": ("domain", "session_id"),
    "get_lessons": ("domain",),
}

#: The argument names the table may feed; the completeness test scans every
#: registered tool's schema for exactly these.
DEFAULTABLE_ARGUMENTS = frozenset({"agent_id", "domain", "session_id"})


def _default_for(argument: str, defaults: dict[str, str]) -> str | None:
    if argument == "session_id":
        prefix = defaults.get("session_id_prefix")
        return f"{prefix}-{secrets.token_hex(6)}" if prefix else None
    return defaults.get(argument)


def fill_arguments(
    tool: str, arguments: dict[str, Any] | None, defaults: dict[str, str]
) -> dict[str, Any] | None:
    """``arguments`` with the key's defaults filled in, or ``None`` if nothing changed.

    Pure so a test can drive it without a server. An argument counts as omitted
    when absent, ``None`` or blank, the same reading every tool already gives it.
    """
    fields = TOOL_DEFAULT_FIELDS.get(tool)
    if not fields or not defaults:
        return None
    current = dict(arguments or {})
    changed = False
    for argument in fields:
        raw = current.get(argument)
        if raw is not None and not isinstance(raw, str):
            continue  # not a string: validation reports it, a default never hides it
        filled = fill_omitted(raw, _default_for(argument, defaults))
        if filled is not None and filled != raw:
            current[argument] = filled
            changed = True
    return current if changed else None


class KeyDefaultsMiddleware(Middleware):
    """Apply the calling key's defaults to a tool call that omitted them."""

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        defaults = current_key_defaults()
        if defaults:
            filled = fill_arguments(
                context.message.name, context.message.arguments, defaults
            )
            if filled is not None:
                context = context.copy(
                    message=context.message.model_copy(update={"arguments": filled})
                )
        return await call_next(context)
