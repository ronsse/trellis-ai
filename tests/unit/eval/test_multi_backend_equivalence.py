"""Unit tests for the multi-backend equivalence scenario.

CI-runnable subset: exercises the SQLite-only path (no live Postgres /
Neo4j). The cross-backend semantics are validated by running the
scenario against `.env`-configured backends and inspecting the report;
that's not what these tests are for.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from eval.generators.graph_generator import generate_graph
from eval.scenarios.multi_backend_equivalence.scenario import (
    RECALL_REGRESS_THRESHOLD,
    run,
)


def test_generator_is_deterministic() -> None:
    a = generate_graph(seed=42, node_count=20, edge_count=40, embedding_count=10)
    b = generate_graph(seed=42, node_count=20, edge_count=40, embedding_count=10)

    assert [n.node_id for n in a.nodes] == [n.node_id for n in b.nodes]
    assert [n.node_type for n in a.nodes] == [n.node_type for n in b.nodes]
    assert [(e.source_id, e.target_id, e.edge_type) for e in a.edges] == [
        (e.source_id, e.target_id, e.edge_type) for e in b.edges
    ]
    assert a.nodes[0].embedding == b.nodes[0].embedding


def test_generator_different_seeds_produce_different_graphs() -> None:
    a = generate_graph(seed=1, node_count=20, edge_count=40, embedding_count=10)
    b = generate_graph(seed=2, node_count=20, edge_count=40, embedding_count=10)

    a_edges = [(e.source_id, e.target_id, e.edge_type) for e in a.edges]
    b_edges = [(e.source_id, e.target_id, e.edge_type) for e in b.edges]
    assert a_edges != b_edges


def test_generator_emits_unit_vectors() -> None:
    g = generate_graph(
        seed=0, node_count=5, edge_count=0, embedding_count=5, embedding_dim=8
    )
    for node in g.nodes:
        assert node.embedding is not None
        norm_sq = sum(x * x for x in node.embedding)
        assert abs(norm_sq - 1.0) < 1e-9


def test_run_sqlite_only_skips_postgres_and_neo4j(monkeypatch) -> None:
    """With no env vars set, only SQLite runs — and the scenario succeeds."""
    for var in (
        "TRELLIS_KNOWLEDGE_PG_DSN",
        "TRELLIS_PG_DSN",
        "TRELLIS_NEO4J_URI",
        "TRELLIS_NEO4J_USER",
        "TRELLIS_NEO4J_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)

    registry = MagicMock()
    report = run(
        registry,
        seed=0,
        node_count=20,
        edge_count=30,
        embedding_count=10,
        embedding_dim=8,
        vector_top_k=5,
    )

    assert report.name == "multi_backend_equivalence"
    assert report.status == "pass"
    assert report.metrics["backends_compared"] == 1.0
    assert report.metrics["node_count"] == 20.0

    messages = [f.message for f in report.findings]
    assert any("compared backends: sqlite" in m for m in messages)
    assert any("postgres backend skipped" in m for m in messages)
    assert any("neo4j backend skipped" in m for m in messages)


def test_run_records_ingest_seconds_metric(monkeypatch) -> None:
    monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)
    monkeypatch.delenv("TRELLIS_PG_DSN", raising=False)
    monkeypatch.delenv("TRELLIS_NEO4J_URI", raising=False)

    report = run(MagicMock(), seed=0, node_count=10, edge_count=10, embedding_count=5)

    assert "ingest_seconds.sqlite" in report.metrics
    assert report.metrics["ingest_seconds.sqlite"] >= 0.0


def test_recall_threshold_constant_is_reasonable() -> None:
    """Pin the threshold so a tightening review surfaces it."""
    assert 0.5 <= RECALL_REGRESS_THRESHOLD <= 1.0
