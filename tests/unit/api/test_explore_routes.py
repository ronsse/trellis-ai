"""Tests for the explore (Memory Explorer) read-only routes.

Covers document browsing (list previews, FTS search, single get),
event-log tailing (filters, ordering, payload stripping), pack
telemetry inspection (summary list, full detail, feedback join), graph
node history, and the sectioned-pack route the SDK targets.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis_api.routes import explore, retrieve


@pytest.fixture
def registry(tmp_path):
    """Fresh registry bound to the app module for each test."""
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


@pytest.fixture
def client(registry):
    """Test client with the explore + retrieve routers, no auth."""

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    app = FastAPI(lifespan=noop_lifespan)
    app.include_router(explore.router, prefix="/api/v1", tags=["explore"])
    app.include_router(retrieve.router, prefix="/api/v1", tags=["retrieve"])
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def test_documents_empty(client):
    resp = client.get("/api/v1/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["documents"] == []


def test_documents_list_previews(client, registry):
    store = registry.knowledge.document_store
    long_content = "x" * 1000
    store.put("doc-long", long_content, {"tags": {"domain": ["testing"]}})
    store.put("doc-short", "short note", None)

    resp = client.get("/api/v1/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["count"] == 2
    by_id = {d["doc_id"]: d for d in data["documents"]}
    # Preview is truncated; full content never ships in list rows.
    assert len(by_id["doc-long"]["preview"]) == 300
    assert by_id["doc-long"]["content_length"] == 1000
    assert "content" not in by_id["doc-long"]
    assert by_id["doc-long"]["metadata"]["tags"]["domain"] == ["testing"]


def test_documents_pagination(client, registry):
    store = registry.knowledge.document_store
    for i in range(5):
        store.put(f"doc-{i}", f"content {i}", None)

    resp = client.get("/api/v1/documents", params={"limit": 2, "offset": 4})
    data = resp.json()
    assert data["total"] == 5
    assert data["count"] == 1
    assert data["offset"] == 4


def test_documents_search(client, registry):
    store = registry.knowledge.document_store
    store.put("doc-pg", "postgres connection pooling guide", None)
    store.put("doc-other", "unrelated gardening notes", None)

    resp = client.get("/api/v1/documents", params={"q": "postgres"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["documents"][0]["doc_id"] == "doc-pg"
    assert "rank" in data["documents"][0]


def test_document_get(client, registry):
    registry.knowledge.document_store.put("doc-1", "full content here", {"k": "v"})
    resp = client.get("/api/v1/documents/doc-1")
    assert resp.status_code == 200
    doc = resp.json()["document"]
    assert doc["content"] == "full content here"
    assert doc["metadata"] == {"k": "v"}
    assert doc["content_hash"]


def test_document_get_404(client):
    resp = client.get("/api/v1/documents/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _seed_events(registry):
    log = registry.operational.event_log
    log.emit(
        EventType.MEMORY_STORED,
        source="mcp_server",
        entity_id="doc-1",
        payload={"doc_id": "doc-1", "deduped": False},
    )
    log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mutation_executor",
        entity_id="pack-1",
        payload={"pack_id": "pack-1", "rating": 1},
    )
    return log


def test_events_list_desc_and_stripped(client, registry):
    _seed_events(registry)
    resp = client.get("/api/v1/events")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["count"] == 2
    # Newest first by default
    assert data["events"][0]["event_type"] == EventType.FEEDBACK_RECORDED.value
    # Payload stripped to keys + summary by default
    first = data["events"][0]
    assert "payload" not in first
    assert first["payload_keys"] == ["pack_id", "rating"]
    assert first["payload_summary"]["rating"] == 1
    # Enum values are surfaced for filter UIs
    assert EventType.PACK_ASSEMBLED.value in data["event_types"]


def test_events_include_payload(client, registry):
    _seed_events(registry)
    resp = client.get("/api/v1/events", params={"include_payload": "true"})
    events = resp.json()["events"]
    assert all("payload" in e for e in events)
    assert events[0]["payload"]["pack_id"] == "pack-1"


def test_events_filters(client, registry):
    _seed_events(registry)
    resp = client.get(
        "/api/v1/events",
        params={"event_type": EventType.MEMORY_STORED.value},
    )
    data = resp.json()
    assert data["count"] == 1
    assert data["events"][0]["entity_id"] == "doc-1"

    resp = client.get("/api/v1/events", params={"source": "mutation_executor"})
    assert resp.json()["count"] == 1

    resp = client.get("/api/v1/events", params={"entity_id": "doc-1"})
    assert resp.json()["count"] == 1


def test_events_bad_event_type_422(client):
    resp = client.get("/api/v1/events", params={"event_type": "not.a.thing"})
    assert resp.status_code == 422
    assert "not.a.thing" in resp.json()["detail"]


def test_events_bad_order_422(client):
    resp = client.get("/api/v1/events", params={"order": "sideways"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------

_PACK_PAYLOAD = {
    "intent": "debug flaky test",
    "domain": "ci",
    "agent_id": "claude",
    "session_id": None,
    "items_count": 2,
    "candidates_found": 5,
    "strategies_used": ["keyword", "semantic"],
    "budget_max_items": 50,
    "budget_max_tokens": 8000,
    "injected_item_ids": ["item-a", "item-b"],
    "injected_items": [
        {
            "item_id": "item-a",
            "item_type": "document",
            "rank": 1,
            "selection_reason": "keyword match",
            "score_breakdown": {"keyword": 0.9},
            "estimated_tokens": 120,
            "strategy_source": "keyword",
            "injected_advisory_ids": [],
        },
        {
            "item_id": "item-b",
            "item_type": "trace",
            "rank": 2,
            "selection_reason": "semantic",
            "score_breakdown": {"semantic": 0.7},
            "estimated_tokens": 300,
            "strategy_source": "semantic",
            "injected_advisory_ids": [],
        },
    ],
    "rejected_items": [
        {
            "item_id": "item-c",
            "item_type": "document",
            "relevance_score": 0.1,
            "reason": "below_threshold",
            "strategy_source": "keyword",
        }
    ],
    "budget_trace": [
        {
            "item_id": "item-a",
            "item_tokens": 120,
            "running_total": 120,
            "included": True,
        }
    ],
}


def _seed_pack(registry, pack_id="pack-1"):
    log = registry.operational.event_log
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="pack_builder",
        entity_id=pack_id,
        entity_type="pack",
        payload=_PACK_PAYLOAD,
    )
    log.emit(
        EventType.FEEDBACK_RECORDED,
        source="mutation_executor",
        entity_id=pack_id,
        payload={"pack_id": pack_id, "rating": 1, "helpful_item_ids": ["item-a"]},
    )


def test_packs_list(client, registry):
    _seed_pack(registry)
    resp = client.get("/api/v1/packs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    pack = data["packs"][0]
    assert pack["pack_id"] == "pack-1"
    assert pack["intent"] == "debug flaky test"
    assert pack["items_count"] == 2
    assert pack["strategies_used"] == ["keyword", "semantic"]
    # Summary rows never carry the full payload
    assert "payload" not in pack
    assert "injected_items" not in pack


def test_pack_detail_with_feedback(client, registry):
    _seed_pack(registry)
    resp = client.get("/api/v1/packs/pack-1")
    assert resp.status_code == 200
    data = resp.json()
    payload = data["pack"]["payload"]
    assert len(payload["injected_items"]) == 2
    assert payload["injected_items"][0]["selection_reason"] == "keyword match"
    assert payload["rejected_items"][0]["reason"] == "below_threshold"
    assert payload["budget_trace"][0]["included"] is True
    # Feedback joined on FEEDBACK_RECORDED.payload["pack_id"]
    assert len(data["feedback"]) == 1
    assert data["feedback"][0]["payload"]["rating"] == 1


def test_pack_detail_404(client):
    resp = client.get("/api/v1/packs/no-such-pack")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Graph node history
# ---------------------------------------------------------------------------


def test_graph_history(client, registry):
    store = registry.knowledge.graph_store
    store.upsert_node("svc-1", "service", {"name": "api", "owner": "alice"})
    store.upsert_node("svc-1", "service", {"name": "api", "owner": "bob"})

    resp = client.get("/api/v1/graph/history", params={"entity_id": "svc-1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    # Newest first: current version open-ended, prior version closed
    assert data["versions"][0]["valid_to"] is None
    assert data["versions"][0]["properties"]["owner"] == "bob"
    assert data["versions"][1]["valid_to"] is not None


def test_graph_history_404(client):
    resp = client.get("/api/v1/graph/history", params={"entity_id": "ghost"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sectioned packs (the route the SDK's assemble_sectioned_pack targets)
# ---------------------------------------------------------------------------


def test_sectioned_pack_roundtrip(client, registry):
    registry.knowledge.document_store.put(
        "doc-1", "postgres pooling guide", {"tags": {"domain": ["ci"]}}
    )
    body = {
        "intent": "configure postgres pooling",
        "sections": [
            {
                "name": "domain_knowledge",
                "retrieval_affinities": ["conventions"],
                "content_types": ["document"],
                "scopes": ["domain"],
                "max_tokens": 2000,
                "max_items": 10,
            }
        ],
        "domain": None,
        "agent_id": "tester",
    }
    resp = client.post("/api/v1/packs/sectioned", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["pack_id"]
    assert data["intent"] == "configure postgres pooling"
    assert len(data["sections"]) == 1
    assert data["sections"][0]["name"] == "domain_knowledge"


def test_sectioned_pack_invalid_section_422(client):
    body = {
        "intent": "x",
        "sections": [{"name": "s", "bogus_field": True}],
    }
    resp = client.post("/api/v1/packs/sectioned", json=body)
    assert resp.status_code == 422
