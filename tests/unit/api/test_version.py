"""Tests for the version handshake route and deprecation helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from trellis.api_version import (
    API_MAJOR,
    API_MINOR,
    MCP_TOOLS_VERSION,
    SDK_MIN,
    WIRE_SCHEMA,
)
from trellis_api import deprecation
from trellis_api.deprecation import (
    DeprecationEntry,
    apply_deprecation_headers,
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

    def test_no_deprecations_by_default(self, client):
        resp = client.get("/api/version")
        assert resp.json()["deprecations"] == []

    def test_deprecations_surfaced_when_registered(self, client, monkeypatch):
        monkeypatch.setitem(
            deprecation.ROUTE_DEPRECATIONS,
            "/api/v1/old/path",
            DeprecationEntry(
                deprecated_since=date(2026, 4, 17),
                sunset_on=date(2026, 10, 17),
                replacement="/api/v1/new/path",
                reason="renamed for clarity",
            ),
        )
        resp = client.get("/api/version")
        deps = resp.json()["deprecations"]
        assert len(deps) == 1
        entry = deps[0]
        assert entry["path"] == "/api/v1/old/path"
        assert entry["deprecated_since"] == "2026-04-17"
        assert entry["sunset_on"] == "2026-10-17"
        assert entry["replacement"] == "/api/v1/new/path"

    def test_version_route_needs_no_store(self, client):
        # Calling twice in a row must not raise — proves the route
        # doesn't reach into the store layer (the fixture provides none).
        for _ in range(3):
            assert client.get("/api/version").status_code == 200


class TestDeprecationHeaders:
    def test_no_op_when_path_not_deprecated(self):
        resp = Response()
        apply_deprecation_headers(resp, "/api/v1/totally/fine")
        assert "Deprecation" not in resp.headers
        assert "Sunset" not in resp.headers
        assert "Link" not in resp.headers

    def test_sets_rfc_headers_when_deprecated(self, monkeypatch):
        monkeypatch.setitem(
            deprecation.ROUTE_DEPRECATIONS,
            "/api/v1/old",
            DeprecationEntry(
                deprecated_since=date(2026, 4, 17),
                sunset_on=date(2026, 10, 17),
                replacement="/api/v1/new",
            ),
        )
        resp = Response()
        apply_deprecation_headers(resp, "/api/v1/old")
        # RFC 9745 @<unix_ts> form
        assert resp.headers["Deprecation"].startswith("@")
        # RFC 8594 HTTP-date form
        assert "2026" in resp.headers["Sunset"]
        assert "GMT" in resp.headers["Sunset"]
        assert resp.headers["Link"] == '</api/v1/new>; rel="successor-version"'

    def test_link_omitted_when_no_replacement(self, monkeypatch):
        monkeypatch.setitem(
            deprecation.ROUTE_DEPRECATIONS,
            "/api/v1/dying",
            DeprecationEntry(
                deprecated_since=date(2026, 4, 17),
                sunset_on=date(2026, 10, 17),
            ),
        )
        resp = Response()
        apply_deprecation_headers(resp, "/api/v1/dying")
        assert "Deprecation" in resp.headers
        assert "Link" not in resp.headers
