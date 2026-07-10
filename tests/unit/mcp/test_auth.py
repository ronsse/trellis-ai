"""Tests for MCP transport resolution and scoped API-key auth.

Mirrors ``tests/unit/api/test_auth.py``: the same token machinery, the
same scope semantics, a different transport. Exercised at the unit seams
rather than through a live uvicorn — the HTTP wiring itself is FastMCP's,
what's ours is the resolvers, the verifier, and the tool→scope map.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastmcp.server.auth import AccessToken, AuthContext

import trellis.mcp.auth as auth_mod
from trellis.auth import SCOPE_ADMIN, SCOPE_INGEST, SCOPE_MUTATE, SCOPE_READ
from trellis.errors import ConfigError
from trellis.mcp.auth import (
    AUTH_MODE_OFF,
    AUTH_MODE_REQUIRED,
    DEFAULT_PATH,
    DEFAULT_PORT,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    TrellisApiKeyVerifier,
    resolve_http_settings,
    resolve_mcp_auth_mode,
    resolve_transport,
    set_auth_enforced,
    trellis_scope,
)
from trellis.mcp.server import mcp

from .conftest import mint

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry


def _ctx(token: AccessToken | None) -> AuthContext:
    """An AuthContext carrying ``token``; the component is never inspected."""
    return AuthContext(token=token, component=MagicMock())


def _token(*scopes: str) -> AccessToken:
    return AccessToken(
        token="opaque",  # noqa: S106 — a placeholder; scope checks never read it
        client_id="key-id",
        scopes=list(scopes),
        expires_at=None,
    )


# ---------------------------------------------------------------------------
# Transport / mode resolution
# ---------------------------------------------------------------------------


class TestResolveTransport:
    def test_unset_is_stdio(self) -> None:
        assert resolve_transport() == TRANSPORT_STDIO

    def test_empty_is_stdio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", "   ")
        assert resolve_transport() == TRANSPORT_STDIO

    @pytest.mark.parametrize("raw", ["http", "HTTP", " Http "])
    def test_http_is_case_and_space_insensitive(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", raw)
        assert resolve_transport() == TRANSPORT_HTTP

    def test_invalid_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", "grpc")
        with pytest.raises(ConfigError, match="TRELLIS_MCP_TRANSPORT") as exc:
            resolve_transport()
        assert exc.value.setting == "TRELLIS_MCP_TRANSPORT"


class TestResolveMcpAuthMode:
    def test_unset_defaults_to_required(self) -> None:
        """The http transport is opt-in; defaulting it open would invert that."""
        assert resolve_mcp_auth_mode() == AUTH_MODE_REQUIRED

    def test_explicit_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_MCP_AUTH_MODE", "OFF")
        assert resolve_mcp_auth_mode() == AUTH_MODE_OFF

    def test_optional_is_not_a_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REST supports ``optional`` to migrate existing callers; MCP has none."""
        monkeypatch.setenv("TRELLIS_MCP_AUTH_MODE", "optional")
        with pytest.raises(ConfigError, match="TRELLIS_MCP_AUTH_MODE"):
            resolve_mcp_auth_mode()

    def test_is_independent_of_rest_auth_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Turning on REST auth must not silently flip the MCP surface."""
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        monkeypatch.setenv("TRELLIS_API_KEY", "legacy-shared-secret")
        assert resolve_mcp_auth_mode() == AUTH_MODE_REQUIRED


class TestResolveHttpSettings:
    def test_defaults_are_loopback_and_enforced(self) -> None:
        settings = resolve_http_settings()
        assert settings.host == "127.0.0.1"
        assert settings.port == DEFAULT_PORT
        assert settings.path == DEFAULT_PATH
        assert settings.auth_enforced is True

    @pytest.mark.parametrize("raw", ["notaport", "0", "65536", "-1"])
    def test_bad_port_raises(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("TRELLIS_MCP_PORT", raw)
        with pytest.raises(ConfigError, match="TRELLIS_MCP_PORT"):
            resolve_http_settings()

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.12", "skynet"])  # noqa: S104
    def test_off_mode_refuses_non_loopback_bind(
        self, monkeypatch: pytest.MonkeyPatch, host: str
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_AUTH_MODE", "off")
        monkeypatch.setenv("TRELLIS_MCP_HOST", host)
        with pytest.raises(ConfigError, match="Refusing to bind"):
            resolve_http_settings()

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_off_mode_allows_loopback_bind(
        self, monkeypatch: pytest.MonkeyPatch, host: str
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_AUTH_MODE", "off")
        monkeypatch.setenv("TRELLIS_MCP_HOST", host)
        assert resolve_http_settings().auth_enforced is False

    def test_insecure_bind_override_permits_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_AUTH_MODE", "off")
        monkeypatch.setenv("TRELLIS_MCP_HOST", "0.0.0.0")  # noqa: S104
        monkeypatch.setenv("TRELLIS_MCP_ALLOW_INSECURE_BIND", "1")
        assert resolve_http_settings().host == "0.0.0.0"  # noqa: S104

    def test_override_fails_closed_on_unrecognised_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_MCP_AUTH_MODE", "off")
        monkeypatch.setenv("TRELLIS_MCP_HOST", "0.0.0.0")  # noqa: S104
        monkeypatch.setenv("TRELLIS_MCP_ALLOW_INSECURE_BIND", "maybe")
        with pytest.raises(ConfigError, match="Refusing to bind"):
            resolve_http_settings()

    def test_required_mode_may_bind_anywhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The container binds 0.0.0.0 and publishes tailnet-only."""
        monkeypatch.setenv("TRELLIS_MCP_HOST", "0.0.0.0")  # noqa: S104
        assert resolve_http_settings().host == "0.0.0.0"  # noqa: S104


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


