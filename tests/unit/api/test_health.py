"""Tests for /healthz and /readyz probe routes."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import health


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
