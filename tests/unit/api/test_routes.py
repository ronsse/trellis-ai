"""Tests for the REST API routes."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import admin, curate, ingest, mutations, policies, retrieve


@pytest.fixture
def client(tmp_path):
    """Create a test client with a temporary store."""
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry

    # Build app without the default lifespan (which calls from_config_dir)
    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
    app.include_router(ingest.router, prefix="/api/v1", tags=["ingest"])
    app.include_router(retrieve.router, prefix="/api/v1", tags=["retrieve"])
    app.include_router(curate.router, prefix="/api/v1", tags=["curate"])
    app.include_router(mutations.router, prefix="/api/v1", tags=["mutations"])
    app.include_router(policies.router, prefix="/api/v1", tags=["policies"])

    with TestClient(app) as c:
        yield c
    registry.close()
    app_module._registry = None


def _make_trace(intent="test task", domain=None, agent_id=None):
    """Build a minimal valid trace payload."""
    ctx = {}
    if domain:
        ctx["domain"] = domain
    if agent_id:
        ctx["agent_id"] = agent_id
    return {
        "source": "agent",
        "intent": intent,
        "steps": [],
        "context": ctx,
    }


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_stats_empty(client):
    resp = client.get("/api/v1/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["traces"] == 0
    assert data["documents"] == 0


def test_ingest_trace(client):
    trace = _make_trace()
    resp = client.post("/api/v1/traces", json=trace)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["trace_id"] is not None


def test_ingest_invalid_trace(client):
    resp = client.post("/api/v1/traces", json={"bad": "data"})
    assert resp.status_code == 422


def test_search_empty(client):
    resp = client.get("/api/v1/search", params={"q": "test"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0


def test_list_traces(client):
    trace = _make_trace(intent="list test")
    client.post("/api/v1/traces", json=trace)

    resp = client.get("/api/v1/traces")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["traces"][0]["intent"] == "list test"


def test_get_trace_not_found(client):
    resp = client.get("/api/v1/traces/nonexistent")
    assert resp.status_code == 404


def test_get_trace_by_id(client):
    trace = _make_trace(intent="get by id")
    ingest_resp = client.post("/api/v1/traces", json=trace)
    trace_id = ingest_resp.json()["trace_id"]

    resp = client.get(f"/api/v1/traces/{trace_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["trace"]["intent"] == "get by id"


def test_create_entity(client):
    resp = client.post(
        "/api/v1/entities",
        json={
            "entity_type": "concept",
            "name": "test entity",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["node_id"] is not None


def test_get_entity(client):
    resp = client.post(
        "/api/v1/entities",
        json={
            "entity_type": "concept",
            "name": "test entity",
        },
    )
    node_id = resp.json()["node_id"]

    resp = client.get(f"/api/v1/entities/{node_id}")
    assert resp.status_code == 200
    assert resp.json()["entity"]["node_id"] == node_id


def test_entity_not_found(client):
    resp = client.get("/api/v1/entities/nonexistent")
    assert resp.status_code == 404


def test_assemble_pack(client):
    resp = client.post(
        "/api/v1/packs",
        json={
            "intent": "test pack",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pack_id"] is not None
    assert data["intent"] == "test pack"


def test_stats_after_ingest(client):
    trace = _make_trace()
    client.post("/api/v1/traces", json=trace)

    resp = client.get("/api/v1/stats")
    data = resp.json()
    assert data["traces"] == 1


def test_precedents_empty(client):
    resp = client.get("/api/v1/precedents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0


# -- Batch mutations --


def test_batch_creates_entities(client):
    """Batch endpoint creates multiple entities in one call."""
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "auth"},
                },
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "billing"},
                },
            ],
            "strategy": "sequential",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["executed"] == 2
    assert data["succeeded"] == 2
    assert data["failed"] == 0
    assert len(data["results"]) == 2
    assert all(r["status"] == "success" for r in data["results"])


def test_batch_stop_on_error(client):
    """Batch with stop_on_error halts after first failure."""
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "ok"},
                },
                {
                    "operation": "entity.create",
                    "args": {},  # missing required fields → validation fail
                },
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "never"},
                },
            ],
            "strategy": "stop_on_error",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["executed"] == 2  # stopped after failure
    assert data["succeeded"] == 1
    assert data["failed"] == 1


def test_batch_continue_on_error(client):
    """Batch with continue_on_error runs all commands."""
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "first"},
                },
                {
                    "operation": "entity.create",
                    "args": {},  # fails
                },
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "third"},
                },
            ],
            "strategy": "continue_on_error",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["executed"] == 3
    assert data["succeeded"] == 2
    assert data["failed"] == 1


def test_batch_idempotency(client):
    """Duplicate idempotency keys within a batch are detected."""
    resp = client.post(
        "/api/v1/commands/batch",
        json={
            "commands": [
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "dedup"},
                    "idempotency_key": "same-key",
                },
                {
                    "operation": "entity.create",
                    "args": {"entity_type": "service", "name": "dedup2"},
                    "idempotency_key": "same-key",
                },
            ],
            "strategy": "sequential",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["succeeded"] == 1
    assert data["duplicates"] == 1


# -- Bulk ingest --


def test_bulk_ingest_entities_edges_aliases(client):
    """End-to-end bulk ingest: entities → edges → aliases in one request."""
    resp = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [
                {
                    "entity_type": "service",
                    "name": "auth",
                    "entity_id": "svc-auth",
                    "properties": {"team": "platform"},
                },
                {
                    "entity_type": "service",
                    "name": "billing",
                    "entity_id": "svc-billing",
                },
            ],
            "edges": [
                {
                    "source_id": "svc-auth",
                    "target_id": "svc-billing",
                    "edge_kind": "entity_related_to",
                },
            ],
            "aliases": [
                {
                    "entity_id": "svc-auth",
                    "source_system": "k8s",
                    "raw_id": "auth-service",
                    "is_primary": True,
                },
            ],
            "requested_by": "bulk-test",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["batch_id"]
    assert data["strategy"] == "continue_on_error"

    assert data["entities"]["total"] == 2
    assert data["entities"]["succeeded"] == 2
    assert data["entities"]["failed"] == 0
    assert data["entities"]["results"][0]["id"] == "svc-auth"

    assert data["edges"]["total"] == 1
    assert data["edges"]["succeeded"] == 1
    assert data["edges"]["results"][0]["id"] is not None

    assert data["aliases"]["total"] == 1
    assert data["aliases"]["succeeded"] == 1
    assert data["aliases"]["results"][0]["name"] == "k8s:auth-service"


def test_bulk_ingest_empty_groups(client):
    """Empty request is valid and returns zero counts."""
    resp = client.post("/api/v1/ingest/bulk", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["entities"]["total"] == 0
    assert data["edges"]["total"] == 0
    assert data["aliases"]["total"] == 0


def test_bulk_ingest_continue_on_error(client):
    """continue_on_error runs all items, reports per-item failures."""
    resp = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [
                {
                    "entity_type": "service",
                    "name": "alpha",
                    "entity_id": "svc-alpha",
                },
                {
                    "entity_type": "service",
                    "name": "beta",
                    "entity_id": "svc-beta",
                },
            ],
            "edges": [
                # Second edge dangles — should fail but not halt the third
                {"source_id": "svc-alpha", "target_id": "svc-beta"},
                {"source_id": "nonexistent", "target_id": "svc-beta"},
                {"source_id": "svc-beta", "target_id": "svc-alpha"},
            ],
            "strategy": "continue_on_error",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["entities"]["succeeded"] == 2
    assert data["edges"]["total"] == 3
    assert data["edges"]["succeeded"] == 2
    assert data["edges"]["failed"] == 1
    assert data["edges"]["skipped"] == 0


def test_bulk_ingest_stop_on_error(client):
    """stop_on_error halts at first failure and skips remaining items across groups."""
    resp = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [
                {
                    "entity_type": "service",
                    "name": "alpha",
                    "entity_id": "svc-alpha",
                },
            ],
            "edges": [
                {"source_id": "nope-1", "target_id": "nope-2"},  # fails
                {"source_id": "svc-alpha", "target_id": "svc-alpha"},  # skipped
            ],
            "aliases": [
                # Should be skipped because edges halted
                {
                    "entity_id": "svc-alpha",
                    "source_system": "k8s",
                    "raw_id": "alpha",
                },
            ],
            "strategy": "stop_on_error",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["entities"]["succeeded"] == 1
    assert data["edges"]["failed"] == 1
    assert data["edges"]["skipped"] == 1
    assert data["aliases"]["skipped"] == 1
    assert data["aliases"]["succeeded"] == 0


def test_bulk_ingest_idempotency(client):
    """Per-item idempotency keys deduplicate within a single bulk batch."""
    resp = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [
                {
                    "entity_type": "service",
                    "name": "dup-a",
                    "idempotency_key": "bulk-key-1",
                },
                {
                    "entity_type": "service",
                    "name": "dup-b",
                    "idempotency_key": "bulk-key-1",
                },
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["entities"]["succeeded"] == 1
    assert data["entities"]["duplicates"] == 1


def test_bulk_ingest_invalid_strategy(client):
    resp = client.post(
        "/api/v1/ingest/bulk",
        json={"entities": [], "strategy": "nonsense"},
    )
    assert resp.status_code == 422


# -- Policy API --


def test_list_policies_empty(client):
    resp = client.get("/api/v1/policies")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["policies"] == []


def test_create_and_list_policy(client):
    resp = client.post(
        "/api/v1/policies",
        json={
            "policy_type": "mutation",
            "scope": {"level": "global"},
            "rules": [{"operation": "entity.create", "action": "deny"}],
            "enforcement": "enforce",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    policy_id = data["policy_id"]

    # List
    resp = client.get("/api/v1/policies")
    assert resp.json()["count"] == 1
    assert resp.json()["policies"][0]["policy_id"] == policy_id


def test_get_policy(client):
    create_resp = client.post(
        "/api/v1/policies",
        json={
            "policy_type": "mutation",
            "scope": {"level": "domain", "value": "payments"},
            "rules": [{"operation": "*", "action": "warn"}],
            "enforcement": "warn",
        },
    )
    policy_id = create_resp.json()["policy_id"]

    resp = client.get(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 200
    assert resp.json()["policy"]["scope"]["value"] == "payments"


def test_get_policy_not_found(client):
    resp = client.get("/api/v1/policies/nonexistent")
    assert resp.status_code == 404


def test_delete_policy(client):
    create_resp = client.post(
        "/api/v1/policies",
        json={
            "policy_type": "mutation",
            "scope": {"level": "global"},
            "rules": [{"operation": "*", "action": "deny"}],
        },
    )
    policy_id = create_resp.json()["policy_id"]

    resp = client.delete(f"/api/v1/policies/{policy_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Verify gone
    assert client.get("/api/v1/policies").json()["count"] == 0


def test_delete_policy_not_found(client):
    resp = client.delete("/api/v1/policies/nonexistent")
    assert resp.status_code == 404
