#!/usr/bin/env python3
"""Load the deterministic eval dataset into the live AuraDB instance.

Single-purpose operational script — populates the AuraDB knowledge plane
(`graph_store` + `vector_store`) with the same synthetic graph the eval
scenarios use, so a follow-up `python -m eval.runner --scenario
populated_graph_performance` actually exercises the live backend.

Reads credentials from environment variables. Honours either family —
`TRELLIS_NEO4J_*` (production namespace, what the eval scenarios probe)
or `TRELLIS_TEST_NEO4J_*` (live-test namespace) — and prefers the
production family. Source `.env` before running:

    set -a && source .env && set +a
    python scripts/load_eval_dataset_to_aura.py --node-count 5000

By default it wipes existing nodes + drops the test-namespace vector
index before loading; pass ``--no-wipe`` to skip.

This is a one-off populate operation, not part of the eval harness.
The eval scenarios are read-only against your real backends.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

# Make the repo root importable so `from eval.generators...` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.generators.graph_generator import generate_graph
from neo4j import GraphDatabase

from trellis.stores.registry import StoreRegistry

PROGRESS_INTERVAL = 500  # nodes / edges per progress print


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Don't clobber values already in os.environ — explicit > file.
        os.environ.setdefault(key, value)


def _resolve_neo4j_creds() -> tuple[str, str, str, str]:
    """Prefer TRELLIS_NEO4J_*; fall back to TRELLIS_TEST_NEO4J_*."""
    families = ("TRELLIS_NEO4J_", "TRELLIS_TEST_NEO4J_")
    for prefix in families:
        uri = os.environ.get(f"{prefix}URI")
        user = os.environ.get(f"{prefix}USER")
        password = os.environ.get(f"{prefix}PASSWORD")
        if uri and user and password:
            database = os.environ.get(f"{prefix}DATABASE", "neo4j")
            return uri, user, password, database
    msg = (
        "No Neo4j credentials found. Set TRELLIS_NEO4J_{URI,USER,PASSWORD} "
        "(or TRELLIS_TEST_NEO4J_*) in env or .env before running."
    )
    raise SystemExit(msg)


def _wipe_and_drop_test_index(
    uri: str, user: str, password: str, database: str
) -> dict[str, int]:
    """Detach-delete every node and drop the leftover test vector index.

    Returns a dict of counts before deletion so the script can show
    "wiped N nodes". The Trellis-schema range indexes (alias_*, edge_*,
    node_*, etc.) are left in place — the registry treats them as
    idempotent on re-init.
    """
    counts = {"nodes": 0, "edges": 0, "indexes_dropped": 0}
    with (
        GraphDatabase.driver(uri, auth=(user, password)) as driver,
        driver.session(database=database) as s,
    ):
        counts["nodes"] = s.run("MATCH (n) RETURN count(n) AS c").single()[0]
        counts["edges"] = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()[0]
        s.run("MATCH (n) DETACH DELETE n").consume()
        for idx in ("trellis_test_node_embeddings",):
            try:
                s.run(f"DROP INDEX {idx} IF EXISTS").consume()
                counts["indexes_dropped"] += 1
            except Exception as exc:
                print(f"  could not drop index {idx}: {exc}", file=sys.stderr)
    return counts


def _verify_post_load(
    uri: str, user: str, password: str, database: str, vector_index_name: str
) -> dict[str, object]:
    """Count nodes/edges/indexes after the load so the operator can sanity-check."""
    out: dict[str, object] = {}
    with (
        GraphDatabase.driver(uri, auth=(user, password)) as driver,
        driver.session(database=database) as s,
    ):
        out["total_nodes"] = s.run("MATCH (n) RETURN count(n) AS c").single()[0]
        out["total_edges"] = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()[0]
        out["nodes_with_embedding"] = s.run(
            "MATCH (n) WHERE n.embedding IS NOT NULL RETURN count(n) AS c"
        ).single()[0]
        out["node_types_top_5"] = [
            dict(r)
            for r in s.run(
                "MATCH (n) RETURN n.type AS type, count(*) AS n ORDER BY n DESC LIMIT 5"
            )
        ]
        vector = s.run(
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


def _build_registry(
    sqlite_dir: Path,
    *,
    uri: str,
    user: str,
    password: str,
    database: str,
    embedding_dim: int,
) -> StoreRegistry:
    """Knowledge graph + vector → AuraDB; everything else → tmp SQLite.

    Uses the FLAT config shape because the direct ``StoreRegistry()``
    constructor does NOT apply plane-split normalisation — that only
    runs inside ``from_config_dir``. Passing a plane-split dict here
    silently falls back to SQLite for every store. Tracked in memory:
    ``project_eval_silent_fallback_planesplit.md``.
    """
    config = {
        "graph": {
            "backend": "neo4j",
            "uri": uri,
            "user": user,
            "password": password,
            "database": database,
        },
        "vector": {
            "backend": "neo4j",
            "uri": uri,
            "user": user,
            "password": password,
            "database": database,
            "dimensions": embedding_dim,
        },
        "document": {"backend": "sqlite"},
        "blob": {"backend": "local"},
        "trace": {"backend": "sqlite"},
        "event_log": {"backend": "sqlite"},
    }
    return StoreRegistry(config=config, stores_dir=sqlite_dir)


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
        "--no-wipe",
        action="store_true",
        help="Skip the pre-load DETACH DELETE + test-index drop.",
    )
    args = parser.parse_args()

    _load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    uri, user, password, database = _resolve_neo4j_creds()

    print(f"AuraDB: {uri} (database={database})")
    print(
        f"Plan: seed={args.seed} nodes={args.node_count} edges={args.edge_count} "
        f"embeddings={args.embedding_count} dim={args.embedding_dim}"
    )

    if not args.no_wipe:
        print("\nWiping existing data + dropping leftover test indexes...")
        wipe = _wipe_and_drop_test_index(uri, user, password, database)
        print(
            f"  pre-load: {wipe['nodes']} nodes / {wipe['edges']} edges / "
            f"{wipe['indexes_dropped']} indexes dropped"
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
        tempfile.TemporaryDirectory() as tmp,
        _build_registry(
            Path(tmp),
            uri=uri,
            user=user,
            password=password,
            database=database,
            embedding_dim=args.embedding_dim,
        ) as registry,
    ):
        knowledge = registry.knowledge
        graph_store = knowledge.graph_store
        vector_store = knowledge.vector_store

        print("\nIngesting nodes + embeddings...")
        t0 = time.perf_counter()
        for i, node in enumerate(graph.nodes, start=1):
            graph_store.upsert_node(
                node_id=node.node_id,
                node_type=node.node_type,
                properties=node.properties,
            )
            if node.embedding is not None:
                vector_store.upsert(
                    item_id=node.node_id,
                    vector=node.embedding,
                    metadata={"node_type": node.node_type},
                )
            if i % PROGRESS_INTERVAL == 0:
                elapsed = time.perf_counter() - t0
                rate = i / elapsed if elapsed > 0 else 0.0
                print(f"  {i}/{len(graph.nodes)} nodes — {rate:.1f}/sec")
        node_seconds = time.perf_counter() - t0

        print("\nIngesting edges...")
        t1 = time.perf_counter()
        for i, edge in enumerate(graph.edges, start=1):
            graph_store.upsert_edge(
                source_id=edge.source_id,
                target_id=edge.target_id,
                edge_type=edge.edge_type,
                properties=edge.properties,
            )
            if i % PROGRESS_INTERVAL == 0:
                elapsed = time.perf_counter() - t1
                rate = i / elapsed if elapsed > 0 else 0.0
                print(f"  {i}/{len(graph.edges)} edges — {rate:.1f}/sec")
        edge_seconds = time.perf_counter() - t1

    print("\nIngest summary:")
    print(
        f"  nodes: {node_seconds:.1f}s — "
        f"{len(graph.nodes) / node_seconds:.1f} nodes/sec"
    )
    print(
        f"  edges: {edge_seconds:.1f}s — "
        f"{len(graph.edges) / edge_seconds:.1f} edges/sec"
    )

    print("\nVerifying load against AuraDB...")
    verify = _verify_post_load(
        uri, user, password, database, vector_index_name="trellis_node_embeddings"
    )
    for k, v in verify.items():
        print(f"  {k}: {v}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
