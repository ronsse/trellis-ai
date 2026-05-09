"""Search-strategy wrappers shared by Tier-2 corpus convergence scenarios."""

from __future__ import annotations

from typing import Any

from trellis.retrieve.strategies import GraphSearch, SearchStrategy


class _SeededGraphSearch(SearchStrategy):
    """Seeded GraphSearch with doc-prefixed item_ids for cross-strategy dedup.

    Two responsibilities, both small:

    1. **Seed gating.** Returns empty when ``filters["seed_ids"]`` is
       absent or empty — avoids GraphSearch's no-seeds fallback of
       returning every non-structural node, which floods the pack on
       small/medium corpora (the dbt scenario has 21 entities; the
       github scenario has ~90).

    2. **item_id canonicalization for dedup.** GraphSearch emits
       ``PackItem.item_id == node_id`` (e.g.,
       ``"model.jaffle_shop.customers"``). KeywordSearch and
       SemanticSearch emit ``PackItem.item_id == "doc:" + node_id``
       (e.g., ``"doc:model.jaffle_shop.customers"``). Without
       canonicalization, the same entity appears in the pack twice
       and PackBuilder's exact-match dedup keeps both — wasting
       budget on cross-strategy duplicates.

       This wrapper rewrites each GraphSearch PackItem's ``item_id``
       to ``"doc:" + node_id`` so PackBuilder's existing dedup
       collapses cross-strategy hits. The original ``node_id`` is
       preserved in ``metadata["graph_node_id"]`` for any downstream
       consumer that wants the raw form.
    """

    def __init__(self, graph_store: Any) -> None:
        self._inner = GraphSearch(graph_store)

    @property
    def name(self) -> str:
        return "graph_seeded"

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        if not filters:
            return []
        seed_ids = filters.get("seed_ids")
        if not seed_ids:
            return []
        items = self._inner.search(query, limit=limit, filters=filters)
        rewritten = []
        for item in items:
            if item.item_id.startswith("doc:"):
                rewritten.append(item)
                continue
            new_metadata = {
                **(item.metadata or {}),
                "graph_node_id": item.item_id,
            }
            rewritten.append(
                item.model_copy(
                    update={
                        "item_id": f"doc:{item.item_id}",
                        "metadata": new_metadata,
                    }
                )
            )
        return rewritten
