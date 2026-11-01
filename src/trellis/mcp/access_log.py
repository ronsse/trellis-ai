"""MCP side of the per-key access log (:mod:`trellis.core.access_log`).

A FastMCP middleware: it runs around a tool call, outside the tool. For a read
tool called by a key (:func:`trellis.mcp.auth.current_principal_id`, ``None``
under stdio or with enforcement off, so a keyless call records nothing) it
opens a holder before the tool runs; the tool names the ids it served into the
holder (:func:`trellis.core.access_log.note_served_items`) from the structured
pack or items it already holds, before any formatting, and the middleware hands
the result to the writer thread as the tool returns. The tool's answer is
passed through untouched, and nothing is parsed out of it.

* **Off is a pass-through.** The knob is read before the holder is opened; off
  opens nothing and the tool's ``note_served_items`` call finds no holder.
* **A tool that does not fill the holder** (``get_file_context``, ``get_lessons``,
  ``get_graph``, ``query_observations``) records ``item_count: null`` and no ids.
* **A tool that raises** records nothing.
* **A worker thread is fine.** A sync tool runs on a thread that holds a copy
  of the context, and the copy points at the same holder object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from trellis.core.access_log import (
    ENTRY_OVERHEAD_BYTES,
    MAX_LOGGED_ITEM_IDS,
    SURFACE_MCP,
    ServedItems,
    access_log_enabled,
    begin_served_items,
    end_served_items,
    enqueue_key_read,
    signal_failure,
)
from trellis.mcp.auth import current_principal_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.registry import StoreRegistry

#: Every tool registered with ``trellis_scope(SCOPE_READ)``
#: (``tests/unit/mcp/test_access_log.py`` pins this against ``server.py``).
READ_TOOLS = frozenset(
    {
        "get_context",
        "get_file_context",
        "get_graph",
        "get_items",
        "get_lessons",
        "get_objective_context",
        "get_sectioned_context",
        "get_task_context",
        "query_observations",
        "search",
    }
)
#: Tools that name the items they served (``note_served_items`` in
#: ``server.py``); the rest record ``item_count: null``. Pinned by a test.
NAMING_TOOLS = frozenset(
    {
        "get_context",
        "get_items",
        "get_objective_context",
        "get_sectioned_context",
        "get_task_context",
        "search",
    }
)


class AccessLogMiddleware(Middleware):
    """Record one ``access.key_read`` row per authenticated read-tool call."""

    def __init__(self, registry_provider: Callable[[], StoreRegistry]) -> None:
        self._registry_provider = registry_provider

    def _open(self, tool: str) -> tuple[Any, ServedItems | None, Any, str | None]:
        """``(registry, holder, token, key id)``; ``None`` when nothing is recorded."""
        key_id = current_principal_id() if tool in READ_TOOLS else None
        if not key_id:
            return None, None, None, None
        registry = self._registry_provider()
        if not access_log_enabled(registry):
            return None, None, None, None
        holder, token = begin_served_items()
        return registry, holder, token, key_id

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        tool = context.message.name
        registry = holder = token = key_id = None
        try:
            registry, holder, token, key_id = self._open(tool)
        except Exception as exc:  # advisory step; never fail the read
            signal_failure(None, SURFACE_MCP, exc)
        try:
            result = await call_next(context)
        finally:
            if token is not None:
                end_served_items(token)
        if holder is not None:
            try:
                item_ids, item_count = holder.result()
                enqueue_key_read(
                    registry,
                    key_id=key_id,
                    surface=SURFACE_MCP,
                    route=tool,
                    items=lambda: (item_ids, item_count),
                    size=ENTRY_OVERHEAD_BYTES
                    + 64 * min(len(item_ids), MAX_LOGGED_ITEM_IDS),
                )
            except Exception as exc:  # advisory step; never fail the read
                signal_failure(registry, SURFACE_MCP, exc)
        return result
