"""Follow-up 1 to gate #765: an exception a tool does not catch still
reaches the caller through FastMCP's own ``call_tool`` wrapping, which
renders any uncaught exception as ``Error calling tool '<name>': {e}``
(``mask_error_details`` defaults to ``False``) with the exception's raw
text embedded, unsanitized. #765's AST roster
(``test_exception_text_roster.py``) scans ``except`` handlers in
``src/trellis/mcp/server.py``, so it cannot see a tool with no handler at
all: ``save_knowledge``'s and ``save_experience``'s own ``executor.execute``
calls have none.

The fix is FastMCP-level middleware (``_sanitize_uncaught_tool_errors``),
wrapping every tool call site, not a per-tool ``try/except`` — so these
tests drive the real dispatch through an in-memory ``fastmcp.Client``
(``Client(server_mod.mcp)``). ``test_store_error_sanitization.py``'s
``unwrap_tool`` pattern calls the bare function directly and never reaches
FastMCP's wrapping layer at all, so it cannot exercise this fix.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

import trellis.mcp.server as server_mod
from trellis.errors import StoreError
from trellis.mcp.auth import set_auth_enforced
from trellis.stores.registry import StoreRegistry

SECRET = "synthetic-g765f1-secret-7h2k"  # noqa: S105 — synthetic, not a real credential
DRIVER_TEXT = (
    'duplicate key value violates unique constraint "nodes_name_key"\n'
    f"DETAIL:  Key (name)=({SECRET}) already exists."
)

#: Passes Trace schema validation, so save_experience reaches its unguarded
#: ``executor.execute`` instead of failing earlier at the JSON-parse step.
VALID_TRACE_JSON = (
    '{"source": "agent", "intent": "probe", "context": {"domain": "probe"}}'
)


class _FakeDriverError(Exception):
    """Not a ``TrellisError`` and not in ``executor._UNEXPECTED_HANDLER_FAILURE``
    — escapes a tool with no try/except exactly as a real driver error would."""


class _ExplodingExecutor:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def execute(self, command: Any) -> Any:
        raise self._exc


def _force_executor_explosion(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    monkeypatch.setattr(
        server_mod,
        "build_curate_executor",
        lambda *args, **kwargs: _ExplodingExecutor(exc),
    )


@pytest.fixture(autouse=True)
def _allow_in_memory_client() -> None:
    """``Client(server_mod.mcp)`` is neither stdio nor an authenticated
    transport; opt out of per-tool scope checks the same way stdio does
    (see the module docstring in ``trellis.mcp.auth``)."""
    set_auth_enforced(enforced=False)


async def _call(name: str, args: dict[str, Any]) -> Any:
    async with Client(server_mod.mcp) as client:
        return await client.call_tool(name, args, raise_on_error=False)


class TestUncaughtExceptionIsSanitized:
    """Re-measurement + regression pin for the two unguarded sites found in
    gate #765's follow-up 1 (save_knowledge) and this PR's own re-measure
    (save_experience, same shape)."""

    async def test_save_knowledge_drops_the_secret_but_names_the_tool(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_executor_explosion(monkeypatch, _FakeDriverError(DRIVER_TEXT))
        result = await _call("save_knowledge", {"name": "n1"})
        text = result.content[0].text
        assert result.is_error
        assert SECRET not in text
        assert "save_knowledge" in text
        assert "Error calling tool" in text

    async def test_save_experience_drops_the_secret_but_names_the_tool(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_executor_explosion(monkeypatch, _FakeDriverError(DRIVER_TEXT))
        result = await _call("save_experience", {"trace_json": VALID_TRACE_JSON})
        text = result.content[0].text
        assert result.is_error
        assert SECRET not in text
        assert "save_experience" in text
        assert "Error calling tool" in text


class TestUncaughtTrellisErrorKeepsItsOwnText:
    """A TrellisError that escapes uncaught is still Trellis's own text —
    #765's ``_exception_detail`` passes a TrellisError through unchanged,
    and the choke point must not start sanitizing it just because it was
    never caught.

    The message below is deliberately secret-*shaped* (a ``token=...``
    assignment, one of ``sanitize_error_message``'s own leak heuristics)
    so this test fails loudly if the choke point ever routes a
    TrellisError through the generic sanitizer instead of
    ``_exception_detail``'s carve-out — a message with nothing to trip
    the heuristic would pass either way and prove nothing."""

    async def test_save_knowledge_trellis_error_text_survives(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        message = f"store unavailable: token={SECRET}-should-not-be-masked"
        _force_executor_explosion(monkeypatch, StoreError(message))
        result = await _call("save_knowledge", {"name": "n2"})
        text = result.content[0].text
        assert result.is_error
        assert message in text
        assert "save_knowledge" in text


class TestCaughtPathsUnchangedByTheNewMiddleware:
    """#765's own sanitized sites, and ordinary success, must render
    identically with the new middleware installed — it must touch only the
    exception shape no tool handler ever caught."""

    async def test_save_memory_caught_error_reply_is_unchanged(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_executor_explosion(monkeypatch, _FakeDriverError(DRIVER_TEXT))
        result = await _call("save_memory", {"content": "whatever content"})
        text = result.content[0].text
        assert result.is_error
        assert SECRET not in text
        assert "save_memory" in text
        assert "governed memory write failed" in text
        assert "[error detail suppressed: potentially sensitive content]" in text

    async def test_save_memory_success_reply_is_unchanged(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = await _call("save_memory", {"content": "a normal memory"})
        assert not result.is_error
        text = result.content[0].text
        assert text.startswith("Memory saved: ")

    async def test_save_knowledge_success_reply_is_unchanged(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = await _call("save_knowledge", {"name": "entity-ok"})
        assert not result.is_error
        text = result.content[0].text
        assert text.startswith("Entity created: ")
