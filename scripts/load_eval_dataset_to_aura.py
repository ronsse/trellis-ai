#!/usr/bin/env python3
"""Load the deterministic eval dataset into the live AuraDB instance.

Single-purpose operational script — populates the AuraDB knowledge plane
(graph + vectors) with the same synthetic graph the eval scenarios use,
so a follow-up ``python -m eval.runner --scenario populated_graph_performance``
actually exercises the live backend.

Reads credentials from environment variables. Honours either family —
``TRELLIS_NEO4J_*`` (production namespace, what the eval scenarios probe)
or ``TRELLIS_TEST_NEO4J_*`` (live-test namespace) — preferring the
production family. Source ``.env`` before running, e.g.

    set -a && source .env && set +a
    python scripts/load_eval_dataset_to_aura.py --node-count 5000

By default it wipes existing nodes + drops the test-namespace vector
index before loading; pass ``--no-wipe`` to skip.

Bulk-write strategy
-------------------
This script bypasses ``StoreRegistry`` / ``Neo4jGraphStore.upsert_node``
and writes via UNWIND-batched Cypher straight against the driver. The
registry path runs ~2 round trips per row (existence check + the
upsert itself) which on AuraDB Free comes out to ~5 nodes/sec — that's
roughly **400x slower** than what UNWIND batching achieves on the same
instance.

That's a real shortcut worth understanding. The library's
``upsert_node`` / ``upsert_edge`` exist for governed, single-row
mutations (validate role immutability, preserve ``created_at`` across
versions, close old SCD-2 versions). The bulk path here intentionally
skips all of that: we just wiped the database, so there is nothing to
preserve and no version to close. Anything other than a fresh-load
should NOT use this path — open ``--no-wipe`` and the loader still
talks straight to the driver and will write duplicate nodes if the
target db isn't empty.

Plan §5.3 deferred item "``upsert_node`` UNWIND-based bulk path" is
the right long-term fix in the library; this script is the empirical
evidence that it's worth doing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

# Make the repo root importable so ``from eval.generators...`` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.generators.graph_generator import GeneratedGraph, generate_graph
from neo4j import GraphDatabase, Session

from trellis.core.ids import generate_ulid

PROGRESS_BATCHES = 5  # progress print every N batches
DEFAULT_BATCH_SIZE = 200


# ---------------------------------------------------------------------------
# Env + credentials
# ---------------------------------------------------------------------------


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(
            key.strip(), value.strip().strip('"').strip("'")
        )


def _resolve_neo4j_creds() -> tuple[str, str, str, str]:
    """Prefer TRELLIS_NEO4J_*; fall back to TRELLIS_TEST_NEO4J_*."""
    for prefix in ("TRELLIS_NEO4J_", "TRELLIS_TEST_NEO4J_"):
        uri = os.environ.get(f"{prefix}URI")
        user = os.environ.get(f"{prefix}USER")
        password = os.environ.get(f"{prefix}PASSWORD")
        if uri and user and password:
            return uri, user, password, os.environ.get(f"{prefix}DATABASE", "neo4j")
    msg = (
        "No Neo4j credentials found. Set TRELLIS_NEO4J_{URI,USER,PASSWORD} "
        "(or TRELLIS_TEST_NEO4J_*) in env or .env before running."
    )
    raise SystemExit(msg)


# ---------------------------------------------------------------------------
# Pre-load wipe + post-load verify
# ---------------------------------------------------------------------------


def _wipe_and_drop_test_index(session: Session) -> dict[str, int]:
    """Detach-delete every node and drop the leftover test vector index."""
    counts = {
        "nodes": session.run("MATCH (n) RETURN count(n) AS c").single()[0],
        "edges": session.run(
            "MATCH ()-[r]->() RETURN count(r) AS c"
        ).single()[0],
        "indexes_dropped": 0,
    }
    session.run("MATCH (n) DETACH DELETE n").consume()
    for idx in ("trellis_test_node_embeddings",):
        try:
            session.run(f"DROP INDEX {idx} IF EXISTS").consume()
            counts["indexes_dropped"] += 1
        except Exception as exc:
            print(f"  could not drop index {idx}: {exc}", file=sys.stderr)
    return counts


def _ensure_vector_index(
    session: Session, *, name: str, dimensions: int
) -> None:
    """Create the vector index up front so embeddings get indexed as we write.

    Mirrors what ``Neo4jVectorStore._init_schema`` would do — we replicate
    it here because we're bypassing the store. Cosine similarity matches
    the existing convention.
    """
    session.run(
        f"CREATE VECTOR INDEX {name} IF NOT EXISTS "
        "FOR (n:Node) ON n.embedding "
        "OPTIONS {indexConfig: {"
        f"  `vector.dimensions`: {dimensions}, "
        "  `vector.similarity_function`: 'cosine'"
        "}}"
    ).consume()


def _verify_post_load(
    session: Session, *, vector_index_name: str
) -> dict[str, object]:
    out: dict[str, object] = {
        "total_nodes": session.run(
            "MATCH (n) RETURN count(n) AS c"
        ).single()[0],
        "total_edges": session.run(
            "MATCH ()-[r]->() RETURN count(r) AS c"
        ).single()[0],
        "nodes_with_embedding": session.run(
            "MATCH (n) WHERE n.embedding IS NOT NULL RETURN count(n) AS c"
        ).single()[0],
        "node_types_top_5": [
            dict(r)
            for r in session.run(
                "MATCH (n:Node) RETURN n.node_type AS type, count(*) AS n "
                "ORDER BY n DESC LIMIT 5"
            )
        ],
    }
    vector = session.run(
        "SHOW INDEXES YIELD name, type, state, options "
        "WHERE name = $name RETURN type, state, options",
        name=vector_index_name,
    ).single()
    if vector:
        cfg = (vector["options"] or {}).get("indexConfig", {})
        out["vector_index"] = {
            "name": vector_index_name,
            "state": vector["state"],
            "type": vector["type"],
            "dimensions": cfg.get("vector.dimensions"),
            "similarity": cfg.get("vector.similarity_function"),
        }
    else:
        out["vector_index"] = None
    return out


# ---------------------------------------------------------------------------
# Bulk UNWIND helpers
# ---------------------------------------------------------------------------


def _batched(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _now_iso() -> str:
    """ISO timestamp — same shape Neo4jGraphStore.upsert_node uses."""
    from datetime import UTC, datetime  # noqa: PLC0415 — local fast import

    return datetime.now(UTC).isoformat()


def _bulk_upsert_nodes(
    session: Session, batch: Iterable[dict[str, Any]]
) -> int:
    """Single round trip: CREATE every node in ``batch`` as a current SCD-2 row.

    Skips the OPTIONAL MATCH / version-close branch the per-row path
    runs because the loader has just wiped the database — there are no
    prior versions to close. Re-running this against a non-empty
    database would create duplicate current rows; the script's
    ``--wipe`` guard is what makes this safe.
    """
    cypher = """
    UNWIND $rows AS row
    CREATE (n:Node)
    SET n = row
    """
    rows = list(batch)
    if not rows:
        return 0
    session.run(cypher, rows=rows).consume()
    return len(rows)


def _bulk_upsert_vectors(
    session: Session, batch: Iterable[dict[str, Any]]
) -> int:
    """Attach embeddings to existing current node versions."""
    cypher = """
    UNWIND $rows AS row
    MATCH (n:Node {node_id: row.item_id})
      WHERE n.valid_to IS NULL
    SET n.embedding = row.vector,
        n.vector_metadata_json = row.meta_json
    """
    rows = list(batch)
    if not rows:
        return 0
    session.run(cypher, rows=rows).consume()
    return len(rows)


def _bulk_upsert_edges(
    session: Session, batch: Iterable[dict[str, Any]]
) -> int:
    """Single round trip: MATCH endpoints + CREATE :EDGE per row."""
    cypher = """
    UNWIND $rows AS row
    MATCH (s:Node {node_id: row.source_id}) WHERE s.valid_to IS NULL
    MATCH (t:Node {node_id: row.target_id}) WHERE t.valid_to IS NULL
    CREATE (s)-[e:EDGE]->(t)
    SET e = row.props
    """
    rows = list(batch)
    if not rows:
        return 0
    session.run(cypher, rows=rows).consume()
    return len(rows)


# ---------------------------------------------------------------------------
# Row payload construction
# ---------------------------------------------------------------------------


def _node_to_row(
    node_id: str, node_type: str, properties: dict[str, Any], now_iso: str
) -> dict[str, Any]:
    """Build the SET payload for ONE node — mirrors Neo4jGraphStore.upsert_node.

    Returned dict has only Neo4j-storable scalar/string values (no
    nested maps) — the SCD-2 fields the schema requires plus the
    JSON-serialised property bags the read path expects. Keep this in
    sync with ``src/trellis/stores/neo4j/graph.py:_node_props_to_dict``.
    """
    return {
        "node_id": node_id,
        "version_id": generate_ulid(),
        "node_type": node_type,
        "node_role": "semantic",
        "generation_spec_json": None,
        "document_ids_json": None,
        "properties_json": json.dumps(properties or {}),
        "created_at": now_iso,
        "updated_at": now_iso,
        "valid_from": now_iso,
        "valid_to": None,
    }


def _edge_to_row(
    source_id: str, target_id: str, edge_type: str, props: dict[str, Any], now_iso: str
) -> dict[str, Any]:
    """Build the SET payload for ONE edge — mirrors Neo4jGraphStore.upsert_edge."""
    edge_id = generate_ulid()
    return {
        "source_id": source_id,
        "target_id": target_id,
        "props": {
            "edge_id": edge_id,
            "version_id": generate_ulid(),
            "source_id": source_id,
            "target_id": target_id,
            "edge_type": edge_type,
            "properties_json": json.dumps(props or {}),
            "created_at": now_iso,
            "valid_from": now_iso,
            "valid_to": None,
        },
    }


# ---------------------------------------------------------------------------
# Main load
# ---------------------------------------------------------------------------


def _ingest(
    session: Session, graph: GeneratedGraph, *, batch_size: int
) -> tuple[float, float]:
    """Run all three ingest phases in one session. Return (node_secs, edge_secs)."""
    now_iso = _now_iso()

    # Phase 1: nodes ------------------------------------------------------
    node_rows = [
        _node_to_row(n.node_id, n.node_type, n.properties, now_iso)
        for n in graph.nodes
    ]
    print(f"\nIngesting {len(node_rows)} nodes (batch={batch_size})...")
    t0 = time.perf_counter()
    written = 0
    for batch_idx, batch in enumerate(_batched(node_rows, batch_size), start=1):
        written += _bulk_upsert_nodes(session, batch)
        if batch_idx % PROGRESS_BATCHES == 0:
            elapsed = time.perf_counter() - t0
            print(
                f"  {written}/{len(node_rows)} nodes "
                f"— {written / elapsed:.0f}/sec"
            )
    node_seconds = time.perf_counter() - t0

    # Phase 2: vectors ----------------------------------------------------
    vector_rows = [
        {
            "item_id": n.node_id,
            "vector": n.embedding,
            "meta_json": json.dumps({"node_type": n.node_type}),
        }
        for n in graph.nodes
        if n.embedding is not None
    ]
    if vector_rows:
        print(f"\nAttaching {len(vector_rows)} embeddings...")
        tv = time.perf_counter()
        for batch in _batched(vector_rows, batch_size):
            _bulk_upsert_vectors(session, batch)
        print(f"  {len(vector_rows)} embeddings — {time.perf_counter() - tv:.1f}s")

    # Phase 3: edges ------------------------------------------------------
    edge_rows = [
        _edge_to_row(e.source_id, e.target_id, e.edge_type, e.properties, now_iso)
        for e in graph.edges
    ]
    print(f"\nIngesting {len(edge_rows)} edges (batch={batch_size})...")
    t1 = time.perf_counter()
    written = 0
    for batch_idx, batch in enumerate(_batched(edge_rows, batch_size), start=1):
        written += _bulk_upsert_edges(session, batch)
        if batch_idx % PROGRESS_BATCHES == 0:
            elapsed = time.perf_counter() - t1
            print(
                f"  {written}/{len(edge_rows)} edges "
                f"— {written / elapsed:.0f}/sec"
            )
    edge_seconds = time.perf_counter() - t1

    return node_seconds, edge_seconds


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load the deterministic eval dataset into AuraDB."
    )
    parser.add_argument("--seed", type=int, default=0, help="Generator seed.")
    parser.add_argument("--node-count", type=int, default=5_000)
    parser.add_argument("--edge-count", type=int, default=20_000)
    parser.add_argument("--embedding-count", type=int, default=1_000)
    parser.add_argument("--embedding-dim", type=int, default=16)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Rows per UNWIND batch (default 200).",
    )
    parser.add_argument(
        "--no-wipe",
        action="store_true",
        help=(
            "Skip the pre-load DETACH DELETE + test-index drop. WARNING: "
            "the bulk path does not deduplicate; re-loading without a wipe "
            "creates duplicate current rows."
        ),
    )
    args = parser.parse_args()

    _load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    uri, user, password, database = _resolve_neo4j_creds()

    print(f"AuraDB: {uri} (database={database})")
    print(
        f"Plan: seed={args.seed} nodes={args.node_count} edges={args.edge_count} "
        f"embeddings={args.embedding_count} dim={args.embedding_dim}"
    )

    print("\nGenerating synthetic graph (deterministic, seeded)...")
    graph = generate_graph(
        seed=args.seed,
        node_count=args.node_count,
        edge_count=args.edge_count,
        embedding_count=args.embedding_count,
        embedding_dim=args.embedding_dim,
    )
    print(
        f"  generated: {len(graph.nodes)} nodes, {len(graph.edges)} edges, "
        f"{args.embedding_count} embeddings"
    )

    with (
        GraphDatabase.driver(uri, auth=(user, password)) as driver,
        driver.session(database=database) as session,
    ):
        if not args.no_wipe:
            print("\nWiping existing data + dropping leftover test indexes...")
            wipe = _wipe_and_drop_test_index(session)
            print(
                f"  pre-load: {wipe['nodes']} nodes / {wipe['edges']} edges / "
                f"{wipe['indexes_dropped']} indexes dropped"
            )

        # Create the vector index up front so embeddings get indexed as
        # we write them, not in a follow-up backfill. Index name matches
        # Neo4jVectorStore's default so subsequent eval scenarios find it.
        _ensure_vector_index(
            session,
            name="trellis_node_embeddings",
            dimensions=args.embedding_dim,
        )

        node_seconds, edge_seconds = _ingest(
            session, graph, batch_size=args.batch_size
        )

        print("\nIngest summary:")
        if node_seconds > 0:
            print(
                f"  nodes: {node_seconds:.1f}s — "
                f"{len(graph.nodes) / node_seconds:.0f} nodes/sec"
            )
        if edge_seconds > 0:
            print(
                f"  edges: {edge_seconds:.1f}s — "
                f"{len(graph.edges) / edge_seconds:.0f} edges/sec"
            )

        print("\nVerifying load against AuraDB...")
        verify = _verify_post_load(
            session, vector_index_name="trellis_node_embeddings"
        )
        for k, v in verify.items():
            print(f"  {k}: {v}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
