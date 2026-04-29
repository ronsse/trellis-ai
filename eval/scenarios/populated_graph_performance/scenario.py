"""Populated-graph performance scenario.

Reuses the deterministic graph generator from scenario 5.1, ingests
into every reachable backend, runs a timed query mix, and records
percentile latencies plus vector recall@k against a brute-force
baseline. Shares ``eval/_backends.py`` helpers with scenario 5.1.
"""

from __future__ import annotations

import math
import statistics
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from eval._backends import (
    BackendHandle,
    get_neo4j_config,
    get_postgres_dsn,
    register_handle,
)
from eval._live_wipe import wipe_live_state
from eval.generators.graph_generator import (
    GeneratedGraph,
    GeneratedNode,
    generate_graph,
)
from eval.runner import Finding, ScenarioReport, ScenarioStatus
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


# Default scale: smaller than plan §5.3's 10-50K nodes so dev-machine
# runs are tractable. ``run()`` kwargs let scheduled runs dial up.
DEFAULT_NODE_COUNT = 1_000
DEFAULT_EDGE_COUNT = 4_000
DEFAULT_EMBEDDING_COUNT = 200
# Aligned with the pgvector contract suite's ``DIMS=3`` so eval
# scenarios cohabit with unit tests on the shared Neon DB. See the
# matching note on ``DEFAULT_EMBEDDING_DIM`` in scenario 5.1.
DEFAULT_EMBEDDING_DIM = 3
DEFAULT_VECTOR_TOP_K = 10
DEFAULT_INGEST_THROUGHPUT_FLOOR = 100.0  # nodes / sec
DEFAULT_RECALL_FLOOR = 0.95


@dataclass(frozen=True)
class QueryMixCounts:
    entity_lookups: int = 20
    type_queries: int = 10
    subgraph_traversals: int = 10
    vector_searches: int = 10


@dataclass
class _BackendMeasurements:
    ingest_seconds: float
    ingest_throughput_nodes_per_sec: float
    latencies_ms: dict[str, list[float]]
    recall_at_k_mean: float | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _time_call(fn: Callable[[], Any]) -> tuple[Any, float]:
    """Invoke ``fn``; return ``(result, elapsed_ms)``."""
    start = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - start) * 1000.0


def _percentiles(values: list[float]) -> dict[str, float]:
    """Return p50/p95/p99 in ms; missing values ⇒ -1.0 sentinel.

    ``statistics.quantiles`` requires at least two data points; for
    smaller samples we report the single value as p50 and -1.0 for the
    higher percentiles to make absent measurements visually obvious in
    reports without breaking the metrics-dict shape.
    """
    if not values:
        return {"p50_ms": -1.0, "p95_ms": -1.0, "p99_ms": -1.0}
    if len(values) == 1:
        only = round(values[0], 4)
        return {"p50_ms": only, "p95_ms": -1.0, "p99_ms": -1.0}

    sorted_values = sorted(values)

    def _quantile(p: float) -> float:
        # Linear interpolation matches statistics.quantiles default; we
        # roll our own to avoid the n>=2 corner case it raises on.
        idx = p * (len(sorted_values) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(sorted_values) - 1)
        frac = idx - lo
        return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac

    return {
        "p50_ms": round(_quantile(0.50), 4),
        "p95_ms": round(_quantile(0.95), 4),
        "p99_ms": round(_quantile(0.99), 4),
    }


def _brute_force_top_k(
    query_vec: list[float],
    embedded_nodes: list[GeneratedNode],
    k: int,
) -> list[str]:
    """Cosine similarity over all embedded nodes; return top-k node ids.

    Both inputs are unit vectors (the generator emits them that way) so
    cosine reduces to dot product — correct *and* slightly faster.
    """
    scored: list[tuple[float, str]] = []
    for node in embedded_nodes:
        emb = node.embedding
        if emb is None:
            continue
        score = sum(a * b for a, b in zip(query_vec, emb, strict=True))
        scored.append((score, node.node_id))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [nid for _, nid in scored[:k]]


