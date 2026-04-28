"""Multi-backend equivalence scenario.

Builds its own registries — the runner-supplied ``registry`` argument is
honoured as the SQLite baseline path (overrides the tmp-dir one) but
the Postgres / Neo4j paths are constructed here from environment
variables. Multi-backend scenarios are inherently special-cased.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from eval.generators.graph_generator import GeneratedGraph, generate_graph
from eval.runner import Finding, ScenarioReport, ScenarioStatus
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


# Defaults are intentionally smaller than plan §5.1's "1K nodes / 5K
# edges / 200 embeddings". The numbers below give meaningful
# multi-backend signal at ~100x faster wall time, which is what we want
# for a scenario that may run on developer machines as a sanity check.
# The full-scale parameters are exposed as kwargs to ``run()`` so a
# scheduled run can dial them up.
DEFAULT_NODE_COUNT = 200
DEFAULT_EDGE_COUNT = 800
DEFAULT_EMBEDDING_COUNT = 50
DEFAULT_EMBEDDING_DIM = 16
DEFAULT_VECTOR_TOP_K = 10
RECALL_REGRESS_THRESHOLD = 0.9
MIN_BACKENDS_FOR_DIFF = 2


@dataclass
class _BackendHandle:
    name: str
    registry: StoreRegistry


def _ingest(handle: _BackendHandle, graph: GeneratedGraph) -> float:
    """Write nodes + edges + embeddings into a backend; return seconds.

    Uses ``upsert_nodes_bulk`` / ``upsert_edges_bulk`` so the cross-
    backend comparison isn't dominated by per-row network round trips
    on Neo4j. The vector store doesn't have a bulk method yet — its
    upsert path is already 1 round trip per row, so the marginal cost
    is small at 50 embeddings (default).

    Edges are deduplicated by ``(source_id, target_id, edge_type)``
    before the bulk call: the generator emits with-replacement, but the
    bulk contract forbids in-batch duplicates because backends can't
    preserve last-write-wins ordering across a single UNWIND. Last
    occurrence wins, matching what the per-row ``upsert_edge`` loop
    used to produce.
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
    for node in graph.nodes:
        if node.embedding is not None:
            vector_store.upsert(
                item_id=node.node_id,
                vector=node.embedding,
                metadata={"node_type": node.node_type},
            )
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


def _query_results(
    handle: _BackendHandle,
    graph: GeneratedGraph,
    *,
    vector_top_k: int,
) -> dict[str, Any]:
    """Run the fixed query mix and return ids/sets the runner can diff."""
    knowledge = handle.registry.knowledge
    graph_store = knowledge.graph_store
    vector_store = knowledge.vector_store

    type_query_ids = {
        row["node_id"] for row in graph_store.query(node_type="entity", limit=10_000)
    }

    subgraph = graph_store.get_subgraph(seed_ids=graph.seed_ids, depth=2)
    subgraph_node_ids = {n["node_id"] for n in subgraph.get("nodes", [])}
    subgraph_edge_tuples = {
        (e["source_id"], e["target_id"], e["edge_type"])
        for e in subgraph.get("edges", [])
    }

    # Vector top-k for the first three embedded nodes — same query
    # vector across backends because the input list is shared.
    vector_topk: dict[str, list[str]] = {}
    for node in graph.nodes[:3]:
        if node.embedding is None:
            continue
        results = vector_store.query(vector=node.embedding, top_k=vector_top_k)
        vector_topk[node.node_id] = [r["item_id"] for r in results]

    return {
        "type_query_ids": type_query_ids,
        "subgraph_node_ids": subgraph_node_ids,
        "subgraph_edge_tuples": subgraph_edge_tuples,
        "vector_topk": vector_topk,
    }


def _recall_at_k(left: list[str], right: list[str], k: int) -> float:
    if k <= 0:
        return 1.0
    overlap = len(set(left[:k]) & set(right[:k]))
    return overlap / k


