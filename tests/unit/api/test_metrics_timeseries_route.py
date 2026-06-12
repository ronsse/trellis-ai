"""Tests for the WP11 metrics-dashboard admin endpoint.

Covers the governance guarantee (admin scope required), metric-name
validation (422 on an unknown metric / group_by / bucket), and the
response shape against a seeded EventLog.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_ADMIN, SCOPE_READ, generate_api_key
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TRELLIS_API_KEY", "TRELLIS_AUTH_MODE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry(tmp_path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    from trellis_api.routes import admin

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    return TestClient(app)


def _mint(registry, scopes, name="test-key"):
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token


def _seed_graded_pack(registry, *, pack_id, success, domain=None):
    now = datetime.now(tz=UTC)
    payload = {"injected_item_ids": ["a", "b"]}
    if domain is not None:
        payload["domain"] = domain
    registry.operational.event_log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            occurred_at=now,
            payload=payload,
        )
    )
    registry.operational.event_log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=pack_id,
            entity_type="feedback",
            occurred_at=now,
            payload={
                "pack_id": pack_id,
                "success": success,
                "items_served": ["a", "b"],
                "helpful_item_ids": ["a"] if success else [],
            },
        )
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuthRequired:
    @pytest.fixture
    def auth_client(self, registry, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        return TestClient(create_app())

    def test_requires_credential(self, auth_client):
        resp = auth_client.get(
            "/api/v1/metrics/timeseries?metric=pack_success_rate"
        )
        assert resp.status_code == 401

    def test_read_scope_forbidden(self, registry, auth_client):
        token = _mint(registry, [SCOPE_READ])
        resp = auth_client.get(
            "/api/v1/metrics/timeseries?metric=pack_success_rate",
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 403

    def test_admin_scope_passes(self, registry, auth_client):
        token = _mint(registry, [SCOPE_ADMIN])
        resp = auth_client.get(
            "/api/v1/metrics/timeseries?metric=pack_success_rate",
            headers={"X-API-Key": token},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_metric_is_422(self, client):
        resp = client.get("/api/v1/metrics/timeseries?metric=bogus")
        assert resp.status_code == 422
        assert "bogus" in resp.json()["detail"]

    def test_unknown_group_by_is_422(self, client):
        resp = client.get(
            "/api/v1/metrics/timeseries?metric=pack_success_rate&group_by=nope"
        )
        assert resp.status_code == 422

    def test_unsupported_bucket_is_422(self, client):
        resp = client.get(
            "/api/v1/metrics/timeseries?metric=pack_success_rate&bucket=week"
        )
        assert resp.status_code == 422

    def test_missing_metric_is_422(self, client):
        # FastAPI rejects the missing required query param itself.
        resp = client.get("/api/v1/metrics/timeseries")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


class TestResponseShape:
    def test_empty_store_empty_series(self, client):
        resp = client.get(
            "/api/v1/metrics/timeseries?metric=pack_success_rate&days=30"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["metric"] == "pack_success_rate"
        assert data["bucket"] == "day"
        assert data["group_by"] == "none"
        assert data["days"] == 30
        assert data["series"] == []

    def test_seeded_series_shape(self, client, registry):
        _seed_graded_pack(registry, pack_id="p1", success=True)
        _seed_graded_pack(registry, pack_id="p2", success=False)
        resp = client.get(
            "/api/v1/metrics/timeseries?metric=pack_success_rate&days=30"
        )
        data = resp.json()
        assert len(data["series"]) == 1
        series = data["series"][0]
        assert series["group_key"] == "all"
        assert len(series["points"]) == 1
        point = series["points"][0]
        assert point["value"] == 0.5
        assert point["sample_count"] == 2
        assert "bucket_start" in point

    def test_group_by_domain_shape(self, client, registry):
        _seed_graded_pack(registry, pack_id="p1", success=True, domain="alpha")
        _seed_graded_pack(registry, pack_id="p2", success=False, domain="beta")
        resp = client.get(
            "/api/v1/metrics/timeseries"
            "?metric=pack_success_rate&group_by=domain"
        )
        data = resp.json()
        assert data["group_by"] == "domain"
        keys = {s["group_key"] for s in data["series"]}
        assert keys == {"alpha", "beta"}

    def test_reference_rate_metric(self, client, registry):
        _seed_graded_pack(registry, pack_id="p1", success=True)
        resp = client.get(
            "/api/v1/metrics/timeseries?metric=reference_rate&days=30"
        )
        assert resp.status_code == 200
        # 1 referenced / 2 served = 0.5
        assert resp.json()["series"][0]["points"][0]["value"] == 0.5
