"""End-to-end integration tests against a live Neo4j instance.

These tests exercise the full vertical slice — extraction → mutation
pipeline → graph store → retrieval → event log — wired against a real
Neo4j backend (graph + vector) with SQLite under the rest of the planes.
The unit suites validate each store in isolation; this file proves the
seams between them hold up against a real database.

Skipped cleanly when ``TRELLIS_TEST_NEO4J_URI`` is unset.

Run locally:

    set -a && source .env && set +a   # bash
    pytest tests/integration/ -v
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

pytest.importorskip("neo4j")

from trellis.extract.commands import result_to_batch
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.json_rules import (
    EdgeRule,
    EntityRule,
    ExtractionRuleBundle,
    JSONRulesExtractor,
)
from trellis.extract.registry import ExtractorRegistry
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandStatus,
    Operation,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GraphSearch, SemanticSearch
from trellis.schemas.pack import PackBudget
from trellis.stores.base.event_log import EventType

# Gate the whole module on the live-Neo4j env var so a missing URI skips
# cleanly instead of erroring inside the registry fixture. Mirrors the
# unit-suite pattern in tests/unit/stores/test_neo4j_*.py.
pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(
        not os.environ.get("TRELLIS_TEST_NEO4J_URI"),
        reason="TRELLIS_TEST_NEO4J_URI not set",
    ),
]


# ---------------------------------------------------------------------------
# 1. ENTITY_CREATE / LINK_CREATE through the mutation pipeline land in Neo4j
# ---------------------------------------------------------------------------


def test_entity_create_lands_in_neo4j(registry: Any, executor: Any) -> None:
    """A single ENTITY_CREATE command should produce a row in the Neo4j graph."""
    command = Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_id": "svc-auth",
            "entity_type": "service",
            "name": "auth-api",
            "properties": {"team": "platform"},
        },
        requested_by="integration:entity_create",
    )

    result = executor.execute(command)

    assert result.status is CommandStatus.SUCCESS
    assert result.created_id == "svc-auth"

    node = registry.knowledge.graph_store.get_node("svc-auth")
    assert node is not None
    assert node["node_type"] == "service"
    assert node["properties"]["name"] == "auth-api"
    assert node["properties"]["team"] == "platform"


def test_link_create_lands_in_neo4j(registry: Any, executor: Any) -> None:
    """LINK_CREATE following two ENTITY_CREATEs builds a real edge in Neo4j."""
    batch = CommandBatch(
        commands=[
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": "svc-a",
                    "entity_type": "service",
                    "name": "a",
                    "properties": {},
                },
                requested_by="integration:link",
            ),
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": "svc-b",
                    "entity_type": "service",
                    "name": "b",
                    "properties": {},
                },
                requested_by="integration:link",
            ),
            Command(
                operation=Operation.LINK_CREATE,
                args={
                    "source_id": "svc-a",
                    "target_id": "svc-b",
                    "edge_kind": "depends_on",
                    "properties": {"weight": 1.0},
                },
                target_id="svc-a",
                requested_by="integration:link",
            ),
        ],
        strategy=BatchStrategy.STOP_ON_ERROR,
        requested_by="integration:link",
    )

    results = executor.execute_batch(batch)
    assert all(r.status is CommandStatus.SUCCESS for r in results), results

    edges = registry.knowledge.graph_store.get_edges("svc-a", direction="outgoing")
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "depends_on"
    assert edges[0]["target_id"] == "svc-b"
    assert edges[0]["properties"]["weight"] == 1.0


def test_mutation_emits_audit_events(registry: Any, executor: Any) -> None:
    """ENTITY_CREATE should emit MUTATION_EXECUTED + ENTITY_CREATED to the log."""
    command = Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_id": "svc-audit",
            "entity_type": "service",
            "name": "audited",
            "properties": {},
        },
        requested_by="integration:audit",
    )
    result = executor.execute(command)
    assert result.status is CommandStatus.SUCCESS

    event_log = registry.operational.event_log

    mutation_events = event_log.get_events(event_type=EventType.MUTATION_EXECUTED)
    assert any(
        e.payload.get("command_id") == command.command_id for e in mutation_events
    ), "MutationExecutor must record the executed command in the event log"

    entity_events = event_log.get_events(event_type=EventType.ENTITY_CREATED)
    assert any(e.entity_id == "svc-audit" for e in entity_events), (
        "ENTITY_CREATE handler must emit ENTITY_CREATED keyed on the new node id"
    )


# ---------------------------------------------------------------------------
# 2. JSONRulesExtractor → drafts → batch → executor → graph rows
# ---------------------------------------------------------------------------


def test_json_extractor_e2e_into_neo4j(registry: Any, executor: Any) -> None:
    """A small declarative bundle should produce Neo4j rows + edges via the pipeline.

    Rules describe a tiny "platform" topology with two services and a
    depends-on relationship encoded as a field reference. The whole stack
    runs unmodified against the live database — extractor is pure,
    drafts route through ``result_to_batch``, executor lands writes in
    Neo4j, telemetry hits the event log.
    """
    rules = ExtractionRuleBundle(
        entity_rules=[
            EntityRule(
                name="service",
                path=["services", "*"],
                entity_type="service",
                id_field="id",
                name_field="name",
                property_fields={"team": "team", "tier": "tier"},
            ),
        ],
        edge_rules=[
            EdgeRule(
                name="depends_on",
                source_rule="service",
                target_rule="service",
                edge_kind="depends_on",
                source_field="depends_on",
            ),
        ],
    )
    extractor = JSONRulesExtractor(
        "integration",
        rules,
        supported_sources=["integration"],
    )

    raw = {
        "services": [
            {
                "id": "svc-orders",
                "name": "orders-api",
                "team": "commerce",
                "tier": "edge",
                "depends_on": ["svc-payments"],
            },
            {
                "id": "svc-payments",
                "name": "payments-api",
                "team": "commerce",
                "tier": "core",
            },
        ],
    }

    ext_registry = ExtractorRegistry()
    ext_registry.register(extractor)
    dispatcher = ExtractionDispatcher(
        ext_registry,
        event_log=registry.operational.event_log,
    )
    result = asyncio.run(
        dispatcher.dispatch(raw, source_hint="integration"),
    )
    assert len(result.entities) == 2
    assert len(result.edges) == 1

    batch = result_to_batch(result, requested_by="integration:json-rules")
    results = executor.execute_batch(batch)
    succeeded = sum(1 for r in results if r.status is CommandStatus.SUCCESS)
    assert succeeded == len(results), [
        (r.operation, r.status, r.message) for r in results
    ]

    graph = registry.knowledge.graph_store
    orders = graph.get_node("svc-orders")
    payments = graph.get_node("svc-payments")
    assert orders is not None
    assert payments is not None
    assert orders["properties"]["team"] == "commerce"
    assert payments["properties"]["tier"] == "core"

    edges = graph.get_edges("svc-orders", direction="outgoing")
    assert [(e["edge_type"], e["target_id"]) for e in edges] == [
        ("depends_on", "svc-payments"),
    ]


# ---------------------------------------------------------------------------
# 3. PackBuilder against Neo4j-backed graph store
# ---------------------------------------------------------------------------


def test_pack_builder_against_neo4j_graph(registry: Any, executor: Any) -> None:
    """PackBuilder + GraphSearch should retrieve nodes from a Neo4j graph store."""
    for entity_id, name, description in [
        ("svc-search", "search-api", "Full-text search service for the catalog"),
        ("svc-catalog", "catalog-api", "Product catalog backed by Postgres"),
        ("svc-cdn", "cdn-edge", "Edge cache fronting static assets"),
    ]:
        result = executor.execute(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": entity_id,
                    "entity_type": "service",
                    "name": name,
                    "properties": {
                        "description": description,
                        "domain": "platform",
                    },
                },
                requested_by="integration:pack",
            ),
        )
        assert result.status is CommandStatus.SUCCESS

    builder = PackBuilder(
        strategies=[GraphSearch(registry.knowledge.graph_store)],
        event_log=registry.operational.event_log,
    )
    pack = builder.build(
        intent="catalog services",
        domain="platform",
        budget=PackBudget(max_items=5, max_tokens=2000),
    )

    item_ids = {item.item_id for item in pack.items}
    assert item_ids == {"svc-search", "svc-catalog", "svc-cdn"}
    assert pack.retrieval_report.candidates_found >= 3
    assert pack.retrieval_report.strategies_used == ["graph"]

    pack_events = registry.operational.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED,
    )
    assert any(e.entity_id == pack.pack_id for e in pack_events)


# ---------------------------------------------------------------------------
# 4. Cross-store: vector.upsert(node_id, ...) → SemanticSearch → PackBuilder
# ---------------------------------------------------------------------------


_EMBEDDER_DIMS = 3


def _fake_embedder(text: str) -> list[float]:
    """Deterministic 3-d embedding driven by character buckets.

    Width matches the integration vector index (see conftest). Splits
    the input into 3 buckets by ord modulo 3 and counts hits per
    bucket, then L2-normalises so cosine similarity returns 1.0 on
    identical input. Avoids any LLM dependency in CI.
    """
    vec = [0.0] * _EMBEDDER_DIMS
    for ch in text.lower():
        vec[ord(ch) % _EMBEDDER_DIMS] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def test_vector_upsert_and_semantic_pack(registry: Any, executor: Any) -> None:
    """Embeddings attached to graph nodes are reachable through PackBuilder.

    Validates the shape #2 contract end-to-end: ENTITY_CREATE lands the
    node, ``vector.upsert(node_id, ...)`` attaches an embedding to that
    same row, and SemanticSearch (via PackBuilder) returns the node
    against a similarity query.
    """
    create = executor.execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={
                "entity_id": "doc-postgres-tuning",
                "entity_type": "document",
                "name": "Postgres tuning playbook",
                "properties": {
                    "excerpt": "How to tune Postgres autovacuum for OLTP workloads.",
                },
            },
            requested_by="integration:vector",
        ),
    )
    assert create.status is CommandStatus.SUCCESS

    excerpt = "How to tune Postgres autovacuum for OLTP workloads."
    embedding = _fake_embedder(excerpt)
    registry.knowledge.vector_store.upsert(
        "doc-postgres-tuning",
        embedding,
        metadata={"content": excerpt, "domain": "platform"},
    )

    builder = PackBuilder(
        strategies=[
            SemanticSearch(registry.knowledge.vector_store, _fake_embedder),
        ],
        event_log=registry.operational.event_log,
    )
    pack = builder.build(
        intent="tune postgres autovacuum for oltp workloads",
        domain="platform",
        budget=PackBudget(max_items=5, max_tokens=2000),
    )

    assert any(item.item_id == "doc-postgres-tuning" for item in pack.items), (
        f"semantic search did not return the embedded node: "
        f"{[i.item_id for i in pack.items]}"
    )
    target = next(i for i in pack.items if i.item_id == "doc-postgres-tuning")
    assert target.item_type == "vector"
    assert target.relevance_score > 0.0
