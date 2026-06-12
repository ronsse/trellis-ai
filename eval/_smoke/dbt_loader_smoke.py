"""dbt loader smoke — load Jaffle Shop into an in-memory registry, print counts.

Run via::

    uv run python -m eval._smoke.dbt_loader_smoke

No API keys required — this validates the corpus loader without
touching any LLM provider. Phase B-1 wiring step 1 of N.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Force stdout to UTF-8 for Windows compatibility (parity with phase_a_smoke).
# Must run before any non-stdlib import that may emit unicode at module-level.
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from eval.corpora.dbt_loader import load_jaffle_shop_corpus  # noqa: E402,I001 — must follow stdout reconfigure above
from trellis.stores.registry import StoreRegistry  # noqa: E402

# Description preview cutoff for sample-entity output.
_DESCRIPTION_PREVIEW_CHARS = 80


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


def main() -> int:  # noqa: PLR0912 — top-level demo orchestrator, single linear flow
    with (
        tempfile.TemporaryDirectory() as stores_dir,
        StoreRegistry(
            config=SQLITE_REGISTRY_CONFIG, stores_dir=Path(stores_dir)
        ) as registry,
    ):
        load_result = load_jaffle_shop_corpus(registry)

        print()
        print("=" * 60)
        print("Jaffle Shop corpus loaded.")
        print()
        print("Counts:")
        for k, v in load_result.as_metrics(prefix="corpus").items():
            print(f"  {k}: {int(v)}")
        print()

        # Inspect the graph. List a few node types + a few edges to
        # confirm the canonical edge_kind landed.
        graph = registry.knowledge.graph_store
        all_nodes = graph.query()
        nodes_by_type: dict[str, int] = {}
        for n in all_nodes:
            t = n.get("node_type", "?")
            nodes_by_type[t] = nodes_by_type.get(t, 0) + 1
        print("Nodes by type:")
        for t, c in sorted(nodes_by_type.items()):
            print(f"  {t}: {c}")
        print()

        # Inspect a few specific entities to verify property structure.
        print("Sample entities:")
        for entity_id in (
            "model.jaffle_shop.customers",
            "model.jaffle_shop.stg_orders",
            "source.jaffle_shop.raw.payments",
        ):
            node = graph.get_node(entity_id)
            if node:
                desc = (node.get("properties") or {}).get("description", "")
                print(f"  {entity_id}:")
                print(f"    type: {node.get('node_type')}")
                truncated = "..." if len(desc) > _DESCRIPTION_PREVIEW_CHARS else ""
                print(
                    f"    description: {desc[:_DESCRIPTION_PREVIEW_CHARS]}{truncated}"
                )
            else:
                print(f"  {entity_id}: NOT FOUND")
        print()

        # Inspect edge_kind distribution. The fixture has ~22 depends_on
        # edges; after canonicalization they should all be "dependsOn".
        # GraphStore exposes get_edges(node_id) — iterate through nodes
        # to enumerate all edges. Dedupe by edge_id.
        seen_edge_ids: set[str] = set()
        all_edges: list[dict] = []
        for n in all_nodes:
            for e in graph.get_edges(n["node_id"], direction="outgoing"):
                eid = e.get("edge_id")
                if eid not in seen_edge_ids:
                    seen_edge_ids.add(eid)
                    all_edges.append(e)
        edge_kinds: dict[str, int] = {}
        for e in all_edges:
            kind = e.get("edge_type", "?")
            edge_kinds[kind] = edge_kinds.get(kind, 0) + 1
        print("Edges by kind:")
        for k, c in sorted(edge_kinds.items()):
            print(f"  {k}: {c}")
        print()

        # Spot-check: customers mart should depend on 3 staging models.
        print("Edge spot-check — model.jaffle_shop.customers outbound edges:")
        customers_edges = graph.get_edges(
            "model.jaffle_shop.customers", direction="outgoing"
        )
        for e in customers_edges:
            print(
                f"  {e.get('source_id')} -[{e.get('edge_type')}]-> {e.get('target_id')}"
            )
        print()

        # Document store check.
        documents = list(registry.knowledge.document_store.search(query="customer"))
        print(f"Document store: {len(documents)} matches for 'customer'")
        if documents:
            for d in documents[:3]:
                print(f"  {d.get('doc_id')}: {(d.get('content') or '')[:60]}...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
