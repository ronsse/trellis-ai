"""Per-key defaults on the MCP surface: the fill rule, the seam and its roster.

A default fills an argument a call omitted and never replaces one it set; a key
that sets none is served exactly as before. The completeness test fails when the
table of defaultable arguments and the registered tools disagree, so a new tool
taking ``domain`` or ``session_id`` is a decision, not an accident.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.context import reset_transport, set_transport
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

import trellis.mcp.key_defaults as key_defaults_mod
from tests.unit.mcp.conftest import mint
from trellis.auth import SCOPE_READ, build_key_defaults
from trellis.mcp.auth import (
    DEFAULTS_CLAIM,
    TrellisApiKeyVerifier,
    current_key_defaults,
    set_auth_enforced,
)
from trellis.mcp.key_defaults import (
    DEFAULTABLE_ARGUMENTS,
    TOOL_DEFAULT_FIELDS,
    KeyDefaultsMiddleware,
    fill_arguments,
)
from trellis.mcp.server import mcp
from trellis.mutate.api_key_commands import build_mint_command

if TYPE_CHECKING:
    from collections.abc import Iterator

    from trellis.stores.registry import StoreRegistry

DEFAULTS = {
    "agent_id": "agent-7",
    "domain": "platform",
    "team": "core",
    "session_id_prefix": "ci",
}


class TestFillArguments:
    def test_an_omitted_domain_and_session_id_are_filled(self) -> None:
        filled = fill_arguments("get_context", {"intent": "x"}, DEFAULTS)
        assert filled is not None
        assert filled["domain"] == "platform"
        assert filled["session_id"].startswith("ci-")
        assert filled["intent"] == "x"

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_a_blank_argument_counts_as_omitted(self, blank: str | None) -> None:
        filled = fill_arguments("get_context", {"domain": blank}, DEFAULTS)
        assert filled is not None
        assert filled["domain"] == "platform"

    def test_a_value_the_call_set_is_never_replaced(self) -> None:
        arguments = {"intent": "x", "domain": "data", "session_id": "mine"}
        assert fill_arguments("get_context", arguments, DEFAULTS) is None
        assert arguments == {"intent": "x", "domain": "data", "session_id": "mine"}

    def test_only_the_unset_half_is_filled(self) -> None:
        filled = fill_arguments("get_context", {"domain": "data"}, DEFAULTS)
        assert filled is not None
        assert filled["domain"] == "data"
        assert filled["session_id"].startswith("ci-")

    def test_a_non_string_value_is_left_for_validation_to_report(self) -> None:
        assert fill_arguments("get_context", {"domain": 5}, {"domain": "d"}) is None

    def test_no_defaults_or_no_matching_default_changes_nothing(self) -> None:
        assert fill_arguments("get_context", {"intent": "x"}, {}) is None
        # No prefix, so no session id is invented; no domain, so none filled.
        assert fill_arguments("get_context", {}, {"team": "core"}) is None

    def test_session_ids_are_fresh_per_call(self) -> None:
        first = fill_arguments("get_context", {}, {"session_id_prefix": "ci"})
        second = fill_arguments("get_context", {}, {"session_id_prefix": "ci"})
        assert first is not None
        assert second is not None
        assert first["session_id"] != second["session_id"]

    def test_get_lessons_takes_a_domain_but_no_session_id(self) -> None:
        filled = fill_arguments("get_lessons", {}, DEFAULTS)
        assert filled == {"domain": "platform"}

    @pytest.mark.parametrize("tool", ["search", "get_items", "save_memory", "nope"])
    def test_a_tool_outside_the_table_is_never_touched(self, tool: str) -> None:
        assert fill_arguments(tool, {"query": "q"}, DEFAULTS) is None

    def test_agent_id_and_team_feed_no_mcp_tool_today(self) -> None:
        fed = {a for fields in TOOL_DEFAULT_FIELDS.values() for a in fields}
        assert fed == {"domain", "session_id"}


class TestRoster:
    async def test_the_table_is_exactly_the_registered_tools_that_take_those_arguments(
        self,
    ) -> None:
        set_auth_enforced(enforced=False)
        try:
            tools = await mcp.list_tools()
        finally:
            set_auth_enforced(enforced=True)
        takes = {
            t.name: {
                a
                for a in (t.parameters or {}).get("properties", {})
                if a in DEFAULTABLE_ARGUMENTS
            }
            for t in tools
        }
        takes = {name: fields for name, fields in takes.items() if fields}
        assert takes == {k: set(v) for k, v in TOOL_DEFAULT_FIELDS.items()}

    def test_the_middleware_is_registered_after_the_access_log(self) -> None:
        names = [type(m).__name__ for m in mcp.middleware]
        assert "KeyDefaultsMiddleware" in names
        assert names.index("KeyDefaultsMiddleware") > names.index("AccessLogMiddleware")


@contextlib.contextmanager
def _http_token(
    claims: dict[str, Any] | None, transport: str = "streamable-http"
) -> Iterator[None]:
    marker = set_transport(transport)
    auth = auth_context_var.set(
        AuthenticatedUser(
            AccessToken(
                token="opaque",  # noqa: S106 -- placeholder, never read
                client_id="key-id",
                scopes=[SCOPE_READ],
                expires_at=None,
                claims=claims or {},
            )
        )
    )
    try:
        yield
    finally:
        auth_context_var.reset(auth)
        reset_transport(marker)


class TestCurrentKeyDefaults:
    def test_a_token_claim_is_the_callers_defaults(self) -> None:
        with _http_token({DEFAULTS_CLAIM: {"domain": "platform"}}):
            assert current_key_defaults() == {"domain": "platform"}

    def test_a_token_without_the_claim_has_none(self) -> None:
        with _http_token(None):
            assert current_key_defaults() == {}

    def test_stdio_has_none_even_beside_a_claim(self) -> None:
        with _http_token({DEFAULTS_CLAIM: {"domain": "platform"}}, transport="stdio"):
            assert current_key_defaults() == {}

    def test_disabled_enforcement_has_none_even_beside_a_claim(self) -> None:
        set_auth_enforced(enforced=False)
        try:
            with _http_token({DEFAULTS_CLAIM: {"domain": "platform"}}):
                assert current_key_defaults() == {}
        finally:
            set_auth_enforced(enforced=True)

    def test_no_credential_has_none(self) -> None:
        marker = set_transport("streamable-http")
        try:
            assert current_key_defaults() == {}
        finally:
            reset_transport(marker)


class TestVerifierCarriesTheBlock:
    async def test_a_key_with_defaults_puts_them_on_the_token(
        self, temp_registry: StoreRegistry
    ) -> None:
        token, record, _command = build_mint_command(
            "svc",
            [SCOPE_READ],
            requested_by="test:seed",
            defaults=build_key_defaults(domain="platform", team="core"),
        )
        temp_registry.operational.api_key_store.create(record)
        verifier = TrellisApiKeyVerifier(
            lambda: temp_registry.operational.api_key_store
        )
        access = await verifier.verify_token(token)
        assert access is not None
        assert access.claims[DEFAULTS_CLAIM] == {"domain": "platform", "team": "core"}

    async def test_a_key_without_defaults_adds_no_claim(
        self, temp_registry: StoreRegistry
    ) -> None:
        token = mint(temp_registry, [SCOPE_READ])
        verifier = TrellisApiKeyVerifier(
            lambda: temp_registry.operational.api_key_store
        )
        access = await verifier.verify_token(token)
        assert access is not None
        assert DEFAULTS_CLAIM not in access.claims

    async def test_an_expired_key_is_refused(
        self, temp_registry: StoreRegistry
    ) -> None:
        from datetime import timedelta

        from trellis.core.base import utc_now

        token = mint(temp_registry, [SCOPE_READ])
        key_id = token.removeprefix("trellis_ak_").split(".")[0]
        store = temp_registry.operational.api_key_store
        store.update(key_id, expires_at=utc_now() - timedelta(seconds=1))
        assert await TrellisApiKeyVerifier(lambda: store).verify_token(token) is None


def _probe_server() -> FastMCP:
    server = FastMCP("defaults-probe")
    server.add_middleware(KeyDefaultsMiddleware())

    @server.tool
    def get_context(
        intent: str, domain: str | None = None, session_id: str | None = None
    ) -> str:
        return json.dumps(
            {"intent": intent, "domain": domain, "session_id": session_id}
        )

    @server.tool
    def search(query: str, domain: str | None = None) -> str:
        return json.dumps({"query": query, "domain": domain})

    return server


async def _call(
    monkeypatch: pytest.MonkeyPatch,
    defaults: dict[str, str],
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    monkeypatch.setattr(key_defaults_mod, "current_key_defaults", lambda: defaults)
    async with Client(_probe_server()) as client:
        result = await client.call_tool(tool, arguments)
    return json.loads(result.content[0].text)


class TestMiddlewareEndToEnd:
    async def test_an_omitted_argument_reaches_the_tool_filled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = await _call(monkeypatch, DEFAULTS, "get_context", {"intent": "x"})
        assert seen["domain"] == "platform"
        assert seen["session_id"].startswith("ci-")

    async def test_an_argument_the_call_set_reaches_the_tool_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = await _call(
            monkeypatch,
            DEFAULTS,
            "get_context",
            {"intent": "x", "domain": "data", "session_id": "mine"},
        )
        assert seen == {"intent": "x", "domain": "data", "session_id": "mine"}

    async def test_a_key_without_defaults_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = await _call(monkeypatch, {}, "get_context", {"intent": "x"})
        assert seen == {"intent": "x", "domain": None, "session_id": None}

    async def test_a_tool_outside_the_table_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = await _call(monkeypatch, DEFAULTS, "search", {"query": "q"})
        assert seen == {"query": "q", "domain": None}