class TestTrellisApiKeyVerifier:
    async def test_valid_key_resolves_to_access_token(
        self, temp_registry: StoreRegistry
    ) -> None:
        token = mint(temp_registry, [SCOPE_READ, SCOPE_INGEST], name="omen")
        verifier = TrellisApiKeyVerifier(
            lambda: temp_registry.operational.api_key_store
        )
        access = await verifier.verify_token(token)
        assert access is not None
        assert set(access.scopes) == {SCOPE_READ, SCOPE_INGEST}
        assert access.client_id

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "garbage",
            "Bearer trellis_ak_aaaaaaaaaaaa.secret",  # un-stripped prefix
            "trellis_ak_nothex.secret",
            "trellis_ak_aaaaaaaaaaaa",  # no separator
            "trellis_ak_aaaaaaaaaaaa.",  # empty secret
            "trellis_ak_aaaaaaaaaaaa.unknown-key-id",
        ],
    )
    async def test_malformed_or_unknown_is_none(
        self, temp_registry: StoreRegistry, bad: str
    ) -> None:
        verifier = TrellisApiKeyVerifier(
            lambda: temp_registry.operational.api_key_store
        )
        assert await verifier.verify_token(bad) is None

    async def test_wrong_secret_is_none(self, temp_registry: StoreRegistry) -> None:
        token = mint(temp_registry, [SCOPE_READ])
        key_id = token.removeprefix("trellis_ak_").split(".")[0]
        verifier = TrellisApiKeyVerifier(
            lambda: temp_registry.operational.api_key_store
        )
        assert await verifier.verify_token(f"trellis_ak_{key_id}.wrong") is None

    async def test_revoked_key_is_none(self, temp_registry: StoreRegistry) -> None:
        token = mint(temp_registry, [SCOPE_READ])
        store = temp_registry.operational.api_key_store
        assert await TrellisApiKeyVerifier(lambda: store).verify_token(token)
        store.revoke(token.removeprefix("trellis_ak_").split(".")[0])
        assert await TrellisApiKeyVerifier(lambda: store).verify_token(token) is None

    async def test_store_outage_fails_closed(self) -> None:
        def boom() -> None:
            msg = "connection refused"
            raise RuntimeError(msg)

        verifier = TrellisApiKeyVerifier(boom)  # type: ignore[arg-type]
        assert await verifier.verify_token("trellis_ak_aaaaaaaaaaaa.x") is None

    async def test_store_outage_never_logs_the_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traceback would render this frame's locals — including the token.

        ``logger.exception`` here would print the caller's bearer token, and
        ``str(exc)`` can carry a DSN with its password. Log the type only.
        """
        secret = "trellis_ak_aaaaaaaaaaaa.SUPER-SECRET-VALUE"  # noqa: S105

        def boom() -> None:
            msg = "connect failed: password=hunter2"
            raise RuntimeError(msg)

        fake_logger = MagicMock()
        monkeypatch.setattr(auth_mod, "logger", fake_logger)
        assert await TrellisApiKeyVerifier(boom).verify_token(secret) is None  # type: ignore[arg-type]

        fake_logger.exception.assert_not_called()
        fake_logger.error.assert_called_once()
        rendered = repr(fake_logger.error.call_args)
        assert "SUPER-SECRET-VALUE" not in rendered
        assert "hunter2" not in rendered
        assert fake_logger.error.call_args.kwargs["error_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Scope checks
# ---------------------------------------------------------------------------


class TestTrellisScope:
    def test_unknown_scope_raises_at_decoration_time(self) -> None:
        with pytest.raises(ValueError, match="Unknown scope"):
            trellis_scope("write")

    def test_exact_scope_allows(self) -> None:
        assert trellis_scope(SCOPE_READ)(_ctx(_token(SCOPE_READ))) is True

    def test_missing_scope_denies(self) -> None:
        assert trellis_scope(SCOPE_MUTATE)(_ctx(_token(SCOPE_READ))) is False

    @pytest.mark.parametrize("required", [SCOPE_READ, SCOPE_INGEST, SCOPE_MUTATE])
    def test_admin_implies_every_scope(self, required: str) -> None:
        """Regression guard against swapping in FastMCP's ``require_scopes``.

        That helper does flat subset matching, so an ``admin``-only key
        would be denied on a ``read`` tool. ``scopes_satisfy`` must stay
        the single source of truth for scope semantics.
        """
        assert trellis_scope(required)(_ctx(_token(SCOPE_ADMIN))) is True

    def test_anonymous_denies_when_enforced(self) -> None:
        assert trellis_scope(SCOPE_READ)(_ctx(None)) is False

    def test_anonymous_allowed_when_enforcement_off(self) -> None:
        set_auth_enforced(enforced=False)
        assert trellis_scope(SCOPE_MUTATE)(_ctx(None)) is True

    def test_anonymous_denial_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty tool list is otherwise an unexplainable symptom."""
        fake_logger = MagicMock()
        monkeypatch.setattr(auth_mod, "logger", fake_logger)
        check = trellis_scope(SCOPE_READ)
        for _ in range(3):
            check(_ctx(None))
        fake_logger.warning.assert_called_once()
        assert fake_logger.warning.call_args.args[0] == "mcp_tool_denied_anonymous"


# ---------------------------------------------------------------------------
# The tool → scope map
# ---------------------------------------------------------------------------

#: The contract this change ships. Cross-checked against the REST router
#: wiring in ``trellis_api/app.py`` — ``retrieve``/``explore``/``observations``
#: are ``read``, ``ingest`` is ``ingest``, ``curate``/``mutations`` are
#: ``mutate``.
EXPECTED_TOOL_SCOPES = {
    "get_context": SCOPE_READ,
    "search": SCOPE_READ,
    "get_graph": SCOPE_READ,
    "get_lessons": SCOPE_READ,
    "query_observations": SCOPE_READ,
    "get_objective_context": SCOPE_READ,
    "get_task_context": SCOPE_READ,
    "get_sectioned_context": SCOPE_READ,
    "save_experience": SCOPE_INGEST,
    "save_knowledge": SCOPE_INGEST,
    "save_memory": SCOPE_INGEST,
    "record_observation": SCOPE_MUTATE,
    "record_feedback": SCOPE_MUTATE,
    "execute_mutation": SCOPE_MUTATE,
}


class TestToolScopeMap:
    async def _tools(self) -> dict[str, object]:
        # list_tools() is itself scope-filtered, so an anonymous listing
        # returns nothing. Drop enforcement to enumerate, then restore it —
        # the checks these tests then invoke consult the same flag, and
        # leaving it off would make every assertion below vacuously true.
        set_auth_enforced(enforced=False)
        try:
            return {t.name: t for t in await mcp.list_tools()}
        finally:
            set_auth_enforced(enforced=True)

    async def test_every_tool_carries_a_scope_check(self) -> None:
        tools = await self._tools()
        unguarded = [
            name for name, t in tools.items() if getattr(t, "auth", None) is None
        ]
        assert not unguarded, f"tools reachable without a scope check: {unguarded}"

    async def test_map_matches_the_contract(self) -> None:
        tools = await self._tools()
        assert set(tools) == set(EXPECTED_TOOL_SCOPES), (
            "a tool was added or removed without updating the scope map"
        )
        actual = {
            name: t.auth.__name__.removeprefix("require_scope_")
            for name, t in tools.items()
        }
        assert actual == EXPECTED_TOOL_SCOPES

    async def test_read_key_cannot_see_mutating_tools(self) -> None:
        """FastMCP filters ``tools/list`` on the same checks that gate calls."""
        tools = await self._tools()
        read_only = {
            name
            for name, t in tools.items()
            if t.auth(_ctx(_token(SCOPE_READ)))  # type: ignore[attr-defined]
        }
        assert read_only == {
            n for n, s in EXPECTED_TOOL_SCOPES.items() if s == SCOPE_READ
        }

    async def test_admin_key_sees_every_tool(self) -> None:
        tools = await self._tools()
        visible = {
            name
            for name, t in tools.items()
            if t.auth(_ctx(_token(SCOPE_ADMIN)))  # type: ignore[attr-defined]
        }
        assert visible == set(EXPECTED_TOOL_SCOPES)
