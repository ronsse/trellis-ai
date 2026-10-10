"""Tests for ``GET /api/v1/loops`` (e167).

The claim under test: a loop with no event yet must read "never run",
never a bare ``0`` that is indistinguishable from "ran and did
nothing" — and the route requires admin scope like the rest of the
Review-queue surfaces it sits beside.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_ADMIN, SCOPE_READ, generate_api_key
from trellis.stores.base.event_log import EventType
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
    """App with just the admin router, auth OFF (the default with no env)."""

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


class TestLoopHealthRoute:
    def test_no_events_reports_never_run_not_zero(self, client) -> None:
        resp = client.get("/api/v1/loops")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["loops"]) == 7
        for row in data["loops"]:
            if row["name"] == "tuner":
                # Documented exception: pending_count/promoted_total are
                # live reads and stay populated at a real 0.
                assert row["last_run_at"] is None
                assert row["counters"] == {"pending_count": 0, "promoted_total": 0}
                continue
            assert row["last_run_at"] is None, row["name"]
            assert row["last_status"] is None, row["name"]
            assert row["counters"] is None, row["name"]

    def test_a_real_cycle_with_zero_counters_differs_from_never_run(
        self, registry, client
    ) -> None:
        registry.operational.event_log.emit(
            EventType.CURATE_CYCLE_COMPLETED,
            source="trellis.worker.curate",
            payload={
                "status": "ok",
                "noise_tagged": 0,
                "noise_refused_non_document": 0,
                "advisories_generated": 0,
                "advisories_refused": 0,
                "advisories_suppressed": 0,
                "advisories_boosted": 0,
                "advisory_store_degraded": False,
                "advisory_store_stale": False,
                "learning_observations": 0,
                "learning_candidates": 0,
                "learning_promotion_ready": {"count": 0},
                "skipped_stages": [],
                "dry_run": False,
            },
        )

        data = client.get("/api/v1/loops").json()

        noise_row = next(r for r in data["loops"] if r["name"] == "noise_demotion")
        assert noise_row["last_run_at"] is not None
        assert noise_row["last_status"] == "ok"
        assert noise_row["counters"] == {
            "noise_tagged": 0,
            "noise_refused_non_document": 0,
        }

    def test_every_row_carries_actuates_and_what_it_changes(self, client) -> None:
        data = client.get("/api/v1/loops").json()
        for row in data["loops"]:
            assert isinstance(row["actuates"], bool)
            assert row["what_it_changes"]
            assert row["description"]


class TestLoopHealthScope:
    @pytest.fixture
    def auth_client(self, registry, monkeypatch):
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        return TestClient(create_app())

    def test_requires_credential(self, auth_client) -> None:
        assert auth_client.get("/api/v1/loops").status_code == 401

    def test_read_scope_is_forbidden(self, registry, auth_client) -> None:
        token = _mint(registry, [SCOPE_READ])
        resp = auth_client.get("/api/v1/loops", headers={"X-API-Key": token})
        assert resp.status_code == 403

    def test_admin_scope_passes(self, registry, auth_client) -> None:
        token = _mint(registry, [SCOPE_ADMIN])
        resp = auth_client.get("/api/v1/loops", headers={"X-API-Key": token})
        assert resp.status_code == 200
