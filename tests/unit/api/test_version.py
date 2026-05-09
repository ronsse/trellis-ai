"""Tests for the version handshake route."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trellis.api_version import (
    API_MAJOR,
    API_MINOR,
    MCP_TOOLS_VERSION,
    SDK_MIN,
    WIRE_SCHEMA,
)
from trellis_api.routes import version as version_route


@pytest.fixture
def client():
    """A minimal app that mounts only the version router.

    No store setup — the version route is deliberately independent of
    StoreRegistry so this fixture doesn't need tmp_path.
    """

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(version_route.router)
    with TestClient(app) as c:
        yield c


class TestVersionEndpoint:
    def test_returns_current_constants(self, client):
        resp = client.get("/api/version")
        assert resp.status_code == 200
        body = resp.json()
        assert body["api_major"] == API_MAJOR
        assert body["api_minor"] == API_MINOR
        assert body["api_version"] == f"{API_MAJOR}.{API_MINOR}"
        assert body["wire_schema"] == WIRE_SCHEMA
        assert body["sdk_min"] == SDK_MIN
        assert body["mcp_tools_version"] == MCP_TOOLS_VERSION

    def test_package_version_present(self, client):
        resp = client.get("/api/version")
        # Either a real version or the dev fallback — both are strings.
        assert isinstance(resp.json()["package_version"], str)

    def test_version_route_needs_no_store(self, client):
        # Calling twice in a row must not raise — proves the route
        # doesn't reach into the store layer (the fixture provides none).
        for _ in range(3):
            assert client.get("/api/version").status_code == 200
