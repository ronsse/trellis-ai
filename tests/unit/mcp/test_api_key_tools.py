"""MCP API-key tools: list and revoke, admin-only, and no tool that mints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import trellis.mcp.server as server_mod
from trellis.auth import SCOPE_ADMIN, SCOPE_READ, generate_api_key
from trellis.mcp.auth import set_auth_enforced
from trellis.mcp.server import mcp
from trellis.stores.base.event_log import EventType

from .conftest import unwrap_tool
from .test_auth import _ctx, _token

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

list_api_keys = unwrap_tool(server_mod.list_api_keys)
revoke_api_key = unwrap_tool(server_mod.revoke_api_key)

API_KEY_TOOLS = ("list_api_keys", "revoke_api_key")


def _seed(registry: StoreRegistry, scopes: list[str], name: str = "k") -> str:
    _, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return record.key_id


async def _tools() -> dict[str, object]:
    set_auth_enforced(enforced=False)
    try:
        return {t.name: t for t in await mcp.list_tools()}
    finally:
        set_auth_enforced(enforced=True)


class TestNoToolMints:
    async def test_no_registered_tool_mints_a_key(self) -> None:
        names = set(await _tools())
        assert {n for n in names if "api_key" in n} == set(API_KEY_TOOLS)
        assert not {n for n in names if "mint" in n or n.startswith("create_api")}

    def test_the_server_never_builds_a_mint_command(self) -> None:
        source = Path(server_mod.__file__).read_text(encoding="utf-8")
        assert "build_mint_command" not in source
        assert "API_KEY_MINT" not in source
        assert "generate_api_key" not in source


class TestAdminOnly:
    @pytest.mark.parametrize("tool", API_KEY_TOOLS)
    async def test_only_an_admin_key_may_call_it(self, tool: str) -> None:
        check = (await _tools())[tool].auth  # type: ignore[attr-defined]
        assert check(_ctx(_token(SCOPE_ADMIN))) is True
        assert check(_ctx(_token(SCOPE_READ))) is False
        assert check(_ctx(_token("read", "external"))) is False
        assert check(_ctx(None)) is False


class TestListApiKeys:
    def test_lists_keys_without_a_secret_or_hash(
        self, temp_registry: StoreRegistry
    ) -> None:
        token, record = generate_api_key("worker", [SCOPE_READ])
        temp_registry.operational.api_key_store.create(record)
        raw = list_api_keys()
        payload = json.loads(raw)
        assert payload["status"] == "ok"
        assert payload["count"] == 1
        assert payload["keys"][0]["key_id"] == record.key_id
        assert "secret_hash" not in raw
        assert record.secret_hash not in raw
        assert token.split(".", 1)[1] not in raw


class TestRevokeApiKey:
    def test_revokes_and_emits_an_event(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(server_mod, "current_principal_id", lambda: "aaaaaaaaaaaa")
        key_id = _seed(temp_registry, [SCOPE_READ], "victim")
        payload = json.loads(revoke_api_key(key_id=key_id))
        assert payload == {"status": "ok", "key_id": key_id}
        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.API_KEY_REVOKED
        )
        assert [e.payload["key_id"] for e in events] == [key_id]
        assert events[0].payload["revoked_by"] == "mcp:revoke_api_key"

    def test_unknown_and_already_revoked_are_refused(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(server_mod, "current_principal_id", lambda: "aaaaaaaaaaaa")
        unknown = json.loads(revoke_api_key(key_id="ffffffffffff"))
        assert unknown["status"] == "rejected"
        assert unknown["reason"] == "unknown_key_id"
        key_id = _seed(temp_registry, [SCOPE_READ])
        assert json.loads(revoke_api_key(key_id=key_id))["status"] == "ok"
        again = json.loads(revoke_api_key(key_id=key_id))
        assert again["reason"] == "already_revoked"

    def test_a_caller_cannot_revoke_the_key_it_is_using(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key_id = _seed(temp_registry, [SCOPE_ADMIN], "me")
        monkeypatch.setattr(server_mod, "current_principal_id", lambda: key_id)
        payload = json.loads(revoke_api_key(key_id=key_id))
        assert payload["status"] == "rejected"
        assert payload["reason"] == "cannot_revoke_own_key"
        record = temp_registry.operational.api_key_store.get(key_id)
        assert record is not None
        assert record.revoked_at is None

    def test_an_unidentified_caller_is_refused_and_pointed_at_the_cli(
        self, temp_registry: StoreRegistry
    ) -> None:
        key_id = _seed(temp_registry, [SCOPE_ADMIN], "last-admin")
        payload = json.loads(revoke_api_key(key_id=key_id))
        assert payload["status"] == "rejected"
        assert payload["reason"] == "unidentified_caller"
        assert "trellis admin api-keys revoke" in payload["message"]
        record = temp_registry.operational.api_key_store.get(key_id)
        assert record is not None
        assert record.revoked_at is None

    def test_a_dead_event_log_still_revokes_and_warns(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sqlite3

        monkeypatch.setattr(server_mod, "current_principal_id", lambda: "aaaaaaaaaaaa")
        key_id = _seed(temp_registry, [SCOPE_READ], "victim")

        def boom(*_a: object, **_k: object) -> None:
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(temp_registry.operational.event_log, "emit", boom)
        payload = json.loads(revoke_api_key(key_id=key_id))
        assert payload["status"] == "ok"
        assert any("audit_event_not_recorded" in w for w in payload["warnings"])
        record = temp_registry.operational.api_key_store.get(key_id)
        assert record is not None
        assert record.revoked_at is not None
