"""Tests for /healthz and /readyz probe routes."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_READ, generate_api_key
from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import health
from trellis_api.routes.health import resolve_ops_detail


@pytest.fixture(autouse=True)
def _clean_exposure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no auth / ops-detail configuration."""
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("TRELLIS_OPS_DETAIL", raising=False)


@pytest.fixture
def client_ready(tmp_path):
    """Client with a fully-initialized in-memory registry — /readyz returns 200."""
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(health.router)
    with TestClient(app) as c:
        yield c
    registry.close()
    app_module._registry = None


@pytest.fixture
def client_unready():
    """Client with no registry — /readyz returns 503."""
    app_module._registry = None

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(health.router)
    with TestClient(app) as c:
        yield c


class TestHealthz:
    def test_always_ok(self, client_unready):
        resp = client_unready.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_never_touches_stores(self, client_unready):
        for _ in range(3):
            assert client_unready.get("/healthz").status_code == 200


class TestReadyz:
    def test_ready_when_all_backends_probe_clean(self, client_ready):
        resp = client_ready.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        # Every cloud-backend an agent's request depends on must be probed.
        assert set(body["backends"].keys()) == {
            "event_log",
            "graph_store",
            "vector_store",
            "document_store",
        }
        for name, backend in body["backends"].items():
            assert backend["status"] == "ok", (name, backend)
            assert "latency_ms" in backend

    def test_initializing_when_registry_absent(self, client_unready):
        resp = client_unready.get("/readyz")
        assert resp.status_code == 503
        assert resp.json() == {"status": "initializing"}

    def test_degraded_when_a_backend_throws(
        self, client_ready, monkeypatch: pytest.MonkeyPatch
    ):
        """A failing backend flips overall status to 503 + lets ops see
        which one is down without grepping logs."""
        registry = app_module._registry
        assert registry is not None

        def _broken_count() -> int:
            msg = "vector backend unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr(registry.knowledge.vector_store, "count", _broken_count)

        resp = client_ready.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["backends"]["vector_store"]["status"] == "degraded"
        assert "vector backend unreachable" in body["backends"]["vector_store"]["error"]
        # Other backends still report individually.
        assert body["backends"]["event_log"]["status"] == "ok"


class TestResolveOpsDetail:
    def test_unset_defaults_authenticated(self) -> None:
        assert resolve_ops_detail() == "authenticated"

    @pytest.mark.parametrize("raw", ["public", "Public", " PUBLIC "])
    def test_public_case_insensitive(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_OPS_DETAIL", raw)
        assert resolve_ops_detail() == "public"

    def test_explicit_authenticated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_OPS_DETAIL", "authenticated")
        assert resolve_ops_detail() == "authenticated"

    @pytest.mark.parametrize("raw", ["everyone", "true", "false", "detail"])
    def test_invalid_raises_loudly(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_OPS_DETAIL", raw)
        with pytest.raises(ConfigError, match="TRELLIS_OPS_DETAIL"):
            resolve_ops_detail()


class TestReadyzDetailGating:
    """The /readyz status line is public; the per-backend breakdown is
    not (unless TRELLIS_OPS_DETAIL=public or the auth mode is open)."""

    def _mint_read_token(self) -> str:
        registry = app_module._registry
        assert registry is not None
        token, record = generate_api_key("probe-reader", [SCOPE_READ])
        registry.operational.api_key_store.create(record)
        return token

    def test_auth_off_detail_preserved(self, client_ready) -> None:
        """Dev posture unchanged — mode off means everyone is
        'authenticated', so the breakdown stays in the body."""
        body = client_ready.get("/readyz").json()
        assert "backends" in body

    def test_required_no_credential_is_minimal_200(
        self, client_ready, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        resp = client_ready.get("/readyz")
        # Orchestrator probes keep working with zero credentials.
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}

    def test_required_no_credential_degraded_is_minimal_503(
        self, client_ready, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        registry = app_module._registry
        assert registry is not None

        def _broken_count() -> int:
            msg = "vector backend unreachable"
            raise RuntimeError(msg)

        monkeypatch.setattr(registry.knowledge.vector_store, "count", _broken_count)
        resp = client_ready.get("/readyz")
        assert resp.status_code == 503
        # Status line only — no backend names / error strings leaked.
        assert resp.json() == {"status": "degraded"}

    def test_required_valid_credential_gets_detail(
        self, client_ready, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        token = self._mint_read_token()
        resp = client_ready.get("/readyz", headers={"X-API-Key": token})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert set(body["backends"].keys()) == {
            "event_log",
            "graph_store",
            "vector_store",
            "document_store",
        }

    def test_required_invalid_credential_is_401(
        self, client_ready, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Presented-but-invalid credential is loud, not silently
        downgraded to the anonymous minimal response."""
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        resp = client_ready.get("/readyz", headers={"X-API-Key": "garbage"})
        assert resp.status_code == 401

    def test_ops_detail_public_overrides_gating(
        self, client_ready, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv("TRELLIS_OPS_DETAIL", "public")
        body = client_ready.get("/readyz").json()
        assert "backends" in body

    def test_optional_mode_anonymous_gets_detail(
        self, client_ready, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Migration mode: anonymous passthrough carries all scopes, so
        the breakdown follows the same permissive posture."""
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "optional")
        body = client_ready.get("/readyz").json()
        assert "backends" in body

    def test_initializing_stays_minimal(
        self, client_unready, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        resp = client_unready.get("/readyz")
        assert resp.status_code == 503
        assert resp.json() == {"status": "initializing"}
