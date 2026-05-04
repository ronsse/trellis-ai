"""Tests for API key authentication on /api/v1 routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app


@pytest.fixture
def client(tmp_path):
    """In-memory app with a fresh registry for each test.

    Bypasses the lifespan (which would re-init the registry from
    config) by setting the module-level ``_registry`` directly. The
    auth dependency reads ``TRELLIS_API_KEY`` from os.environ at
    request time, so tests parametrize via ``monkeypatch.setenv`` /
    ``delenv`` per case.
    """
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry
    app = create_app()
    # The lifespan is registered but TestClient triggers it; replace
    # with a no-op by entering the context manager ourselves.
    with TestClient(app) as c:
        yield c
    registry.close()
    app_module._registry = None


class TestAuthDisabledByDefault:
    """When TRELLIS_API_KEY is unset, the API behaves like before."""

    def test_no_key_set_admin_stats_passes(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200, resp.text

    def test_no_key_set_with_arbitrary_header_still_passes(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "anything"})
        assert resp.status_code == 200, resp.text


class TestAuthEnforced:
    """When TRELLIS_API_KEY is set, /api/v1 requires the matching header."""

    def test_missing_header_returns_401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "test-secret-123")
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 401
        assert "X-API-Key" in resp.json()["detail"]

    def test_wrong_header_returns_401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "test-secret-123")
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "wrong-secret"})
        assert resp.status_code == 401

    def test_correct_header_passes(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "test-secret-123")
        resp = client.get("/api/v1/stats", headers={"X-API-Key": "test-secret-123"})
        assert resp.status_code == 200, resp.text


class TestProbesStayOpen:
    """Health and version probes never require auth — orchestrators
    must be able to probe without the secret."""

    def test_healthz_open_with_key_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "test-secret-123")
        resp = client.get("/healthz")
        assert resp.status_code == 200

    def test_readyz_open_with_key_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "test-secret-123")
        resp = client.get("/readyz")
        assert resp.status_code == 200

    def test_version_open_with_key_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "test-secret-123")
        resp = client.get("/api/version")
        assert resp.status_code == 200


class TestMutationsRoutesGated:
    """Mutating routes (POST/DELETE) get the same auth as reads."""

    def test_post_documents_requires_key(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "test-secret-123")
        resp = client.post(
            "/api/v1/documents",
            json={"doc_id": "d1", "content": "test"},
        )
        assert resp.status_code == 401

    def test_post_documents_with_key_proceeds(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_KEY", "test-secret-123")
        resp = client.post(
            "/api/v1/documents",
            json={"doc_id": "d1", "content": "test"},
            headers={"X-API-Key": "test-secret-123"},
        )
        # Doesn't have to succeed — just has to PAST the auth gate
        # (status != 401 proves auth let it through).
        assert resp.status_code != 401
