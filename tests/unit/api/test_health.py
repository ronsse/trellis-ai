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
    def test_ready_when_registry_initialized(self, client_ready):
        resp = client_ready.get("/readyz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}

    def test_initializing_when_registry_absent(self, client_unready):
        resp = client_unready.get("/readyz")
        assert resp.status_code == 503
        assert resp.json() == {"status": "initializing"}
