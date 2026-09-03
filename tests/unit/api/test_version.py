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
from trellis.core.write_config import ENV_VAR_BY_FIELD
from trellis.core.write_provenance import get_write_provenance
from trellis_api.auth import AUTH_MODE_ENV, AUTH_MODE_REQUIRED
from trellis_api.routes import version as version_route
from trellis_api.routes.health import OPS_DETAIL_ENV, OPS_DETAIL_PUBLIC


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

    def test_reports_write_provenance(self, client):
        """A running container can be asked what write semantics it applies.

        The drift this exists for is invisible otherwise: an image built
        six commits behind serves a superseded path and looks identical
        from the outside.
        """
        body = client.get("/api/version").json()
        provenance = body["write_provenance"]
        assert provenance["version"]
        assert provenance["version_source"]
        assert set(provenance["env_flags"]) == set(ENV_VAR_BY_FIELD)
        assert provenance["env_flags_digest"]

    def test_write_provenance_matches_the_event_stamp(self, client):
        """The endpoint must not report a different answer than the writes."""
        body = client.get("/api/version").json()
        assert body["write_provenance"] == dict(get_write_provenance())

    def test_write_provenance_withheld_from_anonymous_when_auth_required(
        self, client, monkeypatch
    ):
        """Build sha + effective flags are ops detail, gated like /readyz.

        The compatibility fields stay public — an SDK client must be able
        to negotiate before it has a key — but a deployment that opted
        into ``required`` does not hand its commit and enabled ingest
        behaviours to an unauthenticated caller.
        """
        monkeypatch.setenv(AUTH_MODE_ENV, AUTH_MODE_REQUIRED)
        body = client.get("/api/version").json()
        assert body["write_provenance"] is None
        assert body["api_major"] == API_MAJOR

    def test_ops_detail_public_restores_it_for_anonymous_callers(
        self, client, monkeypatch
    ):
        """The same opt-out that publishes the /readyz breakdown."""
        monkeypatch.setenv(AUTH_MODE_ENV, AUTH_MODE_REQUIRED)
        monkeypatch.setenv(OPS_DETAIL_ENV, OPS_DETAIL_PUBLIC)
        assert client.get("/api/version").json()["write_provenance"] is not None


class TestStampStaleness:
    """#348 — a server run from a drifted working tree says so."""

    LIVE_SHA = "def5678" + "1" * 33

    def test_stale_keys_reach_the_ops_detail_response(self, client, pin_source_tree):
        pin_source_tree(commit="abc1234", head=self.LIVE_SHA)
        provenance = client.get("/api/version").json()["write_provenance"]
        assert provenance["stamp_stale"] is True
        assert provenance["source_tree_commit"] == self.LIVE_SHA
        assert provenance["commit"] == "abc1234"

    def test_a_container_image_reports_no_staleness_keys(self, client, pin_source_tree):
        """The deployment shape this route was written for cannot drift."""
        pin_source_tree(
            commit=None, head=self.LIVE_SHA, tree=None, source="fallback-version"
        )
        provenance = client.get("/api/version").json()["write_provenance"]
        assert "stamp_stale" not in provenance
        assert "source_tree_commit" not in provenance