def _diff_pair(
    a_name: str,
    a: dict[str, Any],
    b_name: str,
    b: dict[str, Any],
    *,
    vector_top_k: int,
) -> tuple[list[Finding], dict[str, float]]:
    findings: list[Finding] = []
    metrics: dict[str, float] = {}

    type_only_a = a["type_query_ids"] - b["type_query_ids"]
    type_only_b = b["type_query_ids"] - a["type_query_ids"]
    if type_only_a or type_only_b:
        findings.append(
            Finding(
                severity="fail",
                message=f"type-query id sets differ: {a_name} vs {b_name}",
                detail={
                    f"only_in_{a_name}": sorted(type_only_a)[:20],
                    f"only_in_{b_name}": sorted(type_only_b)[:20],
                },
            )
        )

    sg_only_a = a["subgraph_node_ids"] - b["subgraph_node_ids"]
    sg_only_b = b["subgraph_node_ids"] - a["subgraph_node_ids"]
    if sg_only_a or sg_only_b:
        findings.append(
            Finding(
                severity="fail",
                message=f"subgraph node sets differ: {a_name} vs {b_name}",
                detail={
                    f"only_in_{a_name}": sorted(sg_only_a)[:20],
                    f"only_in_{b_name}": sorted(sg_only_b)[:20],
                },
            )
        )

    edge_only_a = a["subgraph_edge_tuples"] - b["subgraph_edge_tuples"]
    edge_only_b = b["subgraph_edge_tuples"] - a["subgraph_edge_tuples"]
    if edge_only_a or edge_only_b:
        findings.append(
            Finding(
                severity="fail",
                message=f"subgraph edge sets differ: {a_name} vs {b_name}",
                detail={
                    f"only_in_{a_name}": sorted(map(str, edge_only_a))[:20],
                    f"only_in_{b_name}": sorted(map(str, edge_only_b))[:20],
                },
            )
        )

    common_keys = set(a["vector_topk"]) & set(b["vector_topk"])
    recalls = [
        _recall_at_k(a["vector_topk"][key], b["vector_topk"][key], vector_top_k)
        for key in common_keys
    ]
    if recalls:
        avg_recall = sum(recalls) / len(recalls)
        metrics[f"vector_recall_overlap.{a_name}_vs_{b_name}"] = round(avg_recall, 4)
        if avg_recall < RECALL_REGRESS_THRESHOLD:
            findings.append(
                Finding(
                    severity="warn",
                    message=(
                        f"vector top-{vector_top_k} recall overlap {a_name} "
                        f"vs {b_name} = {avg_recall:.3f} below "
                        f"{RECALL_REGRESS_THRESHOLD} threshold"
                    ),
                )
            )

    return findings, metrics


def _build_backends(
    stack: ExitStack, tmp_dir: Path, *, embedding_dim: int
) -> list[_BackendHandle]:
    """Construct every backend handle the env can reach.

    SQLite is always available (writes under ``tmp_dir``). Postgres
    and Neo4j are only attempted if their credentials env vars are
    present. Construction failures are logged and treated as "backend
    skipped" — these backends are env-gated optional, so any failure
    (network, config, missing extra) drops out cleanly rather than
    aborting the whole scenario.
    """
    handles: list[_BackendHandle] = []

    sqlite_config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    sqlite_reg = stack.enter_context(
        StoreRegistry(config=sqlite_config, stores_dir=tmp_dir)
    )
    handles.append(_BackendHandle(name="sqlite", registry=sqlite_reg))

    pg_dsn = os.environ.get("TRELLIS_KNOWLEDGE_PG_DSN") or os.environ.get(
        "TRELLIS_PG_DSN"
    )
    if pg_dsn:
        pg_config = {
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
            "operational": {
                "trace": {"backend": "sqlite"},
                "event_log": {"backend": "sqlite"},
            },
        }
        try:
            pg_reg = stack.enter_context(
                StoreRegistry(config=pg_config, stores_dir=tmp_dir / "pg")
            )
            handles.append(_BackendHandle(name="postgres", registry=pg_reg))
        except Exception as exc:
            logger.warning("eval.postgres_unavailable", error=str(exc))

    neo4j_uri = os.environ.get("TRELLIS_NEO4J_URI")
    neo4j_user = os.environ.get("TRELLIS_NEO4J_USER")
    neo4j_password = os.environ.get("TRELLIS_NEO4J_PASSWORD")
    if neo4j_uri and neo4j_user and neo4j_password:
        neo4j_config = {
            "knowledge": {
                "graph": {
                    "backend": "neo4j",
                    "uri": neo4j_uri,
                    "user": neo4j_user,
                    "password": neo4j_password,
                },
                "vector": {
                    "backend": "neo4j",
                    "uri": neo4j_uri,
                    "user": neo4j_user,
                    "password": neo4j_password,
                    "dimensions": embedding_dim,
                },
                "document": {"backend": "sqlite"},
                "blob": {"backend": "local"},
            },
            "operational": {
                "trace": {"backend": "sqlite"},
                "event_log": {"backend": "sqlite"},
            },
        }
        try:
            neo4j_reg = stack.enter_context(
                StoreRegistry(config=neo4j_config, stores_dir=tmp_dir / "neo4j")
            )
            handles.append(_BackendHandle(name="neo4j", registry=neo4j_reg))
        except Exception as exc:
            logger.warning("eval.neo4j_unavailable", error=str(exc))

    return handles


