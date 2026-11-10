"""The HTTP transport accepts MCP protocol revision ``2026-07-28``.

claude.ai sends ``Mcp-Protocol-Version: 2026-07-28``. On ``mcp`` 1.x every such
request was refused with HTTP 400 ``Unsupported protocol version`` before it
reached a tool, so a ``save_memory`` call with no fallback retry was lost.

These tests drive the real ``mcp`` app over HTTP, the way ``main()`` serves it
(``stateless_http=True``, ``auth_mode=required``), with a minted API key.

``ping`` is not a method at ``2026-07-28``: the revision drops it, so a ``ping``
at that version is refused as a request, not as a version. ``server/discover``
is the revision's liveness and capability probe, so the 200 regression check
uses it. Every request at ``2026-07-28`` carries the protocol version and the
client capabilities in ``params._meta``, and the method in an ``Mcp-Method``
header.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from starlette.testclient import TestClient

from trellis.auth import SCOPE_INGEST, SCOPE_READ
from trellis.mcp.auth import AUTH_MODE_REQUIRED, HttpSettings
from trellis.mcp.server import _configure_http_auth, mcp

from .conftest import mint

if TYPE_CHECKING:
    from collections.abc import Iterator

    from trellis.stores.registry import StoreRegistry

MODERN = "2026-07-28"
LEGACY = "2025-11-25"


@pytest.fixture
def client() -> Iterator[TestClient]:
    _configure_http_auth(
        HttpSettings(
            host="127.0.0.1", port=8421, path="/mcp", auth_mode=AUTH_MODE_REQUIRED
        )
    )
    app = mcp.http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as test_client:
        yield test_client


def _headers(token: str, version: str, **extra: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Mcp-Protocol-Version": version,
        **extra,
    }


def _modern_request(method: str, params: dict[str, Any] | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            **(params or {}),
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def _body(response: Any) -> dict[str, Any]:
    """The JSON-RPC message, whether the reply is plain JSON or one SSE event."""
    if response.headers["content-type"].startswith("text/event-stream"):
        data = [
            line.removeprefix("data:").strip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
        return json.loads(data[-1])
    return response.json()


def _call_tool(
    client: TestClient, token: str, name: str, arguments: dict[str, Any]
) -> Any:
    return client.post(
        "/mcp",
        headers=_headers(
            token, MODERN, **{"Mcp-Method": "tools/call", "Mcp-Name": name}
        ),
        json=_modern_request("tools/call", {"name": name, "arguments": arguments}),
    )


class TestTheModernRevisionIsAccepted:
    def test_server_discover_answers_200(
        self, client: TestClient, temp_registry: StoreRegistry
    ) -> None:
        token = mint(temp_registry, [SCOPE_READ])
        response = client.post(
            "/mcp",
            headers=_headers(token, MODERN, **{"Mcp-Method": "server/discover"}),
            json=_modern_request("server/discover"),
        )
        assert response.status_code == 200, response.text
        result = _body(response)["result"]
        assert MODERN in result["supportedVersions"]

    def test_the_request_from_the_gateway_log_is_not_a_version_refusal(
        self, client: TestClient, temp_registry: StoreRegistry
    ) -> None:
        """The exact request that got ``Unsupported protocol version`` on 1.x.

        It is still refused (bare ``ping`` has no envelope and no method at this
        revision), but no longer for its version.
        """
        token = mint(temp_registry, [SCOPE_READ])
        response = client.post(
            "/mcp",
            headers=_headers(token, MODERN),
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        assert "Unsupported protocol version" not in response.text

    def test_save_memory_is_stored(
        self, client: TestClient, temp_registry: StoreRegistry
    ) -> None:
        token = mint(temp_registry, [SCOPE_READ, SCOPE_INGEST])
        response = _call_tool(
            client, token, "save_memory", {"content": "2026-07-28 regression probe"}
        )
        assert response.status_code == 200, response.text
        result = _body(response)["result"]
        assert not result.get("isError"), result
        assert result["content"][0]["text"].startswith("Memory saved: ")

    def test_scope_is_still_enforced(
        self, client: TestClient, temp_registry: StoreRegistry
    ) -> None:
        """A read-only key cannot write at the new revision either."""
        token = mint(temp_registry, [SCOPE_READ])
        response = _call_tool(client, token, "save_memory", {"content": "denied"})
        body = _body(response)
        assert "error" in body or body["result"].get("isError"), body

    def test_a_request_without_a_key_is_401(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Mcp-Protocol-Version": MODERN,
                "Mcp-Method": "server/discover",
            },
            json=_modern_request("server/discover"),
        )
        assert response.status_code == 401


class TestTheLegacyRevisionStillWorks:
    def test_initialize_at_2025_11_25_answers_200(
        self, client: TestClient, temp_registry: StoreRegistry
    ) -> None:
        token = mint(temp_registry, [SCOPE_READ])
        response = client.post(
            "/mcp",
            headers=_headers(token, LEGACY),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LEGACY,
                    "capabilities": {},
                    "clientInfo": {"name": "regression", "version": "0"},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert _body(response)["result"]["protocolVersion"] == LEGACY
