"""MCP side of the per-key access log (:mod:`trellis.core.access_log`).

A FastMCP middleware: it runs around a tool call, outside the tool, so nothing
in ``trellis.retrieve`` or the tool bodies knows it exists. After a read tool
returns, it records one ``access.key_read`` row for the calling key
(:func:`trellis.mcp.auth.current_principal_id`, ``None`` under stdio or with
enforcement off, so a keyless call records nothing).

The tools answer in markdown, so the returned ids are read back off the two
item-line shapes the pack formatters write (:data:`_ITEM_LINE`); a tool whose
output carries neither records ``item_count: null`` rather than a guess.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import anyio.to_thread
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from trellis.core.access_log import SURFACE_MCP, record_key_read, signal_failure
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
#: Tools whose answer is a list of items: no ids found means zero items.
COUNTED_TOOLS = frozenset(
    {
        "get_context",
        "get_file_context",
        "get_items",
        "get_objective_context",
        "get_sectioned_context",
        "get_task_context",
    }
)
#: ``## [type] `id``` (a full item) or ``- `id` (type...)`` (an index line).
_ITEM_LINE = re.compile(r"^(?:## \[[^\]\n]*\] |- )`([^`\n]+)`(?: \(|$)", re.MULTILINE)


def returned_items(tool: str, text: str) -> tuple[list[str], int | None]:
    """``(ids, true count)`` read back off a tool's markdown answer."""
    ids = _ITEM_LINE.findall(text)
    if ids or tool in COUNTED_TOOLS:
        return ids, len(ids)
    return [], None


class AccessLogMiddleware(Middleware):
    """Record one ``access.key_read`` row per authenticated read-tool call."""

    def __init__(self, registry_provider: Callable[[], StoreRegistry]) -> None:
        self._registry_provider = registry_provider

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        result = await call_next(context)
        try:
            tool = context.message.name
            key_id = current_principal_id() if tool in READ_TOOLS else None
            if key_id:
                text = "\n".join(
                    getattr(block, "text", "") or "" for block in result.content
                )
                await anyio.to_thread.run_sync(self._record, key_id, tool, text)
        except Exception as exc:  # advisory step; never fail the read
            signal_failure(None, SURFACE_MCP, exc)
        return result

    def _record(self, key_id: str, tool: str, text: str) -> None:
        registry = None
        try:
            registry = self._registry_provider()
            record_key_read(
                registry,
                key_id=key_id,
                surface=SURFACE_MCP,
                route=tool,
                items=lambda: returned_items(tool, text),
            )
        except Exception as exc:  # advisory step; never fail the read
            signal_failure(registry, SURFACE_MCP, exc)
