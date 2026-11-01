"""REST API-key management: ``GET``/``POST /api-keys`` and ``DELETE /api-keys/{id}``.

Every route is admin-only, so a read key and an ``external`` key are refused on
all three. Mint and revoke are governed operations that emit an event, and no
event, response or later listing may hold a secret or its hash.

Like ``test_settings_routes.py``, the ``TestClient`` is never entered as a
context manager, so the lifespan never replaces the fixture's registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    TOKEN_PREFIX,
    generate_api_key,
    parse_scope_list,
)
from trellis.core.error_sanitize import sanitize_error_message
from trellis.mutate.commands import Operation
from trellis.mutate.handlers import create_curate_handlers
from trellis.mutate.policy_source import POLICY_FILENAME
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app

BASE = "/api/v1/api-keys"


@pytest.fixture
def registry(tmp_path: Path):
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


def _seed(registry: StoreRegistry, scopes: list[str], name: str) -> tuple[str, str]:
    """Persist a key directly; return ``(token, key_id)``."""
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token, record.key_id


def _h(token: str) -> dict[str, str]:
    return {"X-API-Key": token}


@pytest.fixture
def admin(registry: StoreRegistry) -> tuple[str, str]:
    return _seed(registry, [SCOPE_ADMIN], "root")


def _events(registry: StoreRegistry, event_type: EventType) -> list[Any]:
    return registry.operational.event_log.get_events(event_type=event_type, limit=100)


def _whole_log(registry: StoreRegistry) -> str:
    events = registry.operational.event_log.get_events(limit=1000)
    return json.dumps([e.model_dump(mode="json") for e in events])


class TestEveryRouteIsAdminOnly:
    ROUTES = (
        ("GET", BASE, None),
        ("POST", BASE, {"name": "n", "scopes": "read"}),
        ("DELETE", f"{BASE}/abcdef012345", None),
    )

    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    @pytest.mark.parametrize(
        "scopes", [[SCOPE_READ], ["read", "external"]], ids=["read", "external"]
    )
    def test_a_non_admin_key_is_refused(
        self,
        client: TestClient,
        registry: StoreRegistry,
        method: str,
        path: str,
        body: dict[str, str] | None,
        scopes: list[str],
    ) -> None:
        token, _ = _seed(registry, scopes, "caller")
        before = len(registry.operational.api_key_store.list())
        resp = client.request(method, path, json=body, headers=_h(token))
        assert resp.status_code == 403, resp.text
        assert len(registry.operational.api_key_store.list()) == before
        assert (
            registry.operational.event_log.get_events(
                event_type=EventType.API_KEY_MINTED
            )
            == []
        )

    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_no_credential_is_401(
        self, client: TestClient, method: str, path: str, body: dict[str, str] | None
    ) -> None:
        assert client.request(method, path, json=body).status_code == 401


class TestList:
    def test_never_returns_a_hash_or_any_part_of_a_secret(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        token, record = generate_api_key("other", [SCOPE_READ])
        registry.operational.api_key_store.create(record)
        resp = client.get(BASE, headers=_h(admin[0]))
        assert resp.status_code == 200
        text = resp.text
        assert "secret_hash" not in text
        assert record.secret_hash not in text
        assert token.split(".", 1)[1] not in text
        rows = {r["key_id"]: r for r in resp.json()["keys"]}
        assert set(rows[record.key_id]) == {
            "key_id",
            "name",
            "scopes",
            "created_at",
            "revoked",
            "revoked_at",
        }
        assert rows[record.key_id]["revoked"] is False

    def test_carries_presets_the_ui_offers(
        self, client: TestClient, admin: tuple[str, str]
    ) -> None:
        presets = client.get(BASE, headers=_h(admin[0])).json()["presets"]
        scope_sets = [p["scopes"] for p in presets]
        assert ["read"] in scope_sets
        assert ["read", "external"] in scope_sets
        assert ["admin"] in scope_sets


class TestMint:
    def test_token_is_shown_once_and_works(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        resp = client.post(
            BASE, json={"name": "ci", "scopes": "admin"}, headers=_h(admin[0])
        )
        assert resp.status_code == 201, resp.text
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        token = body["token"]
        assert token.startswith(TOKEN_PREFIX)
        assert body["scopes"] == ["admin"]
        assert "shown once" in body["warning"]
        assert "secret_hash" not in body

        # It authenticates, and it is in no later response.
        listing = client.get(BASE, headers=_h(token))
        assert listing.status_code == 200
        secret = token.split(".", 1)[1]
        assert secret not in listing.text
        assert token not in listing.text

    def test_event_names_the_key_and_never_the_secret(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        body = client.post(
            BASE,
            json={"name": "ci", "scopes": ["read", "ingest"]},
            headers=_h(admin[0]),
        ).json()
        events = _events(registry, EventType.API_KEY_MINTED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["key_id"] == body["key_id"]
        assert payload["name"] == "ci"
        assert payload["scopes"] == ["read", "ingest"]
        assert payload["actor_key_id"] == admin[1]
        assert payload["minted_by"] == f"api:{admin[1]}"
        record = registry.operational.api_key_store.get(body["key_id"])
        assert record is not None
        log = _whole_log(registry)
        assert body["token"] not in log
        assert body["token"].split(".", 1)[1] not in log
        assert record.secret_hash not in log
        assert "secret_hash" not in log

    @pytest.mark.parametrize("scopes", ["read,admin,external", "external", "bogus"])
    def test_invalid_scopes_are_refused_as_the_cli_refuses_them(
        self,
        client: TestClient,
        registry: StoreRegistry,
        admin: tuple[str, str],
        scopes: str,
    ) -> None:
        try:
            generate_api_key("x", parse_scope_list(scopes))
        except ValueError as exc:
            expected = sanitize_error_message(str(exc))
        else:  # pragma: no cover
            pytest.fail(f"{scopes!r} should be refused")
        before = len(registry.operational.api_key_store.list())
        resp = client.post(
            BASE, json={"name": "x", "scopes": scopes}, headers=_h(admin[0])
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == expected
        assert len(registry.operational.api_key_store.list()) == before
        assert _events(registry, EventType.API_KEY_MINTED) == []

    def test_every_preset_can_be_minted(
        self, client: TestClient, admin: tuple[str, str]
    ) -> None:
        presets = client.get(BASE, headers=_h(admin[0])).json()["presets"]
        for preset in presets:
            resp = client.post(
                BASE,
                json={"name": preset["label"], "scopes": preset["scopes"]},
                headers=_h(admin[0]),
            )
            assert resp.status_code == 201, (preset, resp.text)
            assert resp.json()["scopes"] == preset["scopes"]

    def test_external_is_valid_only_as_read_external(
        self, client: TestClient, admin: tuple[str, str]
    ) -> None:
        resp = client.post(
            BASE, json={"name": "pub", "scopes": "read,external"}, headers=_h(admin[0])
        )
        assert resp.status_code == 201
        assert resp.json()["scopes"] == ["read", "external"]

    def test_an_empty_name_is_refused(
        self, client: TestClient, admin: tuple[str, str]
    ) -> None:
        resp = client.post(
            BASE, json={"name": "", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 400

    def test_a_denying_policy_blocks_the_mint(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        policy = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="entity_type", value="api_key"),
            rules=[PolicyRule(operation="api_key.mint", action="deny")],
            enforcement=Enforcement.ENFORCE,
        )
        registry.stores_dir.mkdir(parents=True, exist_ok=True)
        (registry.stores_dir / POLICY_FILENAME).write_text(
            json.dumps({"policies": [policy.model_dump(mode="json")]}),
            encoding="utf-8",
        )
        before = len(registry.operational.api_key_store.list())
        resp = client.post(
            BASE, json={"name": "x", "scopes": "read"}, headers=_h(admin[0])
        )
        assert resp.status_code == 400, resp.text
        assert len(registry.operational.api_key_store.list()) == before
        assert _events(registry, EventType.API_KEY_MINTED) == []

    def test_the_generic_mutation_pipeline_cannot_mint_or_revoke(
        self, registry: StoreRegistry
    ) -> None:
        handlers = create_curate_handlers(registry)
        assert Operation.API_KEY_MINT not in handlers
        assert Operation.API_KEY_REVOKE not in handlers


class TestRevoke:
    def test_revokes_and_emits_an_event(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        victim_token, victim_id = _seed(registry, [SCOPE_ADMIN], "victim")
        resp = client.delete(f"{BASE}/{victim_id}", headers=_h(admin[0]))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "revoked", "key_id": victim_id}
        assert client.get(BASE, headers=_h(victim_token)).status_code == 401
        rows = {
            r["key_id"]: r
            for r in client.get(BASE, headers=_h(admin[0])).json()["keys"]
        }
        assert rows[victim_id]["revoked"] is True
        events = _events(registry, EventType.API_KEY_REVOKED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["key_id"] == victim_id
        assert payload["name"] == "victim"
        assert payload["scopes"] == ["admin"]
        assert payload["actor_key_id"] == admin[1]
        record = registry.operational.api_key_store.get(victim_id)
        assert record is not None
        log = _whole_log(registry)
        assert record.secret_hash not in log
        assert victim_token.split(".", 1)[1] not in log

    def test_unknown_key_is_an_operator_error(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        resp = client.delete(f"{BASE}/ffffffffffff", headers=_h(admin[0]))
        assert resp.status_code == 404
        assert _events(registry, EventType.API_KEY_REVOKED) == []

    def test_already_revoked_key_is_an_operator_error(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        _, victim_id = _seed(registry, [SCOPE_READ], "victim")
        assert (
            client.delete(f"{BASE}/{victim_id}", headers=_h(admin[0])).status_code
            == 200
        )
        again = client.delete(f"{BASE}/{victim_id}", headers=_h(admin[0]))
        assert again.status_code == 409
        assert len(_events(registry, EventType.API_KEY_REVOKED)) == 1

    def test_a_caller_cannot_revoke_the_key_it_is_using(
        self, client: TestClient, registry: StoreRegistry, admin: tuple[str, str]
    ) -> None:
        resp = client.delete(f"{BASE}/{admin[1]}", headers=_h(admin[0]))
        assert resp.status_code == 409
        assert "authenticated with" in resp.json()["detail"]
        record = registry.operational.api_key_store.get(admin[1])
        assert record is not None
        assert record.revoked_at is None
        assert _events(registry, EventType.API_KEY_REVOKED) == []
        assert client.get(BASE, headers=_h(admin[0])).status_code == 200
