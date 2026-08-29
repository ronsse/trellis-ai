"""Tests for the explore (Memory Explorer) read-only routes.

Covers document browsing (list previews, FTS search, single get),
event-log tailing (filters, ordering, payload stripping), pack
telemetry inspection (summary list, full detail, feedback join), graph
node history, and the sectioned-pack route the SDK targets.

Also home to the chunk-visibility assertions for ``GET /api/v1/search``,
which is a *retrieve* route rather than an explore one. It sits here
because it makes the same promise as ``GET /api/v1/documents`` and #396
exists precisely because one was fixed without the other.
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


def _seed_chunked(store, parents=3, per_parent=3):
    """Parents plus their ``#chunk-N`` slices, the shape corpus ingest writes."""
    parent_ids = []
    for p in range(parents):
        parent_id = f"corpus:notes:doc{p}"
        store.put(parent_id, f"parent {p} distinctive body text")
        parent_ids.append(parent_id)
        for c in range(per_parent):
            store.put(
                f"{parent_id}#chunk-{c}",
                f"parent {p} distinctive body text slice {c}",
                {"parent_doc_id": parent_id, "chunk_index": c},
            )
    return parent_ids


def test_documents_excludes_chunks_by_default(client, registry):
    """#385 — the list view served 740 of 1,317 rows as chunk fragments."""
    parent_ids = _seed_chunked(registry.knowledge.document_store)

    data = client.get("/api/v1/documents").json()

    assert data["include_chunks"] is False
    assert {d["doc_id"] for d in data["documents"]} == set(parent_ids)
    # total is counted under the same filter as the rows it describes.
    assert data["total"] == len(parent_ids)
    assert data["count"] == len(parent_ids)


def test_documents_include_chunks_opt_in(client, registry):
    _seed_chunked(registry.knowledge.document_store)

    data = client.get("/api/v1/documents", params={"include_chunks": "true"}).json()

    assert data["include_chunks"] is True
    assert data["total"] == 12
    assert any("#chunk-" in d["doc_id"] for d in data["documents"])


def test_documents_search_excludes_chunks_without_losing_the_parent(client, registry):
    """The parent carries the text its chunks were sliced from, so recall holds."""
    parent_ids = _seed_chunked(registry.knowledge.document_store)

    unfiltered = client.get(
        "/api/v1/documents",
        params={"q": "distinctive", "include_chunks": "true"},
    ).json()
    assert any("#chunk-" in d["doc_id"] for d in unfiltered["documents"])

    filtered = client.get("/api/v1/documents", params={"q": "distinctive"}).json()
    assert {d["doc_id"] for d in filtered["documents"]} == set(parent_ids)


def test_documents_page_is_not_shortened_by_the_chunk_filter(client, registry):
    """A ``limit`` of N returns N non-chunk rows, not N minus the chunks.

    Pins the store-level pushdown: filtering the page after the read would
    return 5 rows here and give the caller no way to tell that from the end
    of the data.
    """
    _seed_chunked(registry.knowledge.document_store, parents=20, per_parent=3)

    data = client.get("/api/v1/documents", params={"limit": 20}).json()

    assert data["count"] == 20
    assert not [d for d in data["documents"] if "#chunk-" in d["doc_id"]]


def test_excluded_chunk_is_still_addressable(client, registry):
    """Excluded from the listing, never removed from the store.

    The id must be percent-encoded — ``#`` is a URL fragment delimiter, so
    an unencoded chunk id never leaves the client. That is ordinary URL
    handling, not a chunk-specific quirk (the Memory Explorer already calls
    ``encodeURIComponent``), but it is asserted here because "chunks stay
    addressable" is the claim that makes excluding them from the listing a
    demotion rather than a disappearance.
    """
    _seed_chunked(registry.knowledge.document_store, parents=1, per_parent=1)

    resp = client.get("/api/v1/documents/corpus:notes:doc0%23chunk-0")

    assert resp.status_code == 200
    assert resp.json()["document"]["doc_id"] == "corpus:notes:doc0#chunk-0"


# ---------------------------------------------------------------------------
# GET /api/v1/search — the other REST surface that hands back document rows
# ---------------------------------------------------------------------------
#
# Lives beside the ``/documents`` chunk tests rather than in
# ``test_routes.py`` on purpose: the two surfaces make the same promise and
# #396 exists because one of them was fixed without the other. A reviewer
# who changes one should be reading the other's assertions in the same
# screenful.


def test_search_excludes_chunks_by_default(client, registry):
    """#396 — the sibling surface #391 left behind."""
    parent_ids = _seed_chunked(registry.knowledge.document_store)

    data = client.get("/api/v1/search", params={"q": "distinctive"}).json()

    assert data["include_chunks"] is False
    assert {d["doc_id"] for d in data["results"]} == set(parent_ids)
    assert data["count"] == len(parent_ids)


def test_search_include_chunks_opt_in(client, registry):
    """The escape hatch, so exclusion is a default rather than a removal."""
    _seed_chunked(registry.knowledge.document_store)

    data = client.get(
        "/api/v1/search", params={"q": "distinctive", "include_chunks": "true"}
    ).json()

    assert data["include_chunks"] is True
    assert data["count"] == 12
    assert any("#chunk-" in d["doc_id"] for d in data["results"])


def test_search_result_set_is_not_shortened_by_the_chunk_filter(client, registry):
    """A ``limit`` of N returns N non-chunk rows, not N minus the chunks.

    Pins the store-level pushdown for the search path specifically. With 3
    chunks per parent, filtering the result set after the read would return
    5 rows for ``limit=20`` and the caller would read that as "only 5
    documents matched" — the same defect the ``/documents`` sibling pins,
    reached through a different store method.
    """
    _seed_chunked(registry.knowledge.document_store, parents=25, per_parent=3)

    data = client.get("/api/v1/search", params={"q": "distinctive", "limit": 20}).json()

    assert data["count"] == 20
    assert not [d for d in data["results"] if "#chunk-" in d["doc_id"]]


def test_search_excluded_chunk_is_still_addressable(client, registry):
    """Excluded from the ranking, never removed from the store."""
    _seed_chunked(registry.knowledge.document_store, parents=1, per_parent=1)

    resp = client.get("/api/v1/documents/corpus:notes:doc0%23chunk-0")

    assert resp.status_code == 200
    assert resp.json()["document"]["doc_id"] == "corpus:notes:doc0#chunk-0"


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
