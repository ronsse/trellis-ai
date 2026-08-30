"""Tests for the REST API routes."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.errors import StaleStoreWriteError
from trellis.schemas.enums import PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
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


class TestAdvisoryGenerateOnADegradedStore:
    """#393 — the REST admin surface must not headline ``ok`` over a refusal.

    The payload carries ``store_degradation`` either way; a caller that
    reads only ``status`` would otherwise record a clean nightly generation
    against a file it could not read.
    """

    def test_status_is_degraded_and_the_file_is_untouched(self, client, tmp_path):
        path = tmp_path / "stores" / "advisories.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"advisories": [ torn', encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        resp = client.post("/api/v1/advisories/generate")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "degraded", body
        assert body["store_degradation"]["reason"] == "malformed_json"
        assert body["advisories_stored"] == 0
        assert path.read_text(encoding="utf-8") == before

    def test_a_clean_store_still_reports_ok(self, client):
        resp = client.post("/api/v1/advisories/generate")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok", body
        assert body["store_degradation"] is None

    def test_listing_still_works_on_a_degraded_store(self, client, tmp_path):
        """Reads stay lenient — a corrupt file must not 500 the read path."""
        path = tmp_path / "stores" / "advisories.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"advisories": [ torn', encoding="utf-8")

        resp = client.get("/api/v1/advisories")

        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 0
