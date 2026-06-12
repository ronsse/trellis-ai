"""GitHub corpus loader smoke — load trellis-ai PR snapshot, print counts.

Run via::

    uv run python -m eval._smoke.github_loader_smoke

No API keys required.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Force stdout to UTF-8 for Windows compatibility before any unicode-emitting import.
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from eval.corpora.github_trellis.loader import (  # noqa: E402,I001 — must follow stdout reconfigure
    build_pr_name_index,
    load_github_corpus,
)
from trellis.stores.registry import StoreRegistry  # noqa: E402


SQLITE_REGISTRY_CONFIG = {
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


def main() -> int:
    with (
        tempfile.TemporaryDirectory() as stores_dir,
        StoreRegistry(
            config=SQLITE_REGISTRY_CONFIG, stores_dir=Path(stores_dir)
        ) as registry,
    ):
        load_result = load_github_corpus(registry)

        print()
        print("=" * 60)
        print("trellis-ai GitHub PR corpus loaded.")
        print()
        print("Counts:")
        for k, v in load_result.as_metrics().items():
            print(f"  {k}: {int(v)}")
        print()

        graph = registry.knowledge.graph_store
        all_nodes = graph.query(limit=500)
        nodes_by_type: dict[str, int] = {}
        for n in all_nodes:
            t = n.get("node_type", "?")
            nodes_by_type[t] = nodes_by_type.get(t, 0) + 1
        print("Nodes by type:")
        for t, c in sorted(nodes_by_type.items()):
            print(f"  {t}: {c}")
        print()

        # Edge kind distribution.
        seen_edge_ids: set[str] = set()
        edge_kinds: dict[str, int] = {}
        for n in all_nodes:
            for e in graph.get_edges(n["node_id"], direction="outgoing"):
                eid = e.get("edge_id")
                if eid in seen_edge_ids:
                    continue
                seen_edge_ids.add(eid)
                kind = e.get("edge_type", "?")
                edge_kinds[kind] = edge_kinds.get(kind, 0) + 1
        print("Edges by kind:")
        for k, c in sorted(edge_kinds.items()):
            print(f"  {k}: {c}")
        print()

        # Spot-check: list outgoing wasInformedBy edges from a recent PR.
        # PR 103 (or whatever the highest is) should have refs.
        sample_pr = max(
            (n for n in all_nodes if n.get("node_type") == "github_pr"),
            key=lambda n: n.get("properties", {}).get("pr_number", 0),
            default=None,
        )
        if sample_pr:
            sample_id = sample_pr["node_id"]
            sample_num = sample_pr["properties"].get("pr_number")
            sample_title = sample_pr["properties"].get("title", "")
            print(f"Spot-check — most recent PR (#{sample_num}): {sample_title[:60]}")
            outgoing = graph.get_edges(sample_id, direction="outgoing")
            for e in outgoing[:10]:
                print(f"  {sample_id} -[{e.get('edge_type')}]-> {e.get('target_id')}")
            print()

        # Name index — used later for seed extraction.
        name_index = build_pr_name_index(registry)
        print(f"Name index entries: {len(name_index)}")
        print(f"Sample entries: {dict(list(name_index.items())[:5])}")
        print()

        # Document store smoke search.
        documents = list(registry.knowledge.document_store.search(query="advisory"))
        print(f"Document store: {len(documents)} matches for 'advisory'")
        for d in documents[:3]:
            print(f"  {d.get('doc_id')}: {(d.get('content') or '')[:80]}...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
