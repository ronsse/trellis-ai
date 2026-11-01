"""An ``external`` key over MCP: public documents answer, everything else is refused.

Run the way ``test_auth.py`` and ``test_sensitivity_surfaces.py`` do: a real key
minted into the store and resolved by ``TrellisApiKeyVerifier`` under enforced
HTTP auth, then real tool calls made with the scopes that verifier returned. A
``read`` key is the control on every call, and anonymous enforced MCP is pinned
so this scope neither gains nor loses it anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastmcp.server.auth import AccessToken, AuthContext
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import mint, unwrap_tool
from tests.unit.mcp.test_auth import EXPECTED_TOOL_SCOPES
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.test_sensitivity import label
from trellis.auth import SCOPE_INGEST, SCOPE_MUTATE, SCOPE_READ, clearance_of
from trellis.mcp.auth import TrellisApiKeyVerifier, set_auth_enforced, trellis_scope
from trellis.mcp.server import current_clearance, mcp

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(server_mod.get_context)
get_sectioned_context = unwrap_tool(server_mod.get_sectioned_context)
get_objective_context = unwrap_tool(server_mod.get_objective_context)
get_task_context = unwrap_tool(server_mod.get_task_context)
search = unwrap_tool(server_mod.search)
get_items = unwrap_tool(server_mod.get_items)
get_graph = unwrap_tool(server_mod.get_graph)
get_lessons = unwrap_tool(server_mod.get_lessons)
get_file_context = unwrap_tool(server_mod.get_file_context)
query_observations = unwrap_tool(server_mod.query_observations)
save_memory = unwrap_tool(server_mod.save_memory)

INTENT = "failover runbook"
PUBLIC_TEXT = "failover runbook published: drain the write queue. " * 4
PLAIN_TEXT = "failover runbook plain-sentinel: promote the replica. " * 4
SECRET_TEXT = "failover runbook secret-sentinel: rotate the seal keys. " * 4


async def _external_scopes(registry: StoreRegistry) -> list[str]:
    """Mint ``read,external``, resolve it as the HTTP transport would."""
    token = mint(registry, ["read", "external"], name="partner")
    verifier = TrellisApiKeyVerifier(lambda: registry.operational.api_key_store)
    access = await verifier.verify_token(token)
    assert access is not None
    return list(access.scopes)


@pytest.fixture
def seeded(temp_registry: StoreRegistry) -> StoreRegistry:
    docs = temp_registry.knowledge.document_store
    docs.put("doc-public", PUBLIC_TEXT, {"title": "Pub", **label("public")})
    docs.put("doc-plain", PLAIN_TEXT, {"title": "Plain", "source_path": "svc/plain.py"})
    docs.put("doc-secret", SECRET_TEXT, {"title": "Secret", **label("restricted")})
    temp_registry.knowledge.graph_store.upsert_node(
        "node-1",
        "entity",
        {"name": "graph-sentinel", "data_classification": label("public")},
    )
    return temp_registry


class TestTheVerifierResolvesIt:
    async def test_the_minted_key_arrives_as_a_public_clearance(
        self, seeded: StoreRegistry
    ) -> None:
        scopes = await _external_scopes(seeded)
        assert set(scopes) == {"read", "external"}
        assert clearance_of(scopes) == "public"
        with as_http(scopes):
            assert current_clearance() == "public"

    async def test_anonymous_enforced_and_stdio_clearances_are_unchanged(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(None):
            assert current_clearance() == "internal"
        with as_stdio():
            assert current_clearance() == "restricted"
        with as_http(["read"]):
            assert current_clearance() == "internal"


class TestToolVisibility:
    async def test_an_external_key_lists_the_read_tools_and_no_write_tool(
        self, seeded: StoreRegistry
    ) -> None:
        scopes = await _external_scopes(seeded)
        ctx = _auth_context(scopes)
        set_auth_enforced(enforced=False)
        try:
            tools = {t.name: t for t in await mcp.list_tools()}
        finally:
            set_auth_enforced(enforced=True)
        visible = {n for n, t in tools.items() if t.auth(ctx)}  # type: ignore[attr-defined]
        assert visible == {
            n for n, s in EXPECTED_TOOL_SCOPES.items() if s == SCOPE_READ
        }


def _auth_context(scopes: list[str]) -> AuthContext:
    token = AccessToken(
        token="opaque",  # noqa: S106 -- placeholder, never read
        client_id="k",
        scopes=scopes,
        expires_at=None,
    )
    return AuthContext(token=token, component=MagicMock())


class TestAnsweringTools:
    @pytest.mark.parametrize(
        "call",
        [
            lambda: get_context(INTENT),
            lambda: get_sectioned_context(INTENT, sections=[{"name": "All"}]),
            lambda: search(INTENT),
        ],
        ids=["get_context", "get_sectioned_context", "search"],
    )
    async def test_only_the_public_document_is_served(
        self, seeded: StoreRegistry, call: object
    ) -> None:
        scopes = await _external_scopes(seeded)
        with as_http(scopes):
            result = call()  # type: ignore[operator]
        assert "published" in result
        assert "plain-sentinel" not in result
        assert "secret-sentinel" not in result
        assert "doc-plain" not in result
        assert "doc-secret" not in result

    @pytest.mark.parametrize(
        "call",
        [lambda: get_objective_context(INTENT), lambda: get_task_context(INTENT)],
        ids=["get_objective_context", "get_task_context"],
    )
    async def test_the_graph_shaped_packs_name_nothing_unlabelled(
        self, seeded: StoreRegistry, call: object
    ) -> None:
        """Their sections route entity items, which an external key is never shown."""
        scopes = await _external_scopes(seeded)
        with as_http(scopes):
            result = call()  # type: ignore[operator]
        for hidden in ("plain-sentinel", "secret-sentinel", "graph-sentinel", "node-1"):
            assert hidden not in result
        assert "doc-plain" not in result
        assert "doc-secret" not in result

    async def test_a_read_key_is_served_the_unlabelled_document_as_before(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http([SCOPE_READ]):
            result = get_context(INTENT)
        assert "plain-sentinel" in result
        assert "secret-sentinel" not in result

    async def test_get_items_reads_a_public_document_and_nothing_else(
        self, seeded: StoreRegistry
    ) -> None:
        scopes = await _external_scopes(seeded)
        with as_http(scopes):
            result = get_items(["doc-public", "doc-plain", "doc-secret", "node-1"])
        assert "published" in result
        for hidden in ("plain-sentinel", "secret-sentinel", "graph-sentinel"):
            assert hidden not in result

    async def test_get_items_answers_a_graph_node_like_a_missing_id(
        self, seeded: StoreRegistry
    ) -> None:
        scopes = await _external_scopes(seeded)
        with as_http(scopes):
            node = get_items(["node-1"])
            ghost = get_items(["node-ghost"])
        assert node.replace("node-1", "X") == ghost.replace("node-ghost", "X")
        with as_http([SCOPE_READ]):
            assert "graph-sentinel" in get_items(["node-1"])

    async def test_a_pack_carries_no_capture_banner_for_an_external_key(
        self, seeded: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            server_mod, "_capture_warning_banner", lambda _r: "BANNER-SENTINEL\n"
        )
        scopes = await _external_scopes(seeded)
        with as_http(scopes):
            external = get_context(INTENT)
        with as_http([SCOPE_READ]):
            reader = get_context(INTENT)
        assert "BANNER-SENTINEL" not in external
        assert "BANNER-SENTINEL" in reader


class TestRefusedTools:
    @pytest.mark.parametrize(
        ("name", "call"),
        [
            ("get_graph", lambda: get_graph("node-1")),
            ("get_lessons", get_lessons),
            ("get_file_context", lambda: get_file_context(["svc/plain.py"])),
            ("query_observations", query_observations),
        ],
    )
    async def test_an_external_key_is_refused_with_a_stated_reason(
        self, seeded: StoreRegistry, name: str, call: object
    ) -> None:
        scopes = await _external_scopes(seeded)
        with as_http(scopes), pytest.raises(McpError, match="external key") as err:
            call()  # type: ignore[operator]
        assert name in str(err.value)
        assert err.value.error.data == {"tool": name, "reason": "external_key"}

    async def test_a_read_key_is_not_refused_by_the_same_gate(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http([SCOPE_READ]):
            assert "graph-sentinel" in get_graph("node-1")
            get_lessons()
            query_observations()

    async def test_an_external_key_cannot_write(self, seeded: StoreRegistry) -> None:
        scopes = await _external_scopes(seeded)
        ctx = _auth_context(scopes)
        set_auth_enforced(enforced=False)
        try:
            tools = {t.name: t for t in await mcp.list_tools()}
        finally:
            set_auth_enforced(enforced=True)
        for name, needed in EXPECTED_TOOL_SCOPES.items():
            if needed != SCOPE_READ:
                assert not tools[name].auth(ctx), name  # type: ignore[attr-defined]


class TestTheTwoWritesAnExternalKeyMustNotReach:
    """``record_feedback`` and ``save_knowledge`` are refused for read+external.

    ``record_feedback`` names a pack's item ids and ``save_knowledge`` writes an
    entity, so each would hand an external key a route that depends on or
    changes more than it may read. They are guarded by the ``mutate`` and
    ``ingest`` scopes, which a ``["read", "external"]`` key does not hold, and
    the gate that enforces that is the real one with enforcement on. If either
    tool were ever re-scoped to ``read`` this fails, and so does the guarantee.
    """

    def test_the_tools_are_guarded_by_mutate_and_ingest(self) -> None:
        assert EXPECTED_TOOL_SCOPES["record_feedback"] == SCOPE_MUTATE
        assert EXPECTED_TOOL_SCOPES["save_knowledge"] == SCOPE_INGEST

    async def test_the_real_scope_checks_refuse_a_read_external_key(
        self, seeded: StoreRegistry
    ) -> None:
        ctx = _auth_context(await _external_scopes(seeded))
        assert set(ctx.token.scopes) == {"read", "external"}  # type: ignore[union-attr]
        assert trellis_scope("mutate")(ctx) is False
        assert trellis_scope("ingest")(ctx) is False
        assert trellis_scope("read")(ctx) is True

    async def test_neither_tool_is_listed_to_the_key(
        self, seeded: StoreRegistry
    ) -> None:
        ctx = _auth_context(await _external_scopes(seeded))
        set_auth_enforced(enforced=False)
        try:
            tools = {t.name: t for t in await mcp.list_tools()}
        finally:
            set_auth_enforced(enforced=True)
        for name in ("record_feedback", "save_knowledge"):
            assert not tools[name].auth(ctx), name  # type: ignore[attr-defined]


class TestAnonymousIsUnchanged:
    def test_anonymous_enforced_mcp_still_reads_unlabelled_and_not_restricted(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(None):
            result = get_context(INTENT)
        assert "plain-sentinel" in result
        assert "secret-sentinel" not in result

    def test_stdio_reads_everything_and_is_not_refused_a_tool(
        self, seeded: StoreRegistry
    ) -> None:
        with as_stdio():
            assert "secret-sentinel" in get_context(INTENT)
            assert "graph-sentinel" in get_graph("node-1")