def _recall_at_k(brute: list[str], approx: list[str], k: int) -> float:
    if k <= 0:
        return 1.0
    return len(set(brute[:k]) & set(approx[:k])) / k


# ---------------------------------------------------------------------------
# Workload
# ---------------------------------------------------------------------------


def _ingest(handle: BackendHandle, graph: GeneratedGraph) -> float:
    """Same shape as scenario 5.1's ingest. Returns wall seconds.

    Uses ``upsert_nodes_bulk`` / ``upsert_edges_bulk`` so the
    populated-graph throughput measurement reflects realistic bulk
    ingest, not the per-row network-bound floor that this scenario was
    originally designed to surface as a Phase 3 deferred item.

    Edges are deduplicated before the bulk call (same reason as
    scenario 5.1: the generator emits with-replacement, but the bulk
    contract forbids in-batch duplicates).
    """
    start = time.perf_counter()
    knowledge = handle.registry.knowledge
    graph_store = knowledge.graph_store
    vector_store = knowledge.vector_store

    graph_store.upsert_nodes_bulk(
        [
            {
                "node_id": n.node_id,
                "node_type": n.node_type,
                "properties": n.properties,
            }
            for n in graph.nodes
        ]
    )
    # ``upsert_bulk`` so the throughput metric isn't dominated by the
    # per-row network round trip (200 embeddings x ~70ms baseline on
    # AuraDB Free = ~14s alone). Without this the scenario keeps
    # reporting the deferred-item warning even after the underlying
    # bulk-ingest paths land their fast path.
    vector_rows = [
        {
            "item_id": n.node_id,
            "vector": n.embedding,
            "metadata": {"node_type": n.node_type},
        }
        for n in graph.nodes
        if n.embedding is not None
    ]
    if vector_rows:
        vector_store.upsert_bulk(vector_rows)
    deduped_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in graph.edges:
        deduped_edges[(e.source_id, e.target_id, e.edge_type)] = {
            "source_id": e.source_id,
            "target_id": e.target_id,
            "edge_type": e.edge_type,
            "properties": e.properties,
        }
    graph_store.upsert_edges_bulk(list(deduped_edges.values()))

    return time.perf_counter() - start


def _measure(
    handle: BackendHandle,
    graph: GeneratedGraph,
    counts: QueryMixCounts,
    *,
    vector_top_k: int,
) -> _BackendMeasurements:
    """Run the timed query mix; return raw latencies + recall mean."""
    knowledge = handle.registry.knowledge
    graph_store = knowledge.graph_store
    vector_store = knowledge.vector_store

    embedded = [n for n in graph.nodes if n.embedding is not None]
    latencies: dict[str, list[float]] = {
        "entity_lookup": [],
        "type_query": [],
        "subgraph": [],
        "vector_topk": [],
    }

    # Entity lookups — round-robin over node ids.
    for i in range(counts.entity_lookups):
        node_id = graph.nodes[i % len(graph.nodes)].node_id

        def _get_node(nid: str = node_id) -> Any:
            return graph_store.get_node(nid)

        _, elapsed = _time_call(_get_node)
        latencies["entity_lookup"].append(elapsed)

    # Type queries — cycle through known types.
    type_pool = ["entity", "artifact", "concept", "event"]
    for i in range(counts.type_queries):
        node_type = type_pool[i % len(type_pool)]

        def _do_query(t: str = node_type) -> Any:
            return graph_store.query(node_type=t, limit=200)

        _, elapsed = _time_call(_do_query)
        latencies["type_query"].append(elapsed)

    # Subgraph traversals — seed from the generator's reserved seeds.
    seed_pool = graph.seed_ids or [graph.nodes[0].node_id]
    for i in range(counts.subgraph_traversals):
        seeds = [seed_pool[i % len(seed_pool)]]

        def _do_subgraph(s: list[str] = seeds) -> Any:
            return graph_store.get_subgraph(seed_ids=s, depth=2)

        _, elapsed = _time_call(_do_subgraph)
        latencies["subgraph"].append(elapsed)

    # Vector top-k — query with the first N embedded nodes' vectors.
    recalls: list[float] = []
    if embedded:
        for i in range(counts.vector_searches):
            query_node = embedded[i % len(embedded)]
            if query_node.embedding is None:
                continue  # unreachable given `embedded` filter; appeases mypy
            query_emb: list[float] = query_node.embedding

            def _do_vector(v: list[float] = query_emb) -> Any:
                return vector_store.query(vector=v, top_k=vector_top_k)

            backend_top, elapsed = _time_call(_do_vector)
            latencies["vector_topk"].append(elapsed)

            backend_ids = [r["item_id"] for r in backend_top]
            brute = _brute_force_top_k(query_emb, embedded, vector_top_k)
            recalls.append(_recall_at_k(brute, backend_ids, vector_top_k))

    ingest_seconds = -1.0  # filled in by caller; keeps this fn pure
    throughput = float("nan")
    return _BackendMeasurements(
        ingest_seconds=ingest_seconds,
        ingest_throughput_nodes_per_sec=throughput,
        latencies_ms=latencies,
        recall_at_k_mean=(statistics.fmean(recalls) if recalls else None),
    )


