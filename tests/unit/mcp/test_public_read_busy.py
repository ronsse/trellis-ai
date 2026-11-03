"""An ``external`` key that is refused a public read gets a structured MCP error.

The refusal is a typed ``PublicReadBusyError`` raised below the tool. FastMCP
wraps whatever a tool does not catch as ``ToolError(...) from exc``, and the
sanitising middleware would then render the cause as text, losing the reason and
the retry hint. It re-raises this one as an ``McpError`` carrying
``PUBLIC_READ_BUSY`` and ``data.retry_after_seconds``.

What reaches a client is narrower than the ``McpError``: the SDK renders any
exception a tool call raises as ``isError`` plus ``str(exc)``, so ``code`` and
``data`` do not cross the wire. The text therefore carries the reason and the
retry hint (``public_read_busy: ... (retry_after_seconds=N)``), and the tests
below pin both the error the middleware builds and the text a client reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.test_sensitivity_surfaces import as_http
from trellis.errors import PublicReadBusyError
from trellis.mcp.auth import set_auth_enforced
from trellis.mcp.server import PUBLIC_READ_BUSY
from trellis.retrieve import public_rank
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator

BUSY = "public reads are busy"
UNAVAILABLE = "store unavailable"


@pytest.fixture(autouse=True)
def _enforce_scopes() -> Iterator[None]:
    """Scope enforcement on, as it is behind a real key.

    With it off the in-memory caller is trusted-local and reads everything, so
    no public-only path would run and the refusal would never be reached.
    """
    set_auth_enforced(enforced=True)
    try:
        yield
    finally:
        set_auth_enforced(enforced=True)


async def _outcome(app: FastMCP, name: str, args: dict[str, Any]) -> Any:
    async with Client(app) as client:
        try:
            return await client.call_tool(name, args, raise_on_error=False)
        except McpError as exc:
            return exc


def _wire_text(outcome: Any) -> str:
    """The text of the error result a client reads (it is all the wire carries)."""
    assert not isinstance(outcome, McpError), outcome
    assert outcome.is_error, outcome
    return str(outcome.content[0].text)


class TestTheMiddlewareRendersTheRefusal:
    async def test_it_builds_an_mcp_error_with_a_code_and_a_retry_hint(self) -> None:
        async def refused(_context: Any) -> Any:
            msg = "Error calling tool 'probe': public reads are busy"
            raise ToolError(msg) from PublicReadBusyError(
                "public reads are busy", retry_after=7
            )

        context = SimpleNamespace(message=SimpleNamespace(name="probe"))
        with pytest.raises(McpError) as caught:
            await server_mod._SanitizeUncaughtToolErrors().on_call_tool(
                context,  # type: ignore[arg-type]
                refused,
            )
        error = caught.value.error
        assert error.code == PUBLIC_READ_BUSY == -32004
        assert error.data == {"reason": "public_read_busy", "retry_after_seconds": 7}
        assert "busy" in error.message
        assert "retry_after_seconds=7" in error.message

    async def test_a_client_reads_the_reason_and_the_hint_in_the_error_text(
        self,
    ) -> None:
        app = FastMCP("busy-probe")
        app.add_middleware(server_mod._SanitizeUncaughtToolErrors())

        @app.tool
        def probe() -> str:
            raise PublicReadBusyError(BUSY, retry_after=7)

        text = _wire_text(await _outcome(app, "probe", {}))
        assert text.startswith("public_read_busy:")
        assert "retry_after_seconds=7" in text

    async def test_any_other_trellis_error_is_rendered_as_before(self) -> None:
        from trellis.errors import StoreError

        app = FastMCP("other-probe")
        app.add_middleware(server_mod._SanitizeUncaughtToolErrors())

        @app.tool
        def probe() -> str:
            raise StoreError(UNAVAILABLE)

        result = await _outcome(app, "probe", {})
        assert not isinstance(result, McpError)
        assert result.is_error
        assert "store unavailable" in result.content[0].text
        assert "public_read_busy" not in result.content[0].text


class TestARealToolOverTheLimit:
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("get_context", {"intent": "failover runbook"}),
            ("search", {"query": "failover"}),
        ],
    )
    async def test_the_public_read_tools_refuse_at_once_with_the_structured_error(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        tool: str,
        args: dict[str, Any],
    ) -> None:
        monkeypatch.setattr(public_rank, "MAX_CONCURRENT_PUBLIC_READS", 0)
        with as_http(["read", "external"]):
            text = _wire_text(await _outcome(server_mod.mcp, tool, args))
        assert text.startswith("public_read_busy:")
        assert (
            f"retry_after_seconds={public_rank.PUBLIC_READ_RETRY_AFTER_SECONDS}" in text
        )

    async def test_a_key_that_reads_more_than_public_is_never_counted(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(public_rank, "MAX_CONCURRENT_PUBLIC_READS", 0)
        with as_http(["read"]):
            result = await _outcome(
                server_mod.mcp, "get_context", {"intent": "failover runbook"}
            )
        assert not isinstance(result, McpError)
        assert not result.is_error
