"""Tests for the REST API routes."""

from __future__ import annotations

import importlib
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import trellis_api.app as app_module
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.errors import StaleStoreWriteError
from trellis.schemas.enums import PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.stores.base import VectorStore
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


def _rich_trace():
    return {
        "source": "agent",
        "intent": "fix the import",
        "steps": [{"step_type": "tool_call", "name": "grep"}],
        "context": {"agent_id": "a1", "domain": "backend"},
    }


def test_ingest_trace_extraction_flag_off(client, monkeypatch):
    """Flag off -> trace stored, graph untouched (byte-identical to today)."""
    monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
    resp = client.post("/api/v1/traces", json=_rich_trace())
    assert resp.status_code == 200
    assert app_module._registry.knowledge.graph_store.count_nodes() == 0


def test_ingest_trace_extraction_flag_on(client, monkeypatch):
    """Flag on -> graph populated, edges carry source_trace_id."""
    monkeypatch.setenv("TRELLIS_ENABLE_TRACE_EXTRACTION", "1")
    resp = client.post("/api/v1/traces", json=_rich_trace())
    assert resp.status_code == 200
    trace_id = resp.json()["trace_id"]

    graph = app_module._registry.knowledge.graph_store
    assert graph.count_nodes() > 0
    assert graph.get_node(f"trace:{trace_id}") is not None
    edges = graph.get_edges(f"trace:{trace_id}", direction="outgoing")
    assert edges
    for edge in edges:
        assert edge.get("properties", {}).get("source_trace_id") == trace_id


def test_ingest_trace_extraction_failure_does_not_fail_request(client, monkeypatch):
    """A broken extraction must never fail the ingest request."""
    monkeypatch.setenv("TRELLIS_ENABLE_TRACE_EXTRACTION", "1")
    import trellis.extract.trace_ingest_hook as hook

    def _boom(*_a, **_k):
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(hook, "result_to_batch", _boom)
    resp = client.post("/api/v1/traces", json=_rich_trace())
    assert resp.status_code == 200


# ── embed-on-ingest (TRELLIS_ENABLE_EMBED_ON_INGEST) ────────────────────

#: Dotted path handed to TRELLIS_EMBEDDING_FN; the registry resolves it
#: lazily, so setting the env inside a test is picked up on first use.
_EMBED_FN_PATH = "tests.unit.api.test_routes._fake_embed"


def _fake_embed(text: str) -> list[float]:
    return [1.0, 0.0, 0.5]


def _broken_embed(text: str) -> list[float]:
    msg = "embedder down"
    raise RuntimeError(msg)


def test_create_document_embed_flag_off(client, monkeypatch):
    """Flag off -> document stored, vector store untouched."""
    monkeypatch.delenv("TRELLIS_ENABLE_EMBED_ON_INGEST", raising=False)
    resp = client.post("/api/v1/documents", json={"content": "hello world"})
    assert resp.status_code == 200
    assert app_module._registry.knowledge.vector_store.count() == 0


def test_create_document_embed_flag_on(client, monkeypatch):
    """Flag on -> vector upserted keyed by doc_id, metadata carries excerpt."""
    monkeypatch.setenv("TRELLIS_ENABLE_EMBED_ON_INGEST", "1")
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", _EMBED_FN_PATH)
    resp = client.post(
        "/api/v1/documents",
        json={"content": "hello world", "metadata": {"domain": "backend"}},
    )
    assert resp.status_code == 200
    doc_id = resp.json()["doc_id"]
    row = app_module._registry.knowledge.vector_store.get(doc_id)
    assert row is not None
    assert row["metadata"]["content"] == "hello world"
    assert row["metadata"]["domain"] == "backend"


def test_create_document_embed_failure_does_not_fail_request(client, monkeypatch):
    """A broken embedder must never fail the document write."""
    monkeypatch.setenv("TRELLIS_ENABLE_EMBED_ON_INGEST", "1")
    monkeypatch.setenv(
        "TRELLIS_EMBEDDING_FN", "tests.unit.api.test_routes._broken_embed"
    )
    resp = client.post("/api/v1/documents", json={"content": "hello world"})
    assert resp.status_code == 200
    assert app_module._registry.knowledge.vector_store.count() == 0


def test_ingest_evidence_embed_flag_on(client, monkeypatch):
    """Evidence with content embeds under its evidence_id."""
    monkeypatch.setenv("TRELLIS_ENABLE_EMBED_ON_INGEST", "1")
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", _EMBED_FN_PATH)
    resp = client.post(
        "/api/v1/evidence",
        json={
            "evidence_type": "document",
            "content": "the API contract says X",
            "source_origin": "test",
        },
    )
    assert resp.status_code == 200
    evidence_id = resp.json()["evidence_id"]
    row = app_module._registry.knowledge.vector_store.get(evidence_id)
    assert row is not None
    assert row["metadata"]["evidence_type"] == "document"


# -- classify-on-write (feature-flagged) --------------------------------------
#
# The shared seam's contract is covered in tests/unit/classify/test_ingest.py;
# these prove the two REST document writers call it before the put. Content
# the deterministic classifiers confidently domain-tag, so the domain-drop
# assertions mean something.

_CLASSIFY_FLAG = "TRELLIS_ENABLE_CLASSIFY_ON_INGEST"
_INFRA = "kubernetes deployment infra helm terraform rollout"


class _BoomPipeline:
    def classify(self, *args, **kwargs):
        msg = "classifier exploded"
        raise RuntimeError(msg)


def _stored_metadata(doc_id):
    doc = app_module._registry.knowledge.document_store.get(doc_id)
    assert doc is not None
    return doc["metadata"] or {}


def test_create_document_classify_flag_off(client, monkeypatch):
    """Flag off -> document stored untagged."""
    monkeypatch.delenv(_CLASSIFY_FLAG, raising=False)
    resp = client.post("/api/v1/documents", json={"content": _INFRA})
    assert resp.status_code == 200
    assert "content_tags" not in _stored_metadata(resp.json()["doc_id"])


def test_create_document_classify_flag_on(client, monkeypatch):
    """Flag on -> tags persisted, minus the hard-excluding domain facet."""
    monkeypatch.setenv(_CLASSIFY_FLAG, "1")
    resp = client.post(
        "/api/v1/documents",
        json={"content": _INFRA, "metadata": {"source": "api"}},
    )
    assert resp.status_code == 200
    meta = _stored_metadata(resp.json()["doc_id"])
    assert meta["source"] == "api"
    assert meta["content_tags"]["signal_quality"]
    assert meta["content_tags"]["domain"] == []
    assert isinstance(meta["auto_importance"], float)


def test_create_document_classify_does_not_clobber_existing_tags(client, monkeypatch):
    """Caller tags survive — and the positive control proves the seam is wired.

    Asserting only that the caller's tags came back is trivially true if
    ``classify_metadata_on_write`` is never called at all, so the same test
    writes a second, tag-less document and asserts that one IS tagged.
    """
    monkeypatch.setenv(_CLASSIFY_FLAG, "1")
    caller_tags = {"domain": ["backend"], "signal_quality": "high"}
    resp = client.post(
        "/api/v1/documents",
        json={"content": _INFRA, "metadata": {"content_tags": caller_tags}},
    )
    assert resp.status_code == 200
    assert _stored_metadata(resp.json()["doc_id"])["content_tags"] == caller_tags

    control = client.post("/api/v1/documents", json={"content": _INFRA})
    assert control.status_code == 200
    assert _stored_metadata(control.json()["doc_id"])["content_tags"]["domain"] == []