# ---------------------------------------------------------------------------
# Backend handles
# ---------------------------------------------------------------------------


_SQLITE_OPERATIONAL = {
    "trace": {"backend": "sqlite"},
    "event_log": {"backend": "sqlite"},
}


def _build_backends(
    stack: ExitStack,
    sqlite_dir: Path,
    *,
    embedding_dim: int,
) -> list[BackendHandle]:
    """Same probe-and-skip pattern as scenario 5.1, sharing eval/_backends.py."""
    handles: list[BackendHandle] = []

    register_handle(
        stack,
        handles,
        name="sqlite",
        config={
            "knowledge": {
                "graph": {"backend": "sqlite"},
                "vector": {"backend": "sqlite"},
                "document": {"backend": "sqlite"},
                "blob": {"backend": "local"},
            },
            "operational": _SQLITE_OPERATIONAL,
        },
        stores_dir=sqlite_dir,
    )

    pg_dsn = get_postgres_dsn()
    if pg_dsn:
        register_handle(
            stack,
            handles,
            name="postgres",
            config={
                "knowledge": {
                    "graph": {"backend": "postgres", "dsn": pg_dsn},
                    "vector": {
                        "backend": "pgvector",
                        "dsn": pg_dsn,
                        "dimensions": embedding_dim,
                    },
                    "document": {"backend": "sqlite"},
                    "blob": {"backend": "local"},
                },
                "operational": _SQLITE_OPERATIONAL,
            },
            stores_dir=sqlite_dir / "pg",
        )

    neo4j_graph = get_neo4j_config()
    neo4j_vector = get_neo4j_config(dimensions=embedding_dim)
    if neo4j_graph and neo4j_vector:
        register_handle(
            stack,
            handles,
            name="neo4j",
            config={
                "knowledge": {
                    "graph": neo4j_graph,
                    "vector": neo4j_vector,
                    "document": {"backend": "sqlite"},
                    "blob": {"backend": "local"},
                },
                "operational": _SQLITE_OPERATIONAL,
            },
            stores_dir=sqlite_dir / "neo4j",
        )

    return handles


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    registry: StoreRegistry,  # noqa: ARG001 — scenario builds its own registries
    *,
    seed: int = 0,
    node_count: int = DEFAULT_NODE_COUNT,
    edge_count: int = DEFAULT_EDGE_COUNT,
    embedding_count: int = DEFAULT_EMBEDDING_COUNT,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    vector_top_k: int = DEFAULT_VECTOR_TOP_K,
    counts: QueryMixCounts | None = None,
    ingest_throughput_floor: float = DEFAULT_INGEST_THROUGHPUT_FLOOR,
    recall_floor: float = DEFAULT_RECALL_FLOOR,
) -> ScenarioReport:
    """Execute the populated-graph performance scenario.

    The runner-supplied ``registry`` is intentionally ignored — like
    scenario 5.1 this scenario constructs its own registries because
    cross-backend comparison is the point.
    """
    counts = counts or QueryMixCounts()
    graph = generate_graph(
        seed=seed,
        node_count=node_count,
        edge_count=edge_count,
        embedding_count=embedding_count,
        embedding_dim=embedding_dim,
    )

    findings: list[Finding] = []
    metrics: dict[str, float] = {
        "node_count": float(len(graph.nodes)),
        "edge_count": float(len(graph.edges)),
        "embedding_count": float(embedding_count),
    }

    with ExitStack() as stack:
        sqlite_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        handles = _build_backends(stack, sqlite_dir, embedding_dim=embedding_dim)
        configured_backends = sorted(h.name for h in handles)
        metrics["backends_measured"] = float(len(handles))
        findings.append(
            Finding(
                severity="info",
                message=("measured backends: " + ", ".join(configured_backends)),
            )
        )
        findings.extend(
            Finding(
                severity="info",
                message=f"{missing} backend skipped — no credentials in env",
            )
            for missing in sorted({"postgres", "neo4j"} - set(configured_backends))
        )

        for handle in handles:
            # Stale rows from prior runs inflate latency + index size on
            # PG and Neo4j; wipe before each handle's run so throughput
            # measurements aren't biased by accumulated state. Same
            # hygiene pattern as scenarios 5.1 and 5.5.
            wipe_live_state(handle.registry)
            ingest_seconds = _ingest(handle, graph)
            throughput = (
                len(graph.nodes) / ingest_seconds
                if ingest_seconds > 0
                else float("inf")
            )
            measurements = _measure(handle, graph, counts, vector_top_k=vector_top_k)
            measurements.ingest_seconds = ingest_seconds
            measurements.ingest_throughput_nodes_per_sec = throughput

            metrics[f"ingest_seconds.{handle.name}"] = round(ingest_seconds, 4)
            metrics[f"ingest_nodes_per_sec.{handle.name}"] = round(throughput, 2)
            for query_name, latencies in measurements.latencies_ms.items():
                pct = _percentiles(latencies)
                for k, v in pct.items():
                    metrics[f"{query_name}.{handle.name}.{k}"] = v

            if measurements.recall_at_k_mean is not None:
                metrics[f"vector_recall_at_{vector_top_k}.{handle.name}"] = round(
                    measurements.recall_at_k_mean, 4
                )
                if measurements.recall_at_k_mean < recall_floor:
                    findings.append(
                        Finding(
                            severity="warn",
                            message=(
                                f"{handle.name}: vector recall@{vector_top_k} "
                                f"= {measurements.recall_at_k_mean:.3f} below "
                                f"floor {recall_floor} — HNSW M / "
                                "efConstruction tuning warranted"
                            ),
                        )
                    )

            if throughput < ingest_throughput_floor and not math.isinf(throughput):
                findings.append(
                    Finding(
                        severity="warn",
                        message=(
                            f"{handle.name}: ingest throughput "
                            f"{throughput:.1f} nodes/sec below floor "
                            f"{ingest_throughput_floor} — UNWIND-based bulk "
                            "upsert path warranted"
                        ),
                    )
                )

    regressed = any(f.severity == "warn" for f in findings)
    failed = any(f.severity == "fail" for f in findings)
    status: ScenarioStatus = "fail" if failed else ("regress" if regressed else "pass")

    decision = (
        "Per-backend latency percentiles + recall@k baseline are now "
        "produced. Plan §5.3 deferred items become actionable based on "
        "the warn findings: HNSW tuning fires when recall < "
        f"{recall_floor}; UNWIND bulk path fires when ingest throughput "
        f"< {ingest_throughput_floor} nodes/sec. Pin these latencies as "
        "a baseline; subsequent runs diff against it. EXPLAIN / PROFILE "
        "plan capture is intentionally deferred to the live-data "
        "revisit pass — see scenario README."
    )

    return ScenarioReport(
        name="populated_graph_performance",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )
