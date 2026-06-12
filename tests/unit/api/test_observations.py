"""REST API tests for the Observation + Measurement routes.

Item 1 Phase 1. In-process FastAPI ``TestClient`` matches the pattern in
``test_routes.py`` and keeps these tests fast (no uvicorn subprocess).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import curate, observations


@pytest.fixture
def client(tmp_path):
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(curate.router, prefix="/api/v1", tags=["curate"])
    app.include_router(observations.router, prefix="/api/v1", tags=["observations"])
    with TestClient(app) as c:
        yield c
    registry.close()
    app_module._registry = None


def _seed_subject(client) -> str:
    resp = client.post(
        "/api/v1/entities",
        json={"entity_type": "Dataset", "name": "users", "properties": {}},
    )
    assert resp.status_code == 200
    return resp.json()["node_id"]


def test_post_then_get_observation(client) -> None:
    subject_id = _seed_subject(client)
    resp = client.post(
        "/api/v1/observations",
        json={
            "subject_entity_id": subject_id,
            "subject_entity_type": "Dataset",
            "observer_agent_id": "agent-1",
            "content": "filter-projection asymmetry on email",
            "confidence": 0.9,
        },
    )
    assert resp.status_code == 201
    observation_id = resp.json()["observation_id"]
    assert observation_id

    resp = client.get("/api/v1/observations", params={"subject_entity_id": subject_id})
    assert resp.status_code == 200
    rows = resp.json()["observations"]
    assert len(rows) == 1
    assert rows[0]["observation_id"] == observation_id
    assert rows[0]["content"].startswith("filter-projection")


def test_post_observation_missing_required_field_returns_422(client) -> None:
    resp = client.post(
        "/api/v1/observations",
        json={
            "subject_entity_id": "ds-1",
            "subject_entity_type": "Dataset",
            # missing content, confidence, observer_agent_id
        },
    )
    assert resp.status_code == 422


def test_post_then_get_measurement(client) -> None:
    subject_id = _seed_subject(client)
    resp = client.post(
        "/api/v1/measurements",
        json={
            "subject_entity_id": subject_id,
            "subject_entity_type": "Dataset",
            "metric_name": "null_rate",
            "metric_value": 0.07,
            "unit": "percent",
            "observer_agent_id": "agent-1",
        },
    )
    assert resp.status_code == 201
    measurement_id = resp.json()["measurement_id"]
    assert measurement_id

    resp = client.get("/api/v1/measurements", params={"metric_name": "null_rate"})
    assert resp.status_code == 200
    rows = resp.json()["measurements"]
    assert len(rows) == 1
    assert rows[0]["measurement_id"] == measurement_id
    assert rows[0]["metric_value"] == pytest.approx(0.07)


def test_query_filters_compose(client) -> None:
    """subject + observer filters AND together."""
    subject_id = _seed_subject(client)
    for observer in ("agent-a", "agent-b"):
        resp = client.post(
            "/api/v1/observations",
            json={
                "subject_entity_id": subject_id,
                "subject_entity_type": "Dataset",
                "observer_agent_id": observer,
                "content": "x",
                "confidence": 0.5,
            },
        )
        assert resp.status_code == 201

    resp = client.get(
        "/api/v1/observations",
        params={"subject_entity_id": subject_id, "observer_agent_id": "agent-a"},
    )
    assert resp.status_code == 200
    rows = resp.json()["observations"]
    assert len(rows) == 1
    assert rows[0]["observer_agent_id"] == "agent-a"
