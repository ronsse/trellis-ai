"""Unit tests for the populated-graph performance scenario.

CI-runnable subset — exercises the SQLite-only path with a small
graph. Live multi-backend timing is the scheduled-run job.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from eval.generators.graph_generator import generate_graph
from eval.scenarios.populated_graph_performance.scenario import (
    DEFAULT_RECALL_FLOOR,
    QueryMixCounts,
    _brute_force_top_k,
    _percentiles,
    _recall_at_k,
    run,
)


def test_percentiles_empty_returns_sentinels() -> None:
    pct = _percentiles([])
    assert pct == {"p50_ms": -1.0, "p95_ms": -1.0, "p99_ms": -1.0}


def test_percentiles_single_value_only_p50() -> None:
    pct = _percentiles([4.2])
    assert pct["p50_ms"] == 4.2
    assert pct["p95_ms"] == -1.0
    assert pct["p99_ms"] == -1.0


def test_percentiles_monotonic_ordering() -> None:
    """p50 <= p95 <= p99 — pin this since percentile computation is bespoke."""
    values = [float(x) for x in range(1, 101)]
    pct = _percentiles(values)
    assert pct["p50_ms"] <= pct["p95_ms"] <= pct["p99_ms"]
    # On 1..100 the linear-interpolation p50 is 50.5; loose check.
    assert 49 <= pct["p50_ms"] <= 51
    assert 94 <= pct["p95_ms"] <= 96


def test_recall_at_k_full_overlap() -> None:
    assert _recall_at_k(["a", "b", "c"], ["a", "b", "c"], k=3) == 1.0


def test_recall_at_k_no_overlap() -> None:
    assert _recall_at_k(["a", "b", "c"], ["x", "y", "z"], k=3) == 0.0


def test_recall_at_k_partial() -> None:
    assert _recall_at_k(["a", "b", "c"], ["a", "x", "c"], k=3) == 2 / 3


def test_brute_force_top_k_returns_self_first() -> None:
    """Querying with a node's own embedding should find it at rank 1."""
    graph = generate_graph(
        seed=0, node_count=8, edge_count=0, embedding_count=8, embedding_dim=8
    )
    embedded = [n for n in graph.nodes if n.embedding is not None]
    assert embedded[0].embedding is not None  # narrow Optional
    top = _brute_force_top_k(embedded[0].embedding, embedded, k=3)
    assert top[0] == embedded[0].node_id


def test_run_sqlite_only_skips_other_backends(monkeypatch) -> None:
    """No env set ⇒ only SQLite is measured; scenario succeeds."""
    for var in (
        "TRELLIS_KNOWLEDGE_PG_DSN",
        "TRELLIS_PG_DSN",
        "TRELLIS_NEO4J_URI",
        "TRELLIS_NEO4J_USER",
        "TRELLIS_NEO4J_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)

    report = run(
        MagicMock(),
        seed=0,
        node_count=30,
        edge_count=40,
        embedding_count=10,
        embedding_dim=8,
        counts=QueryMixCounts(
            entity_lookups=4,
            type_queries=3,
            subgraph_traversals=3,
            vector_searches=4,
        ),
    )

    assert report.name == "populated_graph_performance"
    assert report.status in {"pass", "regress"}
    assert report.metrics["backends_measured"] == 1.0

    # Every per-query latency family is populated for sqlite.
    for query in ("entity_lookup", "type_query", "subgraph", "vector_topk"):
        for pct in ("p50_ms", "p95_ms", "p99_ms"):
            assert f"{query}.sqlite.{pct}" in report.metrics

    assert "ingest_seconds.sqlite" in report.metrics
    assert "ingest_nodes_per_sec.sqlite" in report.metrics
    # Recall metric exists when embeddings present.
    assert "vector_recall_at_10.sqlite" in report.metrics


def test_recall_floor_pinned_as_constant() -> None:
    """Catch a tightening review by surfacing the threshold value."""
    assert 0.5 <= DEFAULT_RECALL_FLOOR <= 1.0


def test_query_mix_counts_dataclass_is_frozen() -> None:
    """Mutability of the counts struct should be a deliberate design change."""
    import dataclasses

    assert dataclasses.is_dataclass(QueryMixCounts)
    counts = QueryMixCounts()
    try:
        counts.entity_lookups = 9999  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    msg = "QueryMixCounts must remain frozen so callers can't silently mutate"
    raise AssertionError(msg)
