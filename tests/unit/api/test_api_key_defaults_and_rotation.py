"""REST: key defaults (set, rotate) and the defaults seam on retrieval routes.

``PUT /api-keys/{id}/defaults`` and ``POST /api-keys/{id}/rotate`` are admin
routes like the rest of the family. The retrieval seam fills an omitted
``domain`` / ``agent_id`` from the calling key's defaults and never replaces
one the request set; a key that sets no defaults is served the pack pinned in
``golden/pack_no_defaults.json``, captured before this feature existed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
import trellis_api.routes.api_keys as api_keys_routes
from tests.unit.api import _pack_scenario
from trellis.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    TOKEN_PREFIX,
    build_key_defaults,
    generate_api_key,
    verify_token,
)
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

BASE = "/api/v1/api-keys"
GOLDEN = Path(__file__).parent / "golden" / "pack_no_defaults.json"


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture(autouse=True)
def _auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")


@pytest.fixture
def client(registry: StoreRegistry) -> TestClient:
    return TestClient(create_app())


def _seed(
    registry: StoreRegistry,
    scopes: list[str],
    name: str = "caller",
    **defaults: str,
) -> tuple[str, str]:
    token, record = generate_api_key(
        name, scopes, build_key_defaults(**defaults) if defaults else None
    )
    registry.operational.api_key_store.create(record)
    return token, record.key_id


def _h(token: str) -> dict[str, str]:
    return {"X-API-Key": token}


@pytest.fixture
def admin(registry: StoreRegistry) -> tuple[str, str]:
    return _seed(registry, [SCOPE_ADMIN], "root")


def _events(registry: StoreRegistry, kind: EventType) -> list[Any]:
    return registry.operational.event_log.get_events(event_type=kind, limit=100)


class TestSetDefaultsRoute:
    def test_a_non_admin_key_is_refused_and_nothing_changes(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token, key_id = _seed(registry, [SCOPE_READ])
        resp = client.put(
            f"{BASE}/{key_id}/defaults",
            json={"defaults": {"domain": "x"}},
            headers=_h(token),
        )
        assert resp.status_code == 403
        assert registry.operational.api_key_store.get(key_id).defaults is None  # type: ignore[union-attr]
        assert client.put(f"{BASE}/{key_id}/defaults", json={}).status_code == 401

    def test_it_replaces_the_block_and_audits_it(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        _, key_id = _seed(registry, [SCOPE_READ], team="core")
        resp = client.put(
            f"{BASE}/{key_id}/defaults",
            json={"defaults": {"domain": "platform", "session_id_prefix": "ci"}},
            headers=_h(admin[0]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["defaults"] == {
            "domain": "platform",
            "session_id_prefix": "ci",
        }
        stored = registry.operational.api_key_store.get(key_id)
        assert stored is not None
        assert stored.defaults.model_dump(exclude_none=True) == {  # type: ignore[union-attr]
            "domain": "platform",
            "session_id_prefix": "ci",
        }
        (event,) = _events(registry, EventType.API_KEY_UPDATED)
        assert event.payload["updated_by"] == f"api:{admin[1]}"
        assert event.payload["actor_key_id"] == admin[1]
        listed = client.get(BASE, headers=_h(admin[0])).json()["keys"]
        assert {r["key_id"]: r for r in listed}[key_id]["defaults"] == {
            "domain": "platform",
            "session_id_prefix": "ci",
        }

    @pytest.mark.parametrize("body", [{}, {"defaults": None}, {"defaults": {}}])
    def test_null_or_empty_clears_the_block(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        body: dict[str, Any],
    ) -> None:
        _, key_id = _seed(registry, [SCOPE_READ], domain="platform")
        resp = client.put(f"{BASE}/{key_id}/defaults", json=body, headers=_h(admin[0]))
        assert resp.status_code == 200, resp.text
        assert resp.json()["defaults"] is None
        assert registry.operational.api_key_store.get(key_id).defaults is None  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "defaults",
        [{"domain": "bad\nvalue"}, {"team": "x" * 500}, {"unknown_field": "v"}],
    )
    def test_a_bad_value_is_a_client_error_and_writes_nothing(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        defaults: dict[str, str],
    ) -> None:
        _, key_id = _seed(registry, [SCOPE_READ], domain="keep")
        resp = client.put(
            f"{BASE}/{key_id}/defaults",
            json={"defaults": defaults},
            headers=_h(admin[0]),
        )
        assert resp.status_code in {400, 422}
        assert _events(registry, EventType.API_KEY_UPDATED) == []
        assert registry.operational.api_key_store.get(key_id).defaults.domain == "keep"  # type: ignore[union-attr]

    def test_unknown_is_404_and_revoked_is_409(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        body = {"defaults": {"domain": "d"}}
        assert (
            client.put(
                f"{BASE}/ffffffffffff/defaults", json=body, headers=_h(admin[0])
            ).status_code
            == 404
        )
        _, key_id = _seed(registry, [SCOPE_READ])
        assert (
            client.delete(f"{BASE}/{key_id}", headers=_h(admin[0])).status_code == 200
        )
        assert (
            client.put(
                f"{BASE}/{key_id}/defaults", json=body, headers=_h(admin[0])
            ).status_code
            == 409
        )


class TestRotateRoute:
    def test_a_non_admin_key_is_refused_and_mints_nothing(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        token, key_id = _seed(registry, [SCOPE_READ])
        assert (
            client.post(f"{BASE}/{key_id}/rotate", headers=_h(token)).status_code == 403
        )
        assert client.post(f"{BASE}/{key_id}/rotate").status_code == 401
        assert len(registry.operational.api_key_store.list()) == 1

    def test_it_returns_the_new_token_once_and_revokes_the_old_key(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        old_token, old_id = _seed(
            registry, [SCOPE_READ, "restricted"], "svc", domain="platform"
        )
        resp = client.post(f"{BASE}/{old_id}/rotate", headers=_h(admin[0]))
        assert resp.status_code == 201, resp.text
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        assert body["status"] == "rotated"
        assert body["retired"] == "revoked"
        assert body["old_key_id"] == old_id
        assert body["scopes"] == ["read", "restricted"]
        assert body["defaults"] == {"domain": "platform"}
        assert body["token"].startswith(TOKEN_PREFIX)
        store = registry.operational.api_key_store
        assert verify_token(body["token"], store) is not None
        assert verify_token(old_token, store) is None
        # The token is in no later response and in no event.
        assert body["token"] not in client.get(BASE, headers=_h(admin[0])).text
        log = json.dumps(
            [
                e.model_dump(mode="json")
                for e in registry.operational.event_log.get_events(limit=1000)
            ]
        )
        assert body["token"].split(".", 1)[1] not in log
        minted = [
            e
            for e in _events(registry, EventType.API_KEY_MINTED)
            if e.payload["key_id"] == body["key_id"]
        ]
        assert minted[0].payload["rotated_from"] == old_id

    def test_grace_keeps_the_old_key_until_the_window_closes(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        old_token, old_id = _seed(registry, [SCOPE_READ])
        resp = client.post(
            f"{BASE}/{old_id}/rotate", json={"grace": "2h"}, headers=_h(admin[0])
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["retired"] == "grace"
        assert body["old_expires_at"] is not None
        assert verify_token(old_token, registry.operational.api_key_store) is not None

    @pytest.mark.parametrize("grace", ["0h", "91d", "soon"])
    def test_a_bad_grace_is_400_before_anything_is_minted(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        grace: str,
    ) -> None:
        _, old_id = _seed(registry, [SCOPE_READ])
        before = len(registry.operational.api_key_store.list())
        resp = client.post(
            f"{BASE}/{old_id}/rotate", json={"grace": grace}, headers=_h(admin[0])
        )
        assert resp.status_code == 400
        assert len(registry.operational.api_key_store.list()) == before

    def test_unknown_is_404_and_revoked_is_409(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        assert (
            client.post(f"{BASE}/ffffffffffff/rotate", headers=_h(admin[0])).status_code
            == 404
        )
        _, key_id = _seed(registry, [SCOPE_READ])
        client.delete(f"{BASE}/{key_id}", headers=_h(admin[0]))
        before = len(registry.operational.api_key_store.list())
        assert (
            client.post(f"{BASE}/{key_id}/rotate", headers=_h(admin[0])).status_code
            == 409
        )
        assert len(registry.operational.api_key_store.list()) == before

    def test_the_calling_key_cannot_rotate_itself_without_grace(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        resp = client.post(f"{BASE}/{admin[1]}/rotate", headers=_h(admin[0]))
        assert resp.status_code == 409
        assert len(registry.operational.api_key_store.list()) == 1
        graced = client.post(
            f"{BASE}/{admin[1]}/rotate", json={"grace": "5m"}, headers=_h(admin[0])
        )
        assert graced.status_code == 201, graced.text
        assert len(registry.operational.api_key_store.list()) == 2

    def test_a_failed_retire_still_hands_over_the_new_token(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old_token, old_id = _seed(registry, [SCOPE_READ])
        real = api_keys_routes.build_api_keys_executor

        def build(*args: Any, **kwargs: Any) -> Any:
            executor = real(*args, **kwargs)
            inner = executor.execute

            def execute(command: Command) -> CommandResult:
                if command.operation == Operation.API_KEY_REVOKE:
                    return CommandResult(
                        command_id=command.command_id,
                        status=CommandStatus.FAILED,
                        operation=command.operation,
                        message="store unavailable",
                    )
                return inner(command)

            executor.execute = execute  # type: ignore[method-assign]
            return executor

        monkeypatch.setattr(api_keys_routes, "build_api_keys_executor", build)
        resp = client.post(f"{BASE}/{old_id}/rotate", headers=_h(admin[0]))
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "partial"
        assert body["retired"] == "still_valid"
        store = registry.operational.api_key_store
        assert verify_token(old_token, store) is not None
        assert verify_token(body["token"], store) is not None


class TestDefaultsSeam:
    """The retrieval routes fill what a request omitted, never what it set."""

    @staticmethod
    def _corpus(registry: StoreRegistry) -> None:
        for doc_id, (content, metadata) in _pack_scenario.DOCS.items():
            registry.knowledge.document_store.put(doc_id, content, dict(metadata))

    def _pack(self, client: TestClient, token: str, **body: Any) -> dict[str, Any]:
        resp = client.post(
            "/api/v1/packs",
            json={"intent": _pack_scenario.INTENT, "max_items": 10, **body},
            headers=_h(token),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_an_omitted_domain_and_agent_id_come_from_the_key(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        self._corpus(registry)
        token, _ = _seed(registry, [SCOPE_READ], domain="platform", agent_id="agent-7")
        pack = self._pack(client, token)
        assert pack["domain"] == "platform"
        assert pack["agent_id"] == "agent-7"

    def test_a_default_serves_what_the_same_explicit_request_serves(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        self._corpus(registry)
        token, _ = _seed(registry, [SCOPE_READ], domain="platform", agent_id="agent-7")
        plain, _ = _seed(registry, [SCOPE_READ], "plain")
        filled = self._pack(client, token)
        explicit = self._pack(client, plain, domain="platform", agent_id="agent-7")
        assert filled["items"]
        assert _pack_scenario._scrub(filled) == _pack_scenario._scrub(explicit)

    def test_a_request_value_is_never_replaced_by_the_default(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        self._corpus(registry)
        token, _ = _seed(registry, [SCOPE_READ], domain="platform", agent_id="agent-7")
        pack = self._pack(client, token, domain="data", agent_id="someone-else")
        assert pack["domain"] == "data"
        assert pack["agent_id"] == "someone-else"

    def test_search_fills_an_omitted_domain_and_keeps_an_explicit_one(
        self, client: TestClient, registry: StoreRegistry
    ) -> None:
        self._corpus(registry)
        token, _ = _seed(registry, [SCOPE_READ], domain="platform")
        plain, _ = _seed(registry, [SCOPE_READ], "plain")

        def ids(resp: Any) -> set[str]:
            assert resp.status_code == 200, resp.text
            return {r["doc_id"] for r in resp.json()["results"]}

        everything = ids(
            client.get("/api/v1/search", params={"q": "service"}, headers=_h(plain))
        )
        assert {"doc-alpha-runbook", "doc-beta-notes"} <= everything
        filled = ids(
            client.get("/api/v1/search", params={"q": "service"}, headers=_h(token))
        )
        assert filled == {"doc-alpha-runbook", "doc-alpha-rollback"} & everything
        assert filled
        explicit = ids(
            client.get(
                "/api/v1/search",
                params={"q": "service", "domain": "data"},
                headers=_h(token),
            )
        )
        assert explicit == {"doc-beta-notes"}

    def test_a_key_without_defaults_is_served_the_pre_change_pack(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        golden = json.loads(GOLDEN.read_text())
        assert _pack_scenario.run(tmp_path, monkeypatch) == golden
        assert golden["items"], "the golden pack must serve something"
        assert golden["domain"] is None
        assert golden["agent_id"] is None
