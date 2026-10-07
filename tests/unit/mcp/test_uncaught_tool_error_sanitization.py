"""An exception a tool does not catch reaches the caller through FastMCP's
own ``call_tool`` wrapping, which renders it as ``Error calling tool
'<name>': {e}`` (``mask_error_details`` defaults to ``False``) with the
exception's raw text embedded. #765's AST roster
(``test_exception_text_roster.py``) scans ``except`` handlers in
``src/trellis/mcp/server.py``, so it cannot see a tool with no handler at
all: ``save_knowledge``'s and ``save_experience``'s own ``executor.execute``
calls have none.

The fix is FastMCP-level middleware (``_SanitizeUncaughtToolErrors``),
wrapping every tool call site, not a per-tool ``try/except`` — so these
tests drive the real dispatch through an in-memory ``fastmcp.Client``
(``Client(server_mod.mcp)``). ``test_store_error_sanitization.py``'s
``unwrap_tool`` pattern calls the bare function directly and never reaches
FastMCP's wrapping layer at all, so it cannot exercise this fix.

``TestCarveOutsLeaveTheirCauseUntouched`` below pins the middleware's three
documented carve-outs (``_SanitizeUncaughtToolErrors``'s own docstring),
which the dispatch tests above never exercise: no production tool raises a
bare ``ToolError``, an ``McpError`` already uncaught-safe, or an
``httpx`` rate-limit/timeout. Those tests build a *fresh* ``FastMCP``
instance with only the middleware under test and synthetic probe tools, so
a probe tool never registers on the shared global ``server_mod.mcp``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ErrorData

import trellis.mcp.server as server_mod
from trellis.errors import StoreError
from trellis.mcp.auth import set_auth_enforced
from trellis.stores.registry import StoreRegistry

SECRET = "synthetic-g765f1-secret-7h2k"  # noqa: S105 — synthetic, not a real credential
DRIVER_TEXT = (
    'duplicate key value violates unique constraint "nodes_name_key"\n'
    f"DETAIL:  Key (name)=({SECRET}) already exists."
)
SUPPRESSED = "[error detail suppressed: potentially sensitive content]"

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
    """``save_knowledge`` and ``save_experience`` call ``executor.execute``
    with no ``try/except``."""

    async def test_save_knowledge_drops_the_secret_but_names_the_tool(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_executor_explosion(monkeypatch, _FakeDriverError(DRIVER_TEXT))
        result = await _call("save_knowledge", {"name": "n1"})
        text = result.content[0].text
        assert result.is_error
        assert SECRET not in text
        assert text == f"Error calling tool 'save_knowledge': {SUPPRESSED}"

    async def test_save_experience_drops_the_secret_but_names_the_tool(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_executor_explosion(monkeypatch, _FakeDriverError(DRIVER_TEXT))
        result = await _call("save_experience", {"trace_json": VALID_TRACE_JSON})
        text = result.content[0].text
        assert result.is_error
        assert SECRET not in text
        assert text == f"Error calling tool 'save_experience': {SUPPRESSED}"


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


class TestCaughtPathsUnchangedByTheMiddleware:
    """#765's own sanitized sites, and ordinary success, must render
    identically with the middleware installed — it must touch only the
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
        assert SUPPRESSED in text

    async def test_save_knowledge_success_reply_is_unchanged(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = await _call("save_knowledge", {"name": "entity-ok"})
        assert not result.is_error
        text = result.content[0].text
        assert text.startswith("Entity created: ")


def _fresh_carve_out_app() -> FastMCP:
    """A ``FastMCP`` instance carrying only the middleware under test, so a
    synthetic probe tool never touches the shared ``server_mod.mcp``."""
    app = FastMCP("test-carve-outs")
    app.add_middleware(server_mod._SanitizeUncaughtToolErrors())
    return app


async def _call_fresh(
    app: FastMCP, name: str, args: dict[str, Any] | None = None
) -> Any:
    async with Client(app) as client:
        return await client.call_tool(name, args or {}, raise_on_error=False)


class TestCarveOutsLeaveTheirCauseUntouched:
    """The three cases ``_SanitizeUncaughtToolErrors`` leaves alone *by
    design* (its own docstring): ``cause is None``, ``isinstance(cause,
    McpError)``, and a cause whose ``ToolError`` text does not start with
    FastMCP's own ``"Error calling tool {name!r}:"`` prefix. #769's gate
    mutants M4 (the ``McpError`` carve-out removed) and M5 (the prefix gate
    removed) both survived the tests above — none of them drives a probe
    through either carve-out."""

    async def test_mcp_error_cause_is_not_resanitized(self) -> None:
        """An uncaught ``McpError`` is already one of #765's 14
        already-safe sites by the time it reaches this middleware, or a
        validation message that is the caller's own input and was never
        routed through ``_exception_detail`` at all — re-running
        ``_exception_detail`` on it would be redundant at best and could
        mangle deliberately-verbatim text at worst, so the carve-out skips
        it. The message below is shaped like one of
        ``sanitize_error_message``'s own leak heuristics (``token=...``)
        specifically so a second pass (what M4 does once the carve-out is
        removed) is visible: it would replace the text with the
        suppressed marker instead of leaving it exactly as the McpError's
        author wrote it.
        """
        message = f"token={SECRET}-should-not-be-resanitized"
        app = _fresh_carve_out_app()

        @app.tool
        def probe_mcp_error() -> str:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=message))

        result = await _call_fresh(app, "probe_mcp_error")
        text = result.content[0].text
        assert result.is_error
        assert text == f"Error calling tool 'probe_mcp_error': {message}"

    async def test_non_prefixed_cause_keeps_its_retry_hint(self) -> None:
        """``call_tool`` builds two actionable messages of its own for an
        ``httpx`` rate-limit or timeout (chained ``from e`` too), and
        neither starts with the generic wrap's prefix — rewriting them
        would throw away the retry hint they exist to give. Shape the
        ``httpx`` cause with a deny-listed fragment (``token=...``) so
        that if M5's prefix gate were removed, the retry message would be
        replaced by ``f"{prefix} {_exception_detail(cause)}"`` and the
        secret-shaped text would come back sanitized to the suppressed
        marker — a visibly different reply either way.
        """
        app = _fresh_carve_out_app()

        @app.tool
        def probe_rate_limited() -> str:
            request = httpx.Request("GET", "https://upstream.invalid/v1")
            response = httpx.Response(429, request=request, text="rate limited")
            message = f"rate limited: token={SECRET}-not-the-point"
            raise httpx.HTTPStatusError(message, request=request, response=response)

        result = await _call_fresh(app, "probe_rate_limited")
        text = result.content[0].text
        assert result.is_error
        assert SECRET not in text
        assert text == "Rate limited by upstream API, please retry later"

    async def test_bare_tool_error_with_no_cause_is_unchanged(self) -> None:
        """``cause is None`` means a tool (or FastMCP itself) raised
        ``ToolError`` directly with no ``from`` clause — its author
        already chose the message, so the carve-out must not touch it,
        however that message happens to read. The message below is
        deliberately shaped to start with FastMCP's own ``"Error calling
        tool {name!r}:"`` prefix, which isolates this carve-out from the
        prefix gate (``test_non_prefixed_cause_keeps_its_retry_hint``
        above pins that one on a message the prefix gate alone saves): a
        bare ``ToolError`` whose text happens to pass the prefix check
        too would, without the ``cause is None`` check, be rebuilt as
        ``f"{prefix} {_exception_detail(None)}"`` — literally ``None``
        where the author's own text, secret-shaped fragment included,
        used to be.
        """
        app = _fresh_carve_out_app()
        prefix = "Error calling tool 'probe_bare_tool_error':"
        message = f"{prefix} token={SECRET}-author-chose-this"

        @app.tool
        def probe_bare_tool_error() -> str:
            raise ToolError(message)

        result = await _call_fresh(app, "probe_bare_tool_error")
        text = result.content[0].text
        assert result.is_error
        assert text == message