def run(
    registry: StoreRegistry,  # noqa: ARG001 — scenario builds its own registries
    *,
    seed: int = 0,
    node_count: int = DEFAULT_NODE_COUNT,
    edge_count: int = DEFAULT_EDGE_COUNT,
    embedding_count: int = DEFAULT_EMBEDDING_COUNT,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    vector_top_k: int = DEFAULT_VECTOR_TOP_K,
) -> ScenarioReport:
    """Execute the multi-backend equivalence scenario.

    The runner-supplied ``registry`` is intentionally ignored: this
    scenario constructs its own registries because comparing backends
    is the entire point. See the README in this directory.
    """
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
        tmp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        handles = _build_backends(stack, tmp_dir, embedding_dim=embedding_dim)

        configured_backends = sorted(h.name for h in handles)
        metrics["backends_compared"] = float(len(handles))
        findings.append(
            Finding(
                severity="info",
                message=f"compared backends: {', '.join(configured_backends)}",
            )
        )
        findings.extend(
            Finding(
                severity="info",
                message=f"{missing} backend skipped — no credentials in env",
            )
            for missing in sorted({"postgres", "neo4j"} - set(configured_backends))
        )

        results: dict[str, dict[str, Any]] = {}
        for handle in handles:
            ingest_seconds = _ingest(handle, graph)
            metrics[f"ingest_seconds.{handle.name}"] = round(ingest_seconds, 4)
            results[handle.name] = _query_results(
                handle, graph, vector_top_k=vector_top_k
            )

        # Pairwise diff against the first backend. Sqlite is always
        # present and is the reference because the contract test suite
        # treats it as the canonical implementation.
        if len(handles) >= MIN_BACKENDS_FOR_DIFF:
            reference = handles[0].name
            for handle in handles[1:]:
                pair_findings, pair_metrics = _diff_pair(
                    reference,
                    results[reference],
                    handle.name,
                    results[handle.name],
                    vector_top_k=vector_top_k,
                )
                findings.extend(pair_findings)
                metrics.update(pair_metrics)

    failed = any(f.severity == "fail" for f in findings)
    regressed = any(f.severity == "warn" for f in findings)
    status: ScenarioStatus
    if failed:
        status = "fail"
    elif regressed:
        status = "regress"
    else:
        status = "pass"

    if len(handles) >= MIN_BACKENDS_FOR_DIFF:
        decision = (
            "Confirms (or denies) cross-backend equivalence on the canonical "
            "DSL. Unblocks plan §5.1 deferred items: vector DSL Phase 4 "
            "(canonical translation layer) and EXPLAIN-validated query plan "
            "baseline. If status==pass with all three backends compared, "
            "promote the relevant Phase 3 items to active. If status==fail, "
            "the offending backend has a contract bug — fix before scaling."
        )
    else:
        decision = (
            "Single-backend run only — re-run with TRELLIS_KNOWLEDGE_PG_DSN "
            "and TRELLIS_NEO4J_URI set to get a real equivalence signal."
        )

    return ScenarioReport(
        name="multi_backend_equivalence",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )
