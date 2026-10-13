"""Tests for scoped API-key authentication on /api/v1 routes.

Covers the TRELLIS_AUTH_MODE matrix (off / optional / required +
backwards-compat inference), both credential headers (X-API-Key wins
over Bearer), per-router scope enforcement (403 for a valid key
lacking the scope), the legacy shared-secret grant, and the
always-open probes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import (
    SCOPE_ADMIN,
    SCOPE_MUTATE,
    SCOPE_READ,
    generate_api_key,
)
from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_api.auth import resolve_auth_mode, warn_if_unauthenticated


@pytest.fixture
def registry(tmp_path):
    """Fresh registry bound to the app module for each test."""
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    """In-memory app over the fixture registry.

    Deliberately does NOT enter the TestClient context manager: doing
    so runs the lifespan, which re-inits ``_registry`` from the real
    config dir and would shadow the tmp-path registry the tests mint
    keys into. Auth env vars are read at request time, so tests
    parametrize via ``monkeypatch.setenv`` / ``delenv`` per case.
    """
    app = create_app()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no auth configuration."""
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)


def _mint(registry, scopes: list[str], name: str = "test-key") -> str:
    """Mint and persist a scoped key; return the bearer token."""
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token


# ---------------------------------------------------------------------------
# Mode resolution (unit level)
# ---------------------------------------------------------------------------


class TestResolveAuthMode:
    def test_unset_no_legacy_key_is_off(self) -> None:
        assert resolve_auth_mode() == "off"

    def test_unset_with_legacy_key_is_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "s3cret")
        assert resolve_auth_mode() == "required"

    @pytest.mark.parametrize("mode", ["off", "optional", "required"])
    def test_explicit_modes(self, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", mode)
        assert resolve_auth_mode() == mode

    def test_mode_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "Required")
        assert resolve_auth_mode() == "required"

    def test_invalid_mode_raises_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "definitely-not-a-mode")
        with pytest.raises(ConfigError, match="TRELLIS_AUTH_MODE"):
            resolve_auth_mode()

    def test_invalid_mode_crashes_startup_warning_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "bogus")
        with pytest.raises(ConfigError):
            warn_if_unauthenticated()


# ---------------------------------------------------------------------------
# off mode (and backwards-compat default)
# ---------------------------------------------------------------------------


class TestOffMode:
    def test_default_unset_passes_without_credentials(self, client: TestClient) -> None:
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200, resp.text

    def test_explicit_off_passes_without_credentials(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200, resp.text

    def test_off_ignores_garbage_credentials(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "garbage"})
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# optional mode
# ---------------------------------------------------------------------------


class TestOptionalMode:
    def test_no_credential_passes_with_all_scopes(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "optional")
        # /stats is admin-scoped — anonymous passthrough grants all scopes.
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200, resp.text

    def test_bad_credential_is_401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "optional")
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "garbage"})
        assert resp.status_code == 401

    def test_valid_credential_is_scoped(
        self, client: TestClient, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "optional")
        token = _mint(registry, [SCOPE_READ])
        resp = client.get("/api/v1/stats", headers={"X-API-Key": token})
        assert resp.status_code == 403  # read key on admin route


# ---------------------------------------------------------------------------
# required mode
# ---------------------------------------------------------------------------


class TestRequiredMode:
    @pytest.fixture(autouse=True)
    def _required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

    def test_missing_credential_is_401(self, client: TestClient) -> None:
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 401
        # Undifferentiated body — no key_id / reason leakage.
        assert resp.json()["detail"] == "missing or invalid API credentials"

    def test_malformed_token_is_401(self, client: TestClient) -> None:
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "garbage"})
        assert resp.status_code == 401

    def test_revoked_key_is_401(self, client: TestClient, registry) -> None:
        token, record = generate_api_key("revoked", [SCOPE_ADMIN])
        store = registry.operational.api_key_store
        store.create(record)
        store.revoke(record.key_id)
        resp = client.get("/api/v1/stats", headers={"X-API-Key": token})
        assert resp.status_code == 401

    def test_valid_admin_key_passes(self, client: TestClient, registry) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        resp = client.get("/api/v1/stats", headers={"X-API-Key": token})
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


class TestScopeEnforcement:
    @pytest.fixture(autouse=True)
    def _required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

    def test_read_key_allowed_on_retrieve(self, client: TestClient, registry) -> None:
        token = _mint(registry, [SCOPE_READ])
        resp = client.get("/api/v1/traces", headers={"X-API-Key": token})
        assert resp.status_code == 200, resp.text

    def test_read_key_denied_on_mutations_batch(
        self, client: TestClient, registry
    ) -> None:
        token = _mint(registry, [SCOPE_READ])
        resp = client.post(
            "/api/v1/commands/batch",
            json={"commands": []},
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 403
        assert "mutate" in resp.json()["detail"]

    def test_read_key_denied_on_ingest(self, client: TestClient, registry) -> None:
        token = _mint(registry, [SCOPE_READ])
        resp = client.post("/api/v1/traces", json={}, headers={"X-API-Key": token})
        assert resp.status_code == 403

    def test_mutate_key_denied_on_admin(self, client: TestClient, registry) -> None:
        token = _mint(registry, [SCOPE_MUTATE])
        resp = client.get("/api/v1/stats", headers={"X-API-Key": token})
        assert resp.status_code == 403

    def test_admin_key_implies_all_scopes(self, client: TestClient, registry) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        # read route
        assert (
            client.get("/api/v1/traces", headers={"X-API-Key": token}).status_code
            == 200
        )
        # mutate route — past the auth gate (not 401/403)
        resp = client.post(
            "/api/v1/commands/batch",
            json={"commands": []},
            headers={"X-API-Key": token},
        )
        assert resp.status_code not in (401, 403)

    def test_observations_get_needs_read_post_needs_mutate(
        self, client: TestClient, registry
    ) -> None:
        token = _mint(registry, [SCOPE_READ])
        ok = client.get("/api/v1/observations", headers={"X-API-Key": token})
        assert ok.status_code == 200, ok.text
        denied = client.post(
            "/api/v1/observations", json={}, headers={"X-API-Key": token}
        )
        assert denied.status_code == 403

    def test_policies_get_needs_read_post_needs_admin(
        self, client: TestClient, registry
    ) -> None:
        token = _mint(registry, [SCOPE_READ, SCOPE_MUTATE])
        ok = client.get("/api/v1/policies", headers={"X-API-Key": token})
        assert ok.status_code == 200, ok.text
        denied = client.post("/api/v1/policies", json={}, headers={"X-API-Key": token})
        assert denied.status_code == 403
        also_denied = client.delete("/api/v1/policies/p1", headers={"X-API-Key": token})
        assert also_denied.status_code == 403


class TestPackTelemetryNeedsAdmin:
    """Events, packs and analyze reports replay other callers' pack
    telemetry (intents, served item ids and titles, attribution) — a
    `read` key (what an agent or connector holds) must not reach them.
    The content routes on the same routers (documents, graph history)
    are unaffected and stay on `read`.
    """

    @pytest.fixture(autouse=True)
    def _required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

    @pytest.fixture
    def seeded_pack_id(self, registry) -> str:
        """A real PACK_ASSEMBLED row so the detail routes can 200."""
        from trellis.stores.base.event_log import EventType

        event = registry.operational.event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack-telemetry-1",
            payload={"intent": "test intent", "items_count": 0},
        )
        return event.entity_id

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/v1/events"),
            ("get", "/api/v1/events/nonexistent"),
            ("get", "/api/v1/packs"),
            ("get", "/api/v1/packs/nonexistent"),
            ("get", "/api/v1/analyze"),
            ("get", "/api/v1/analyze/holdout"),
        ],
    )
    def test_read_key_is_403(
        self, client: TestClient, registry, method: str, path: str
    ) -> None:
        token = _mint(registry, [SCOPE_READ])
        resp = getattr(client, method)(path, headers={"X-API-Key": token})
        assert resp.status_code == 403, resp.text
        assert "admin" in resp.json()["detail"]

    def test_admin_key_is_200_on_list_routes(
        self, client: TestClient, registry
    ) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        for path in ("/api/v1/events", "/api/v1/packs", "/api/v1/analyze"):
            resp = client.get(path, headers={"X-API-Key": token})
            assert resp.status_code == 200, (path, resp.text)

    def test_admin_key_is_200_on_detail_routes(
        self, client: TestClient, registry, seeded_pack_id: str
    ) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        events = client.get("/api/v1/events", headers={"X-API-Key": token}).json()
        event_id = events["events"][0]["event_id"]
        resp = client.get(f"/api/v1/events/{event_id}", headers={"X-API-Key": token})
        assert resp.status_code == 200, resp.text
        resp = client.get(
            f"/api/v1/packs/{seeded_pack_id}", headers={"X-API-Key": token}
        )
        assert resp.status_code == 200, resp.text
        resp = client.get("/api/v1/analyze/holdout", headers={"X-API-Key": token})
        assert resp.status_code == 200, resp.text

    def test_read_key_still_200_on_content_routes(
        self, client: TestClient, registry
    ) -> None:
        """Documents and graph history share a router with the
        pack-telemetry routes above but are not telemetry — they stay
        read-scoped."""
        token = _mint(registry, [SCOPE_READ])
        resp = client.get("/api/v1/documents", headers={"X-API-Key": token})
        assert resp.status_code == 200, resp.text
        # No such node, but the response is a 404 from the handler, not a
        # 401/403 from the auth gate — proving the route itself is read.
        resp = client.get(
            "/api/v1/graph/history",
            params={"entity_id": "nonexistent"},
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Legacy shared secret
# ---------------------------------------------------------------------------


class TestLegacySharedSecret:
    def test_shared_secret_grants_admin(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unset mode + key set → inferred "required" (pre-scopes behavior).
        monkeypatch.setenv("TRELLIS_API_KEY", "legacy-secret-123")
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "legacy-secret-123"})
        assert resp.status_code == 200, resp.text

    def test_shared_secret_grants_mutate(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "legacy-secret-123")
        resp = client.post(
            "/api/v1/commands/batch",
            json={"commands": []},
            headers={"X-API-Key": "legacy-secret-123"},
        )
        assert resp.status_code not in (401, 403)

    def test_wrong_shared_secret_is_401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "legacy-secret-123")
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_shared_secret_works_in_required_mode_with_scoped_keys(
        self, client: TestClient, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv("TRELLIS_API_KEY", "legacy-secret-123")
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "legacy-secret-123"})
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Header handling
# ---------------------------------------------------------------------------


class TestHeaders:
    @pytest.fixture(autouse=True)
    def _required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

    def test_bearer_header_parity(self, client: TestClient, registry) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        resp = client.get("/api/v1/stats", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text

    def test_bearer_scheme_case_insensitive(self, client: TestClient, registry) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        resp = client.get("/api/v1/stats", headers={"Authorization": f"bearer {token}"})
        assert resp.status_code == 200, resp.text

    def test_x_api_key_wins_over_bearer(self, client: TestClient, registry) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        # Valid Bearer + garbage X-API-Key → X-API-Key wins → 401.
        resp = client.get(
            "/api/v1/stats",
            headers={
                "X-API-Key": "garbage",
                "Authorization": f"Bearer {token}",
            },
        )
        assert resp.status_code == 401

    def test_non_bearer_authorization_is_ignored(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/stats", headers={"Authorization": "Basic dXNlcjpwdw=="}
        )
        # No usable credential presented → 401 in required mode.
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Probes stay open
# ---------------------------------------------------------------------------


class TestProbesStayOpen:
    @pytest.fixture(autouse=True)
    def _required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

    def test_healthz_open(self, client: TestClient) -> None:
        assert client.get("/healthz").status_code == 200

    def test_readyz_open(self, client: TestClient) -> None:
        assert client.get("/readyz").status_code == 200

    def test_version_open(self, client: TestClient) -> None:
        assert client.get("/api/version").status_code == 200