def test_create_document_null_metadata_does_not_fail_request(client, monkeypatch):
    """``"metadata": null`` is a shape clients send; it must not 500.

    Regression: the seam's guards used to run outside its try/except, so a
    non-mapping ``metadata`` raised ``TypeError`` out of a durable write path
    (200 with the flag off, 500 with it on).
    """
    monkeypatch.setenv(_CLASSIFY_FLAG, "1")
    resp = client.post("/api/v1/documents", json={"content": _INFRA, "metadata": None})
    assert resp.status_code == 200
    assert _stored_metadata(resp.json()["doc_id"])["content_tags"]["domain"] == []


def test_create_document_classify_failure_does_not_fail_request(client, monkeypatch):
    """A broken classifier must never fail the document write."""
    import trellis.classify.ingest as ingest_mod

    monkeypatch.setenv(_CLASSIFY_FLAG, "1")
    monkeypatch.setattr(ingest_mod, "_ingest_classifier", _BoomPipeline())
    resp = client.post("/api/v1/documents", json={"content": _INFRA})
    assert resp.status_code == 200
    assert "content_tags" not in _stored_metadata(resp.json()["doc_id"])


def _post_evidence(client, content=_INFRA):
    return client.post(
        "/api/v1/evidence",
        json={
            "evidence_type": "document",
            "content": content,
            "source_origin": "test",
        },
    )


def test_ingest_evidence_classify_flag_off(client, monkeypatch):
    monkeypatch.delenv(_CLASSIFY_FLAG, raising=False)
    resp = _post_evidence(client)
    assert resp.status_code == 200
    assert "content_tags" not in _stored_metadata(resp.json()["evidence_id"])


def test_ingest_evidence_classify_flag_on(client, monkeypatch):
    monkeypatch.setenv(_CLASSIFY_FLAG, "1")
    resp = _post_evidence(client)
    assert resp.status_code == 200
    meta = _stored_metadata(resp.json()["evidence_id"])
    assert meta["evidence_type"] == "document"
    assert meta["content_tags"]["domain"] == []


def test_ingest_evidence_classify_skips_content_less_evidence(client, monkeypatch):
    """A uri-only evidence row has nothing to classify.

    The positive control (same test, evidence WITH content) is what makes the
    absence assertion mean "correctly skipped" rather than "never wired".
    """
    monkeypatch.setenv(_CLASSIFY_FLAG, "1")
    resp = client.post(
        "/api/v1/evidence",
        json={
            "evidence_type": "document",
            "uri": "https://example.test/doc",
            "source_origin": "test",
        },
    )
    assert resp.status_code == 200
    assert "content_tags" not in _stored_metadata(resp.json()["evidence_id"])

    control = _post_evidence(client)
    assert control.status_code == 200
    control_meta = _stored_metadata(control.json()["evidence_id"])
    assert control_meta["content_tags"]["domain"] == []


def test_ingest_evidence_classify_failure_does_not_fail_request(client, monkeypatch):
    import trellis.classify.ingest as ingest_mod

    monkeypatch.setenv(_CLASSIFY_FLAG, "1")
    monkeypatch.setattr(ingest_mod, "_ingest_classifier", _BoomPipeline())
    resp = _post_evidence(client)
    assert resp.status_code == 200
    assert "content_tags" not in _stored_metadata(resp.json()["evidence_id"])


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


# -- Links / allow_dangling (issue #211) --


def test_create_link_default_rejects_dangling_target(client):
    """Without allow_dangling, a link to a non-existent target is rejected.

    Mirrors the LinkCreateHandler FK pre-flight over the REST boundary:
    the source exists, the target does not, so the orphan-edge check fires
    and the route surfaces it as a 400.
    """
    src = client.post(
        "/api/v1/entities",
        json={"entity_type": "table", "name": "events", "entity_id": "tbl-events"},
    )
    assert src.status_code == 200

    resp = client.post(
        "/api/v1/links",
        json={
            "source_id": "tbl-events",
            "target_id": "tbl-ghost",
            "edge_kind": "references_table",
        },
    )
    assert resp.status_code == 400
    # Orphan-edge message names the missing endpoint.
    assert "tbl-ghost" in resp.json()["detail"]


def test_create_link_allow_dangling_writes_edge(client):
    """allow_dangling=true lets a curator write an edge-before-node over HTTP.

    The #211 path: a promoted table-reference edge whose target table has
    not been materialised yet must be writable when the caller opts in.
    """
    src = client.post(
        "/api/v1/entities",
        json={"entity_type": "table", "name": "events", "entity_id": "tbl-events2"},
    )
    assert src.status_code == 200

    resp = client.post(
        "/api/v1/links",
        json={
            "source_id": "tbl-events2",
            "target_id": "tbl-ghost2",
            "edge_kind": "references_table",
            "allow_dangling": True,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["edge_id"] is not None


def test_bulk_ingest_edge_allow_dangling(client):
    """A bulk edge with allow_dangling=true survives a missing target node."""
    resp = client.post(
        "/api/v1/ingest/bulk",
        json={
            "entities": [
                {"entity_type": "table", "name": "src", "entity_id": "tbl-src"},
            ],
            "edges": [
                {
                    "source_id": "tbl-src",
                    "target_id": "tbl-not-yet",
                    "edge_kind": "references_table",
                    "allow_dangling": True,
                },
            ],
            "strategy": "continue_on_error",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["edges"]["total"] == 1
    assert data["edges"]["succeeded"] == 1
    assert data["edges"]["rejected"] == 0


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


def test_assemble_pack_threads_attribution_into_telemetry(client):
    """The REST seam forwards run_id / intent_family to PackBuilder.

    The wire DTO accepting the fields is not enough — if the route drops
    them, every REST-served pack still lands in the learning join's
    ``unknown-run`` bucket.
    """
    from trellis.stores.base.event_log import EventType

    resp = client.post(
        "/api/v1/packs",
        json={"intent": "validate the pii convention", "run_id": "run-42"},
    )
    assert resp.status_code == 200

    events = app_module._registry.operational.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, limit=10
    )
    assert len(events) == 1
    assert events[0].payload["run_id"] == "run-42"
    # Derived server-side: the caller sent no intent_family.
    assert events[0].payload["intent_family"] == "validation_diagnostics"


def _seed_advisories(stores_dir, count):
    """Write ``count`` global advisories with strictly decreasing confidence."""
    from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
    from trellis.stores.advisory_store import AdvisoryStore

    store = AdvisoryStore(stores_dir / "advisories.json")
    for i in range(count):
        store.put(
            Advisory(
                advisory_id=f"adv-{i:02d}",
                category=AdvisoryCategory.APPROACH,
                confidence=round(0.9 - i * 0.05, 4),
                message=f"Finding {i} (n=5, effect=+60%).",
                evidence=AdvisoryEvidence(
                    sample_size=5,
                    success_rate_with=0.6,
                    success_rate_without=0.0,
                    effect_size=0.6,
                ),
                scope="global",
            )
        )
    return store


def test_assemble_pack_inherits_the_advisory_cap(client, tmp_path):
    """#392 — the cap is applied at assembly, so REST inherits it.

    ``POST /api/v1/packs`` dumps ``pack.advisories`` whole. Uncapped, the
    reference deployment shipped 44 full advisory objects — ~31,530 bytes
    per response — on a surface nobody had looked at. Capping at render
    would have left this one untouched.
    """
    from trellis.retrieve.pack_builder import PackBuilder

    _seed_advisories(tmp_path / "stores", 12)

    resp = client.post("/api/v1/packs", json={"intent": "test pack"})

    assert resp.status_code == 200
    served = [a["advisory_id"] for a in resp.json()["advisories"]]
    cap = PackBuilder._ADVISORY_MAX_COUNT
    assert served == [f"adv-{i:02d}" for i in range(cap)]


def test_assemble_sectioned_pack_inherits_the_advisory_cap(client, tmp_path):
    """The sectioned REST route dumps the same field and takes the same cut."""
    from trellis.retrieve.pack_builder import PackBuilder

    _seed_advisories(tmp_path / "stores", 12)

    resp = client.post(
        "/api/v1/packs/sectioned",
        json={"intent": "test pack", "sections": [{"name": "all"}]},
    )

    assert resp.status_code == 200
    served = [a["advisory_id"] for a in resp.json()["advisories"]]
    cap = PackBuilder._ADVISORY_MAX_COUNT
    assert served == [f"adv-{i:02d}" for i in range(cap)]


def test_assemble_pack_serves_every_advisory_below_the_cap(client, tmp_path):
    """The cap is a ceiling, not a fixed size — three in, three out."""
    _seed_advisories(tmp_path / "stores", 3)

    resp = client.post("/api/v1/packs", json={"intent": "test pack"})

    assert resp.status_code == 200
    served = [a["advisory_id"] for a in resp.json()["advisories"]]
    assert served == ["adv-00", "adv-01", "adv-02"]


def test_api_pack_builder_wires_semantic_dedup(tmp_path):
    """The HTTP pack path mirrors the MCP server: near-duplicate suppression
    is wired at assembly (F14, #259) so cross-source clones can't re-serve."""
    from trellis.retrieve.pack_builder import SemanticDedupConfig

    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    try:
        builder = retrieve._build_pack_builder(registry)
        assert isinstance(builder._semantic_dedup, SemanticDedupConfig)
        assert builder._semantic_dedup.threshold == 0.85
    finally:
        registry.close()


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
    # Variant A' (adr-extraction-validation.md §5.5): orphan-edge FK failures
    # raised by LinkCreateHandler now route through _emit_rejection and
    # surface as REJECTED, not FAILED.
    assert data["edges"]["rejected"] == 1
    assert data["edges"]["failed"] == 0
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
    # Orphan-edge FK rejection now surfaces as REJECTED (Variant A'); stop
    # semantics still halt the batch via _is_terminal_failure.
    assert data["edges"]["rejected"] == 1
    assert data["edges"]["failed"] == 0
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


class TestPolicyRoutesOnADegradedStore:
    """#413 — the REST CRUD surface is one of the two writers that laundered
    a damaged access-control file into an empty, *enforced* one.

    ``POST /policies`` on a store that could not read its file rewrote the
    file with what survived (nothing), after which the strict enforcement
    reader parsed a perfectly valid zero-policy file and the gate allowed
    everything. Nothing in that sequence returned an error.
    """

    @staticmethod
    def _damage(tmp_path, text: str):
        path = tmp_path / "stores" / "policies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_get_carries_the_degradation(self, client, tmp_path):
        """``count`` alone under-reports, so a caller reading it as the size
        of the ruleset would be wrong."""
        self._damage(tmp_path, '{"policys": [{"policy_id": "x"}]}')

        resp = client.get("/api/v1/policies")

        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["store_degradation"]["reason"] == "malformed_envelope"
        assert body["store_degradation"]["recovery"].startswith("mv ")

    def test_create_is_refused_and_the_bytes_survive(self, client, tmp_path):
        path = self._damage(tmp_path, '{"policys": [{"policy_id": "x"}]}')
        before = path.read_bytes()

        resp = client.post(
            "/api/v1/policies",
            json={
                "policy_type": "mutation",
                "scope": {"level": "global"},
                "rules": [{"operation": "*", "action": "deny"}],
            },
        )

        # 409, not 503: the file will not repair itself, so "retry later"
        # is the wrong instruction. And not 500 — that body says only
        # "internal server error" and drops the recovery command.
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        # Not ``degraded_store_write``: the same helper answers a read route,
        # where nothing was being written.
        assert detail["code"] == "degraded_store"
        assert detail["store_degradation"]["recovery"].startswith("mv ")
        assert path.read_bytes() == before

    def test_delete_is_refused_rather_than_reporting_not_found(self, client, tmp_path):
        """A 404 from a degraded store is a claim it cannot support."""
        path = self._damage(tmp_path, "{ broken")
        before = path.read_bytes()

        resp = client.delete("/api/v1/policies/whatever")

        assert resp.status_code == 409
        assert path.read_bytes() == before

    def test_get_one_does_not_claim_absence(self, client, tmp_path):
        self._damage(tmp_path, "{ broken")

        resp = client.get("/api/v1/policies/whatever")

        assert resp.status_code == 409

    def test_a_repaired_file_is_picked_up_without_a_restart(self, client, tmp_path):
        """A fix must not need a restart to take effect.

        The store used to be cached for the life of the process, so an
        operator who repaired ``policies.json`` would keep getting 409s
        until someone bounced the API — turning the fix into an outage of
        its own. Repaired here by writing **valid content**, not by deleting
        the file: deleting exercises the absent-file path, which is a
        different branch and the easier one.
        """
        path = self._damage(tmp_path, "{ broken")
        assert client.get("/api/v1/policies").json()["store_degradation"]

        surviving = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="global"),
            rules=[PolicyRule(operation="entity.delete", action="deny")],
        )
        path.write_text(
            json.dumps({"policies": [surviving.model_dump(mode="json")]}),
            encoding="utf-8",
        )
        assert client.get("/api/v1/policies").json()["store_degradation"] is None

        resp = client.post(
            "/api/v1/policies",
            json={
                "policy_type": "mutation",
                "scope": {"level": "global"},
                "rules": [{"operation": "*", "action": "deny"}],
            },
        )
        assert resp.status_code == 200
        listed = client.get("/api/v1/policies").json()
        # The key is always present, ``None`` when clean — an optional key
        # makes every client handle its absence, and absence is the case
        # they would guess wrong about.
        assert listed["store_degradation"] is None
        # And the repaired file's own policy survived the write.
        assert surviving.policy_id in {p["policy_id"] for p in listed["policies"]}

    def test_a_healthy_cached_store_cannot_overwrite_another_writer(
        self, client, tmp_path
    ):
        """The defect that arrives with no corruption at all.

        The store used to be cached for the life of the process and
        invalidated only on *degradation*, so a healthy cached view outlived
        the file. The reference deployment writes this file from two
        processes — a host ``trellis policy add`` and this containerised API
        against one bind-mounted data dir — so:

            GET  /policies        -> caches a store holding [A]
            trellis policy add B  -> file is [A, B]
            POST /policies (C)    -> file becomes [A, C]; B is gone
            GET  /policies        -> 200, no degradation, reports normal

        A declared ``deny`` deleted from disk *and* from Stage 2 by an
        unrelated successful request. Not caching is what fixes it; the
        assertion here is that a second writer's row survives.
        """
        path = tmp_path / "stores" / "policies.json"
        path.parent.mkdir(parents=True, exist_ok=True)

        first = client.post(
            "/api/v1/policies",
            json={
                "policy_type": "mutation",
                "scope": {"level": "global"},
                "rules": [{"operation": "entity.create", "action": "deny"}],
            },
        )
        assert first.status_code == 200
        a_id = first.json()["policy_id"]

        # Another process appends B, exactly as ``trellis policy add`` does.
        from trellis.stores.policy_store import PolicyStore

        other = PolicyStore(path)
        b = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="domain", value="payments"),
            rules=[PolicyRule(operation="*", action="deny")],
        )
        other.add(b)

        second = client.post(
            "/api/v1/policies",
            json={
                "policy_type": "mutation",
                "scope": {"level": "team", "value": "core"},
                "rules": [{"operation": "*", "action": "warn"}],
            },
        )
        assert second.status_code == 200

        listed = {
            p["policy_id"] for p in client.get("/api/v1/policies").json()["policies"]
        }
        assert a_id in listed
        assert b.policy_id in listed, "the other writer's policy was deleted"
        assert second.json()["policy_id"] in listed

    def test_a_stale_write_is_refused_rather_than_silently_winning(
        self, client, tmp_path
    ):
        """The window not-caching cannot close, closed by compare-and-swap.

        ``refuse_if_stale`` is a different failure from a degraded load —
        transient, and retryable — so it carries its own code rather than
        claiming the file is damaged.
        """
        from trellis.stores.policy_store import PolicyStore

        path = tmp_path / "stores" / "policies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        store = PolicyStore(path)
        store.add(
            Policy(
                policy_type=PolicyType.MUTATION,
                scope=PolicyScope(level="global"),
                rules=[PolicyRule(operation="*", action="deny")],
            )
        )

        # A second process writes between this store's load and its save.
        PolicyStore(path).add(
            Policy(
                policy_type=PolicyType.MUTATION,
                scope=PolicyScope(level="team", value="core"),
                rules=[PolicyRule(operation="*", action="warn")],
            )
        )
        before = path.read_bytes()

        with pytest.raises(StaleStoreWriteError):
            store.add(
                Policy(
                    policy_type=PolicyType.MUTATION,
                    scope=PolicyScope(level="domain", value="payments"),
                    rules=[PolicyRule(operation="*", action="deny")],
                )
            )

        assert path.read_bytes() == before

    def test_the_stale_refusal_reaches_the_client_as_a_409(
        self, client, tmp_path, monkeypatch
    ) -> None:
        """The route's stale branch, over HTTP rather than at the store.

        ``test_a_stale_write_is_refused_rather_than_silently_winning``
        drives ``PolicyStore`` directly, so nothing exercised
        ``_refusal_http_error``'s stale arm through a request: deleting
        ``recovery`` from that response body left all 266 targeted tests
        green. ``recovery`` is the entire stated justification for
        answering 409 rather than 500 ("that body says only 'internal
        server error' and drops the recovery command"), so it has to be
        asserted on the response a client actually receives.

        The store is injected already-behind. Now that the route builds one
        per request the real window is microseconds wide, which is the
        point of the compare-and-swap — it is not reproducible by racing.
        """
        from trellis.stores.policy_store import PolicyStore

        path = tmp_path / "stores" / "policies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        behind = PolicyStore(path)
        PolicyStore(path).add(
            Policy(
                policy_type=PolicyType.MUTATION,
                scope=PolicyScope(level="global"),
                rules=[PolicyRule(operation="*", action="deny")],
            )
        )
        landed = path.read_bytes()
        monkeypatch.setattr(
            "trellis_api.routes.policies._get_policy_store", lambda: behind
        )

        resp = client.post(
            "/api/v1/policies",
            json={
                "policy_type": "mutation",
                "scope": {"level": "team", "value": "core"},
                "rules": [{"operation": "*", "action": "warn"}],
            },
        )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        # Its own code: retry, rather than go and look at the file.
        assert detail["code"] == "stale_store_write"
        assert detail["recovery"] == "trellis policy list"
        # Nothing was damaged, so there is no degradation to report.
        assert detail["store_degradation"] is None
        assert path.read_bytes() == landed


class TestAdvisoryGenerateOnADegradedStore:
    """#393/#484 — the surface must not headline ``ok`` over a refusal.

    Two headlines, fixed a release apart. #393 fixed the body's: the
    payload carries ``store_degradation`` either way, so a caller reading
    only ``status`` would record a clean nightly generation against a file
    it could not read. #484 fixed the status line's, which #393 pinned at
    200 and left as the half a plain ``response.ok`` check still read as
    success.

    Every arm is asserted here — clean, degraded, unconfigured, stale — so
    a change that answers one status for all of them cannot pass.
    """

    def test_status_is_degraded_and_the_file_is_untouched(self, client, tmp_path):
        """#484 reverses this test's 200 — deliberately, not by oversight.

        #393 wrote it asserting ``resp.status_code == 200`` and it passed
        for the life of that fix: the claim under test was the *body's*
        headline, and pinning the status it happened to be served under
        was how the second headline stayed unexamined. That reasoning no
        longer holds, because a status line is not incidental packaging —
        it is the field an HTTP caller branches on, and ``response.ok``
        cannot see a ``status`` key. A refusal that renders as success to
        the overwhelmingly common client shape is the #437 class with the
        status line as the channel.

        Everything #393 actually asserted is unchanged and still asserted
        here: the body's ``status``, its ``store_degradation`` and its
        ``advisories_stored`` keep their existing places and values. Only
        the status line moves.
        """
        path = tmp_path / "stores" / "advisories.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"advisories": [ torn', encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        resp = client.post("/api/v1/advisories/generate")

        assert resp.status_code == 409, resp.text
        body = resp.json()
        # The honest half, unchanged and *not* moved under ``detail``.
        assert body["status"] == "degraded", body
        assert body["store_degradation"]["reason"] == "malformed_json"
        assert body["advisories_stored"] == 0
        # ``routes/policies.py``'s vocabulary for this exact condition, so
        # one condition does not get two codes across two surfaces.
        assert body["code"] == "degraded_store", body
        assert path.read_text(encoding="utf-8") == before

    def test_a_clean_store_still_reports_ok(self, client):
        resp = client.post("/api/v1/advisories/generate")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok", body
        assert body["store_degradation"] is None
        # A success carries no refusal code. Without this a mutant that
        # answers ``degraded_store`` unconditionally survives, and the
        # uniformity it would hide is the whole defect.
        assert "code" not in body, body

    def test_an_unconfigured_stores_dir_is_also_a_refusal(
        self, client, tmp_path, monkeypatch
    ):
        """The third arm of the same asymmetry (#484).

        A deployment with no ``stores_dir`` has nowhere to write, so this
        request generated nothing and stored nothing — and said so at 200,
        exactly as the degraded arm did. Included in #484's fix because
        leaving it is the uniformity failure the fix exists to remove: a
        route with one honest refusal arm and one lying one is no more
        trustworthy than one with two.

        409 rather than a status of its own: ``stores_dir`` unset is a
        ``ConfigError`` in all but name, and #483 set that family's status
        at this boundary. The ``code`` is what separates it from the
        degraded and stale refusals, per ``routes/policies.py``.

        Driven through the real ``load_advisory_store``, which returns
        ``None`` only when ``stores_dir`` itself is ``None`` — so the
        registry is unconfigured rather than the loader mocked.
        """
        monkeypatch.setattr(app_module._registry, "_stores_dir", None)

        resp = client.post("/api/v1/advisories/generate")

        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["status"] == "error", body
        assert body["code"] == "stores_dir_unconfigured", body
        # Distinguishable from the degraded refusal, which shares the
        # status: a caller told only "409" cannot act, and the two need
        # different actions (configure a directory vs. repair a file).
        assert body["message"] == "stores_dir not configured", body

    def test_the_degraded_refusal_keeps_the_report_body(self, client, tmp_path):
        """The refusal is answered in place, not raised into a handler.

        Two routes to a non-2xx were available and both lose the body.
        Raising ``DegradedStoreWriteError`` into the global typed handler
        answers **500**, because that error subclasses ``StoreError`` and
        ``middleware._error_status`` remaps only ``ConfigError`` — a
        *differently* wrong status, blaming the server for a file an
        operator has to go and look at. Raising ``HTTPException`` answers
        409 but replaces the body with a ``detail`` envelope. The body was
        the half #393 got right, so neither is acceptable: the status is
        set on the injected ``Response`` and every report field stays
        where it was.
        """
        path = tmp_path / "stores" / "advisories.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"advisories": [ torn', encoding="utf-8")

        resp = client.post("/api/v1/advisories/generate")

        assert resp.status_code == 409, resp.text
        body = resp.json()
        # Not the ``{"detail": ...}`` an HTTPException produces, and not
        # the ``{"code", "message", "request_id"}`` the typed handler does.
        assert "detail" not in body, body
        # The whole AdvisoryReport is still at the top level.
        for field in (
            "advisories_generated",
            "advisories_stored",
            "total_packs",
            "total_feedback",
            "analysis_window_days",
            "store_degradation",
        ):
            assert field in body, (field, body)

    def test_listing_still_works_on_a_degraded_store(self, client, tmp_path):
        """Reads stay lenient — a corrupt file must not 500 the read path."""
        path = tmp_path / "stores" / "advisories.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"advisories": [ torn', encoding="utf-8")

        resp = client.get("/api/v1/advisories")

        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 0

    def test_a_stale_write_is_409_rather_than_a_hidden_500(
        self, client, tmp_path, monkeypatch
    ):
        """#438 — this container and the host CLI write the same file.

        409 rather than the 500 ``unhandled_exception_handler`` would
        produce: that handler's body says only "internal server error", so
        the caller would learn nothing about a refusal that is entirely
        theirs to retry. Driven by making the generator raise, because
        generation only writes when the window yields advisories — the
        claim under test is the route's, and the store's own guard is
        pinned in ``tests/unit/stores/test_advisory_store.py``.
        """
        from trellis.errors import StaleStoreWriteError
        from trellis_api.routes import admin as admin_routes

        message = "Refusing to write the Trellis advisory file: it changed."

        path = tmp_path / "stores" / "advisories.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"advisories": []}', encoding="utf-8")

        class _RefusingGenerator:
            def __init__(self, *args, **kwargs):
                pass

            def generate(self, *, days=30):
                raise StaleStoreWriteError(
                    message,
                    store="advisory",
                    path=str(path),
                    recovery="trellis analyze advisory-effectiveness --dry-run",
                )

        monkeypatch.setattr(admin_routes, "AdvisoryGenerator", _RefusingGenerator)

        resp = client.post("/api/v1/advisories/generate")

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "stale_store_write"
        assert detail["path"] == str(path)


class TestVectorsResetStatusLine:
    """#506 — ``POST /vectors/reset`` answered 200 whatever happened.

    Three arms, three claims, and until this fix the status line made the
    same claim for all of them. An unconfigured store did nothing and said
    200. A reset that was attempted and *crashed* said 200 with
    ``status: "error"`` in the body — the #437 class with the status line
    as the channel, on a destructive route. And the success arm, found
    while fixing the other two, said **500** on the default SQLite backend
    after a reset that had actually worked.

    Every arm is asserted here, and the two refusals are asserted to be
    distinguishable from each other, so a change that answers one status
    for all of them cannot pass.
    """

    def test_a_successful_reset_answers_200(self, client):
        """The default backend is SQLite, and it used to answer 500 here.

        ``_dimensions`` was defined on ``PgVectorStore``,
        ``ArcadeDBVectorStore`` and ``Neo4jVectorStore`` and **not** on
        ``SQLiteVectorStore``; the route read it in the ``else`` block,
        outside the ``try``, so on a default deployment the table was
        dropped and recreated and the caller was then told ``500
        internal_error`` by the app's catch-all. Asserting the message
        too, not just the status: a mutant that reports the wrong
        backend's story is a wrong contract even when the status is right.

        The message is SQLite's own answer now rather than the absence of
        an attribute: it declares ``dimensions`` as ``None`` because it
        keeps a width per row and pins none (#512).
        """
        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok", body
        assert body["message"] == "Recreated (backend declares no fixed dimensionality)"
        # A success carries no refusal code. Without this a mutant that
        # emits ``vector_reset_failed`` unconditionally survives, and the
        # uniformity it would hide is the whole defect.
        assert "code" not in body, body

    def test_a_successful_reset_really_recreated_the_table(self, client):
        """The 200 is a claim about the store, so check the store.

        Asserting only the status line would let a mutant that skips the
        drop-and-recreate entirely and returns the same dict pass — which
        is the class of defect this route exists to avoid.
        """
        store = app_module._registry.knowledge.vector_store
        store.upsert("doc:1", [0.1, 0.2, 0.3], metadata={})
        assert store.count() == 1

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 200, resp.text
        assert store.count() == 0

    def test_an_unconfigured_vector_store_is_a_refusal_not_a_success(
        self, client, monkeypatch
    ):
        """409, the answer #484/#505 settled on for the sibling route.

        Nothing was dropped and nothing was recreated, so this is the same
        condition class as ``stores_dir`` unset — a ``ConfigError`` in all
        but name, which is the family #483 put at 409 for this boundary.
        The route reaches the arm through ``getattr(..., None)``, so the
        provocation is removing the attribute from the plane class rather
        than mocking the route's own lookup.
        """
        from trellis.stores.registry import _KnowledgePlane

        monkeypatch.delattr(_KnowledgePlane, "vector_store")

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["status"] == "error", body
        assert body["code"] == "vector_store_unconfigured", body
        assert body["message"] == "Vector store not configured", body

    def test_a_failed_reset_answers_500_not_200(self, client):
        """The arm the issue called the worse one, provoked for real.

        No mock: the vectors database file is replaced with bytes that are
        not a SQLite database, so the connection the route's worker thread
        opens raises ``sqlite3.DatabaseError: file is not a database``
        from inside the ``try``. The fixture is verified below to provoke
        exactly that, rather than some other exception the same ``except
        Exception`` would also swallow.

        **500, not the 409 the sibling arm answers, and deliberately not
        by symmetry.** 409 says a precondition on the caller's side is
        unmet and a reshaped request can succeed; a store that fell over
        mid-reset is not the caller's to fix, and it is what
        ``middleware._error_status`` already answers for a ``StoreError``
        reaching this boundary. Answering 409 here would give one
        condition two statuses across two surfaces — the uniformity
        failure #484 existed to remove, reintroduced in the name of
        matching the arm next door.
        """
        store = app_module._registry.knowledge.vector_store
        db_path = Path(str(store._db_path))
        db_path.write_bytes(b"not a sqlite database" * 32)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        # Pin the provocation: a fresh connection — which is what the
        # route's worker thread opens — fails, and fails as a
        # ``DatabaseError`` from the sqlite driver.
        with pytest.raises(sqlite3.DatabaseError):
            sqlite3.connect(str(db_path)).execute("PRAGMA journal_mode").fetchone()

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["status"] == "error", body
        assert body["code"] == "vector_reset_failed", body
        # The backend's own words, which is the half the catch is kept
        # for: the app's catch-all would answer "internal server error"
        # and nothing else.
        assert body["message"] == "file is not a database", body

    def test_a_failure_after_the_drop_is_also_500(self, client):
        """The half the spec's 500 description promises: the table is gone.

        The arm above fails before the ``DROP`` runs. This one fails
        after, which is the case that makes 500 rather than 409 matter —
        the request was not a no-op, the vectors table has been dropped
        and not recreated, and no retry of the same request by the same
        caller repairs that. ``_init_schema`` is patched on the instance
        (the recreate cannot be made to fail for real on a healthy file)
        and it raises the driver's own ``OperationalError``, not a bare
        ``Exception``, so the arm is provoked by the type it would see in
        production.
        """
        store = app_module._registry.knowledge.vector_store
        store.upsert("doc:1", [0.1, 0.2, 0.3], metadata={})

        message = "disk I/O error"

        def _boom() -> None:
            raise sqlite3.OperationalError(message)

        store._init_schema = _boom  # type: ignore[method-assign]

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["code"] == "vector_reset_failed", body
        assert body["message"] == "disk I/O error", body
        # The destructive half really happened — this is why the status is
        # not a 409 "nothing was done".
        del store._init_schema
        with pytest.raises(sqlite3.OperationalError):
            store.count()

    def test_a_backend_error_echoing_a_dsn_is_suppressed(self, client):
        """The word *sanitized* is now in the published 500 description.

        The ``except`` is kept for the **body**, and the body's only guard
        is ``sanitize_error_message`` (#206) — a driver that fell over
        routinely echoes the DSN it could not reach, and an API response
        body is exactly the artifact that guard was written for. Nothing
        pinned it: dropping the call left all 311 API tests green, while
        the two sibling leak guards at this boundary — ``/readyz``'s probe
        error and ``trellis_error_handler``'s ``path`` — each have a test
        of this shape. A guard whose removal is invisible is one a
        refactor removes.
        """
        store = app_module._registry.knowledge.vector_store

        def _boom() -> None:
            msg = (
                "connection failed: could not connect to "
                '"postgresql://trellis:s3cretpw@db.internal:5432/prod"'
            )
            raise sqlite3.OperationalError(msg)

        store._init_schema = _boom  # type: ignore[method-assign]

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["code"] == "vector_reset_failed", body
        assert body["message"] == SUPPRESSED_MARKER, body
        # Not just the field — the credential is nowhere in the response.
        assert "s3cretpw" not in resp.text

    def test_a_failed_reset_is_logged_on_the_operator_channel(self, client):
        """The other half of why the ``except`` is kept, also unpinned.

        Catching is justified by two things: the sanitized message in the
        body, and ``logger.exception`` still recording the traceback for
        an operator. Downgrading that call to ``logger.debug`` left the
        API suite green — and this repo has already recorded (#404) that a
        ``logger.debug`` fires under **no** shipped log configuration, so
        the mutant silently turns the only operator channel for a
        half-completed destructive reset into a no-op. Asserted at the
        level and with ``exc_info``, which is what separates
        ``logger.exception`` from a bare log line.
        """
        store = app_module._registry.knowledge.vector_store
        message = "disk I/O error"

        def _boom() -> None:
            raise sqlite3.OperationalError(message)

        store._init_schema = _boom  # type: ignore[method-assign]

        with capture_logs() as logs:
            resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 500, resp.text
        entry = next(
            (e for e in logs if e.get("event") == "vectors_reset_failed"), None
        )
        assert entry is not None, logs
        assert entry["log_level"] == "error", entry
        assert "exc_info" in entry, entry

    def test_a_backend_that_declares_a_width_reports_it(self, client):
        """The other side of the ``dims`` conditional, which nothing reached.

        Every backend in the default test selection is SQLite and SQLite
        declares ``None``, so the branch a pgvector deployment actually
        takes was unexercised — dropping the ``D`` from the message left
        the suite green.

        A subclass declaring 1536 rather than a monkeypatch: before #512
        the route read ``getattr(store, "_dimensions", None)`` and setting
        that attribute on a live ``SQLiteVectorStore`` instance was enough
        to steer it, which is the defect. ``dimensions`` is a property on
        the *class* now, so the only way to make a backend report a width
        is for the backend to declare one — which is the fix, restated as
        a test that could not be written the old way.
        """
        registry = app_module._registry
        store = _FixedWidthVectorStore()
        registry._cache["vector"] = store

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok", body
        assert body["message"] == "Recreated with 1536D", body
        assert "code" not in body, body
        assert store.reset_calls == 1

    def test_a_declaration_that_raises_cannot_follow_a_completed_reset(self, client):
        """Why the width is read *before* the reset, made falsifiable.

        The width is a cosmetic field in a 200 body, and reading it after
        the drop-and-recreate would put a property call between a
        completed destructive operation and the report of it — #506's
        defect (a successful reset answering 500) with a new attribute in
        the hole. The contract forbids a declaration that raises, so this
        store is out of contract; what the ordering buys is that an
        out-of-contract backend fails a request that **changed nothing**
        rather than one that emptied the store and then lied about it.

        Moving the read into the ``else`` block is behaviourally identical
        for every shipped backend, so the **store** assertion is the whole
        test: the caller sees the same failure either way. The raise
        escapes this route's own ``except`` (it is outside the ``try``),
        so the app's catch-all answers it — and ``TestClient`` re-raises
        server exceptions after the handler runs, which is why this is a
        ``pytest.raises`` rather than a 500 assertion.
        """

        class _AngryDeclarationStore(_ResettableVectorStore):
            @property
            def dimensions(self) -> int | None:
                msg = "declaration unavailable"
                raise RuntimeError(msg)

        registry = app_module._registry
        store = _AngryDeclarationStore()
        registry._cache["vector"] = store

        with pytest.raises(RuntimeError, match="declaration unavailable"):
            client.post("/api/v1/vectors/reset")

        assert store.reset_calls == 0

    def test_no_refusal_is_wrapped_in_a_detail_envelope(self, client, monkeypatch):
        """Pins the docstring's contract for *every* arm it speaks for.

        The endpoint docstring is what ``scripts/generate_openapi.py``
        publishes as the OpenAPI description, and it says **all three**
        refusals name themselves in a ``code`` at the **top level**.
        #505's review gate caught exactly this claim being false for one
        of three arms, because that arm raised ``HTTPException`` and so
        put its code at ``body["detail"]["code"]`` — a generated client
        would look for a key that is not there. #511 added a third arm to
        this route and so a third chance to make the same claim falsely.
        Asserted here per arm, not asserted once and assumed to
        generalise.
        """
        from trellis.stores.registry import _KnowledgePlane

        registry = app_module._registry
        store = registry.knowledge.vector_store

        # Arm three first: it swaps the cached store, and the two arms
        # below need the real one back.
        registry._cache["vector"] = _UnresettableVectorStore()
        unsupported = client.post("/api/v1/vectors/reset")
        assert unsupported.status_code == 409, unsupported.text
        assert "detail" not in unsupported.json(), unsupported.text
        assert "code" in unsupported.json(), unsupported.text
        registry._cache["vector"] = store

        db_path = Path(str(store._db_path))
        db_path.write_bytes(b"not a sqlite database" * 32)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        failed = client.post("/api/v1/vectors/reset")
        assert failed.status_code == 500, failed.text
        assert "detail" not in failed.json(), failed.text
        assert "code" in failed.json(), failed.text

        monkeypatch.delattr(_KnowledgePlane, "vector_store")
        unconfigured = client.post("/api/v1/vectors/reset")
        assert unconfigured.status_code == 409, unconfigured.text
        assert "detail" not in unconfigured.json(), unconfigured.text
        assert "code" in unconfigured.json(), unconfigured.text

        # And they are told apart by more than the status: a caller given
        # only "something went wrong" cannot choose between configuring a
        # store, going to look at one, and reaching for the backend's own
        # tooling. The two that *share* a status are the pair that most
        # needs distinguishing, and a mutant that emits one code for both
        # would otherwise pass every assertion above.
        codes = [
            failed.json()["code"],
            unconfigured.json()["code"],
            unsupported.json()["code"],
        ]
        assert len(set(codes)) == 3, codes

    def test_the_spec_declares_every_refusal(self):
        """``openapi-check`` was green against a spec with only a 200.

        #484 found the same thing next door: a committed spec that never
        described the behaviour, so regenerating it was part of the fix
        rather than a follow-up. Read off the app the generator reads, so
        this fails if the ``responses=`` argument is dropped even when the
        committed YAML still happens to carry the declaration.
        """
        from trellis_api.routes import admin as admin_routes

        route = next(
            r
            for r in admin_routes.router.routes
            if getattr(r, "path", None) == "/vectors/reset"
        )
        assert set(route.responses) == {409, 500}, route.responses
        for status in (409, 500):
            assert route.responses[status]["description"], route.responses

        # Three refusals under two statuses, so the status set alone no
        # longer says every arm is declared. #511's arm shares 409 with
        # the unconfigured one and is distinguished by ``code``, which
        # means the *description* is the only place the spec can carry it
        # — and a reader of the generated YAML has nothing else to go on.
        refusal_409 = route.responses[409]["description"]
        for code in ("vector_store_unconfigured", "vector_reset_unsupported_backend"):
            assert code in refusal_409, refusal_409
        assert "vector_reset_failed" in route.responses[500]["description"]

        # #512 widened the unsupported arm: it also answers for an object
        # that is not a vector store at all, which has a different message
        # and no code of its own. The description is the only place the
        # spec can carry that, for the same reason the code split is —
        # this arm shares its status with the unconfigured one.
        assert "interface" in refusal_409, refusal_409


class _UnresettableVectorStore(VectorStore):
    """A ``VectorStore`` shaped like the blessed substrate: no reset.

    ``ArcadeDBVectorStore`` and ``Neo4jVectorStore`` keep no storage of
    their own — vectors are properties on the graph store's ``(:Node)``
    rows — so neither implements ``reset_storage`` and both decline. It
    declares ``None`` for its width, which is a *separate* answer: the
    shipped graph-node backends actually pin one, and this fixture says
    ``None`` so that a route reading the width of a store it refused
    would be reading the wrong thing rather than coincidentally the right
    one.

    A real ABC subclass rather than a ``MagicMock`` on purpose: a
    ``MagicMock`` answers *every* attribute lookup, so it would report
    support and exercise the supported path while claiming to test the
    unsupported one. Every inherited operation raises, so an arm that
    reaches past the refusal fails loudly instead of quietly returning a
    mock.
    """

    def __init__(self) -> None:
        self.reset_calls = 0

    @property
    def dimensions(self) -> int | None:
        return None

    def _unreachable(self, *args, **kwargs):
        msg = "the unsupported arm must not touch the store"
        raise AssertionError(msg)

    upsert = upsert_bulk = query = get = delete = count = close = _unreachable


class _ResettableVectorStore(_UnresettableVectorStore):
    """Declares support by implementing it, and records the call."""

    def reset_storage(self) -> None:
        self.reset_calls += 1


class _FixedWidthVectorStore(_ResettableVectorStore):
    """A backend that pins a width, which no default-selection backend does."""

    @property
    def dimensions(self) -> int | None:
        return 1536


class _HandleRichButUnresettableStore(_UnresettableVectorStore):
    """Carries both private handles the pre-#512 probe looked for.

    ``_pool`` and ``_conn`` are present and it still does not implement
    ``reset_storage``, so the declared answer and the probed answer are
    **opposites** for this store. Nothing shipped has this shape, and
    that is the point: the only way to tell a declaration apart from an
    inference behaviourally is a store the two disagree about.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pool = object()
        self._conn = object()


class _NotAVectorStore:
    """Duck-typed like the old probe's happy path, outside the ABC entirely.

    The registry instantiates whatever class a config names. Before #512
    this object would have been reset through its private attributes; the
    route now depends on the abstraction, so it is refused — and refused
    with the 409 that says nothing was touched, rather than an
    ``AttributeError`` escaping as a 500 the moment the ABC is asked
    something this object cannot answer.
    """

    def __init__(self) -> None:
        self._pool = object()
        self._conn = object()
        self.init_schema_calls = 0

    def _init_schema(self) -> None:
        self.init_schema_calls += 1


class TestVectorsResetUnsupportedBackend:
    """#511/#512 — the route has never worked on the blessed substrate.

    ``ArcadeDBVectorStore`` and ``Neo4jVectorStore`` have neither ``_conn``
    nor ``_pool``, so ``POST /vectors/reset`` reached for ``_conn`` and
    died on ``AttributeError``. #506 made that honest — ``500
    vector_reset_failed`` with ``'ArcadeDBVectorStore' object has no
    attribute '_conn'`` — and honest was the improvement. It is still a
    poor refusal: a leaked private attribute name is not an instruction,
    and a 5xx describes a *permanent property of the backend* as a
    transient server failure, which invites a retry that can never
    succeed.

    #511 answered 409 after *probing* for those private attributes. #512
    replaced the probe: a backend implements
    ``VectorStore.reset_storage`` or it does not, ``supports_reset()`` is
    derived from that, and there is no dispatch left for a second check
    to disagree with.
    """

    def test_a_backend_that_cannot_be_reset_is_refused_not_crashed(self, client):
        """409 with a code of its own, and no leaked attribute name.

        The status is the caller-facing half: 4xx says "this request
        cannot be satisfied here", where the 500 said "the server broke,
        try again". The body is the operator-facing half, and the
        regression it guards is specifically the *old* message — a mutant
        that keeps the new status but re-raises the ``AttributeError``
        text is caught by the ``_conn`` assertion, not by the status one.
        """
        registry = app_module._registry
        store = _UnresettableVectorStore()
        registry._cache["vector"] = store

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["status"] == "error", body
        assert body["code"] == "vector_reset_unsupported_backend", body
        # Actionable: it names the backend, states that nothing changed,
        # and gives the operator somewhere to go. The recovery is two
        # clauses and both are pinned — asserting only ``reindex-vectors``
        # let a mutant that deleted "rebuild the index with the backend's
        # own tooling" survive, and repopulating an index nobody rebuilt
        # is not a recovery.
        assert "_UnresettableVectorStore" in body["message"], body
        assert "keeps no `vectors` table" in body["message"], body
        assert "Nothing was changed" in body["message"], body
        assert "Rebuild the backend's vector index" in body["message"], body
        assert "trellis admin reindex-vectors --force" in body["message"], body
        # The #510 message, which said nothing an operator could act on.
        assert "_conn" not in resp.text, resp.text

    def test_the_unsupported_arm_touches_the_store_not_at_all(self, client):
        """The 409's promise is that nothing happened, so check the store.

        ``_UnresettableVectorStore`` raises from every ``VectorStore``
        operation and counts reset calls, so a mutant that runs the
        recreate before deciding the backend is unsupported — the ordering
        that would make the "nothing was changed" sentence a lie — fails
        here rather than passing on the status line.
        """
        registry = app_module._registry
        store = _UnresettableVectorStore()
        registry._cache["vector"] = store

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 409, resp.text
        assert store.reset_calls == 0

    def test_an_object_that_is_not_a_vector_store_is_refused_not_crashed(self, client):
        """The route depends on the abstraction, so it checks for it.

        ``StoreRegistry`` instantiates whatever class a config names, and
        the pre-#512 route drove any object carrying the right private
        attributes. Asking a non-``VectorStore`` for ``supports_reset()``
        would raise ``AttributeError`` above the ``try`` and escape as a
        500 ``internal_error`` — the shape #506 removed from this route,
        reintroduced by the fix for #511's sibling. It is refused with the
        arm whose promise is that nothing was touched, and the store is
        checked, not just the status.

        The message says what is true of *this* object and not what is
        true of ArcadeDB. "Keeps no `vectors` table" is a fact the ABC's
        declaration entitles the route to state about a ``VectorStore``
        that declined; said about an object that never implemented the
        interface it is invented — which is #512's own failure mode,
        committed in the sentence that fixes #512. Both halves are
        asserted: the true one present, the borrowed one absent.
        """
        registry = app_module._registry
        store = _NotAVectorStore()
        registry._cache["vector"] = store

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "vector_reset_unsupported_backend", body
        assert "_NotAVectorStore" in body["message"], body
        assert "does not implement" in body["message"], body
        assert "`VectorStore` interface" in body["message"], body
        assert "keeps no `vectors` table" not in body["message"], body
        # The recovery still rides along: the two conditions differ in
        # what happened, not in what the operator does next.
        assert "Nothing was changed" in body["message"], body
        assert "trellis admin reindex-vectors --force" in body["message"], body
        assert store.init_schema_calls == 0

    def test_the_refusal_is_decided_by_capability_not_by_class_name(self):
        """The detection rule, attacked directly with two liars.

        A roster of backend class names is the obvious implementation and
        the one that rots: it goes wrong the moment a fifth backend lands,
        and silently. Both fakes below defeat a name-based rule and
        neither defeats a capability check — one is *named*
        ``SQLiteVectorStore`` and cannot be reset, the other is *named*
        ``ArcadeDBVectorStore`` and can. Asserting only the first would
        leave a rule of the form "unsupported unless named SQLite/Pg"
        alive.
        """
        from trellis_api.routes import admin as admin_routes

        liar_named_supported = type(
            "SQLiteVectorStore", (_UnresettableVectorStore,), {}
        )
        liar_named_unsupported = type(
            "ArcadeDBVectorStore", (_ResettableVectorStore,), {}
        )

        assert admin_routes._vector_reset_refusal(liar_named_supported()) is not None
        assert admin_routes._vector_reset_refusal(liar_named_unsupported()) is None

    def test_every_shipped_vector_backend_is_classified(self):
        """The roster lives in the *test*, derived from the registry.

        A roster in ``admin.py`` would rot unnoticed; a roster here fails
        the moment ``_BUILTIN_BACKENDS`` grows a vector backend nobody has
        classified, which is the notification that a new backend needs an
        answer to "can this route reset it?".

        Since #512 the answer is a classmethod on the type, so this needs
        no instance at all: nothing is constructed, no driver connects and
        no socket opens. Before, it had to ``object.__new__`` each backend
        and accept that the *handle* reported for pgvector was not the one
        production takes, because ``_pool`` is set in ``__init__``.
        """
        from trellis.stores.registry import _BUILTIN_BACKENDS

        expected = {
            "sqlite": True,
            "pgvector": True,
            "neo4j": False,
            "arcadedb": False,
        }
        shipped = _BUILTIN_BACKENDS["knowledge"]["vector"]
        assert set(shipped) == set(expected), sorted(shipped)

        probed: dict[str, bool] = {}
        for name, (module, cls_name) in shipped.items():
            try:
                cls = getattr(importlib.import_module(module), cls_name)
            except ImportError:
                continue  # optional driver extra not installed
            probed[name] = cls.supports_reset()

        assert probed == {k: v for k, v in expected.items() if k in probed}, probed
        # Floor against a vacuous pass: the whole claim is about these
        # two, and both import with no optional extra, so a run that
        # skipped them proves nothing.
        assert probed.get("neo4j") is False, probed
        assert probed.get("arcadedb") is False, probed

    def test_a_reset_that_raises_is_a_failure_not_an_unsupported_backend(self, client):
        """Cannot and would-not-this-time are different answers.

        The refusal decides "can the reset *begin*", nothing more. A
        backend that implements ``reset_storage`` and then fails inside it
        has *attempted* a reset — the table may be gone — which is the
        500's claim and not the 409's. Widening the refusal to catch that
        would move a genuinely destructive failure into the arm whose
        whole promise is that nothing was touched.

        The failure is the driver's own ``sqlite3.DatabaseError``, not a
        bare ``Exception``, so the arm is provoked by the type it would
        see in production.
        """
        message = "file is not a database"

        class _AngryResetStore(_ResettableVectorStore):
            def reset_storage(self) -> None:
                raise sqlite3.DatabaseError(message)

        registry = app_module._registry
        registry._cache["vector"] = _AngryResetStore()

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 500, resp.text
        body = resp.json()
        assert body["code"] == "vector_reset_failed", body
        assert body["message"] == message, body

    def test_the_route_runs_the_backends_own_reset_and_no_sql_of_its_own(self, client):
        """The absorbed half of #512, and the pgvector arm's replacement.

        The route used to hold two SQL bodies — ``store._conn.execute(...)``
        for SQLite and a pooled ``cursor.execute(...)`` for pgvector —
        picked by a dispatch on the probe's answer. Nothing in the default
        selection reached the pooled one, so on ``8a21d3f`` replacing the
        whole pooled arm with the SQLite one left the API suite green.
        There is now one call, the backend owns its own SQL, and the SQL
        is covered where it lives: ``VectorStoreContractTests`` runs the
        reset case against SQLite by default and against pgvector in
        ``live-infra``.

        A ``VectorStore`` that raises from every other operation, so a
        mutant reaching past ``reset_storage`` for a handle of its own
        fails loudly rather than quietly.
        """
        registry = app_module._registry
        store = _ResettableVectorStore()
        registry._cache["vector"] = store

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok", resp.text
        assert store.reset_calls == 1

    def test_the_refusal_and_the_work_read_one_fact(self, client):
        """The "decided once" claim, made falsifiable against the old rule.

        #520's load-bearing sentence was that the refusal and the dispatch
        read the same value so they cannot disagree — and a mutant
        reintroducing a second, independent ``hasattr`` check survived its
        whole 7,388-test suite, because the two spellings agree for every
        shipped backend. #512 removes the second value rather than testing
        around it: ``supports_reset()`` is derived from the
        ``reset_storage`` override and the route calls that same method,
        so there is nothing left to re-ask.

        What makes that behaviourally checkable is a store the *old* rule
        and the new one answer **oppositely**. This one carries both
        private handles the probe looked for and implements no reset, so a
        route reverted to the probe accepts it, reaches for ``_conn`` and
        500s; the declaration refuses it untouched.
        """
        registry = app_module._registry
        store = _HandleRichButUnresettableStore()
        registry._cache["vector"] = store

        # The premise, asserted rather than assumed: the two rules
        # disagree about this store. Without this the assertions below
        # could pass for the wrong reason if the fixture stopped being a
        # disagreement.
        assert hasattr(store, "_pool")
        assert hasattr(store, "_conn")
        assert type(store).supports_reset() is False

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "vector_reset_unsupported_backend", resp.text
        assert store.reset_calls == 0

    def test_the_declaration_wins_over_the_absence_of_a_handle_too(self, client):
        """The same disagreement, pointed the other way.

        ``_ResettableVectorStore`` has neither ``_pool`` nor ``_conn`` and
        implements ``reset_storage``, so the old probe refuses it and the
        declaration accepts it. Asserting only the direction above would
        leave a rule of the form "refuse unless it has a handle *and*
        declares" alive — which is two facts again, and passes every test
        that only ever removes support.
        """
        registry = app_module._registry
        store = _ResettableVectorStore()
        registry._cache["vector"] = store

        assert not hasattr(store, "_pool")
        assert not hasattr(store, "_conn")

        resp = client.post("/api/v1/vectors/reset")

        assert resp.status_code == 200, resp.text
        assert store.reset_calls == 1
