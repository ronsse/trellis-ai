"""Graph-axis control keys must not reach a document or vector store.

``PackBuilder.build`` hands **one** ``filters`` mapping to every strategy,
and injects ``include_structural`` into it. :class:`GraphSearch` ``pop``\\ s
the control keys it owns; the document and vector stores have never heard of
them and compile an unknown key to hard metadata equality, which matches no
row. So asking for one extra *category of graph node* silently emptied the
keyword and semantic axes.

The failure is the #404 shape one layer up: the strategies return ``[]``,
which is byte-identical to "nothing matched" — no warning, no
``strategy_failures`` entry, no ``RejectedItem``, nothing for
``summarize_withheld`` to see.

Latent since the initial commit because nothing in the repository passed one.
#375/#436 made it reachable **by following the documentation**: a
newly-written meta-Activity is minted ``node_role="structural"``, so
``PackBuilder.build``'s own docstring and ``trellis.meta.recorder``'s module
docstring both now instruct an operator to pass ``include_structural=True``
alongside ``include_meta=True``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import (
    GRAPH_CONTROL_FILTER_KEYS,
    GraphSearch,
    KeywordSearch,
    SemanticSearch,
    strip_graph_controls,
)
from trellis.schemas import well_known as wk
from trellis.schemas.pack import PackBudget
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path


def _embed(text: str) -> list[float]:
    """Deterministic 3-d toy embedding — enough for ordering, not meaning."""
    return [1.0, (sum(ord(c) for c in text) % 97) / 97.0, 0.5]


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    reg.knowledge.graph_store.upsert_node(
        node_id="n1",
        node_type=wk.OBSERVATION,
        properties={"name": "graph note", "description": "caching in the graph"},
    )
    reg.knowledge.document_store.put(
        "d1", "how to fix the caching layer", {"title": "caching doc"}
    )
    reg.knowledge.vector_store.upsert(
        "v1",
        _embed("how to fix the caching layer"),
        metadata={"excerpt": "how to fix the caching layer", "title": "caching vec"},
    )
    return reg


def _three_axis_builder(registry: StoreRegistry) -> PackBuilder:
    return PackBuilder(
        strategies=[
            KeywordSearch(registry.knowledge.document_store),
            SemanticSearch(registry.knowledge.vector_store, _embed),
            GraphSearch(registry.knowledge.graph_store),
        ]
    )


class TestStripGraphControls:
    """The helper itself, including the ``{}`` → ``None`` coercion."""

    def test_control_keys_are_dropped(self) -> None:
        assert strip_graph_controls(
            {"include_structural": True, "domain": "infra"}
        ) == {"domain": "infra"}

    def test_metadata_keys_are_untouched(self) -> None:
        payload = {"category": "gotcha", "source": "cli"}
        assert strip_graph_controls(dict(payload)) == payload

    def test_a_controls_only_mapping_becomes_none(self) -> None:
        """``{}`` is a predicate over nothing to some backends; ``None``
        is the "no filters" the store ABCs actually document."""
        assert strip_graph_controls({"include_structural": True}) is None

    @pytest.mark.parametrize("empty", [None, {}])
    def test_empty_input_passes_through(self, empty: dict[str, Any] | None) -> None:
        assert strip_graph_controls(empty) == empty

    def test_the_key_set_matches_what_graphsearch_pops(self) -> None:
        """A control key GraphSearch pops but this set omits is the bug
        again, for the next key."""
        assert set(GRAPH_CONTROL_FILTER_KEYS) == {
            "include_structural",
            "include_unconfirmed",
            "seed_ids",
        }


class TestAxesSurviveTheControlKey:
    """The whole point: one extra graph category must not cost two axes."""

    def test_include_structural_does_not_empty_the_pack(
        self, registry: StoreRegistry
    ) -> None:
        builder = _three_axis_builder(registry)
        budget = PackBudget(max_items=50, max_tokens=100_000)

        baseline = builder.build("caching", budget=budget)
        widened = builder.build("caching", budget=budget, include_structural=True)

        assert sorted(i.item_id for i in baseline.items) == ["d1", "n1", "v1"]
        # Before the fix this was ``["n1"]`` — keyword and semantic both
        # returned zero rows, indistinguishably from an empty corpus.
        assert sorted(i.item_id for i in widened.items) == ["d1", "n1", "v1"]

    @pytest.mark.parametrize("key", sorted(GRAPH_CONTROL_FILTER_KEYS))
    def test_a_caller_supplied_control_key_also_survives(
        self, registry: StoreRegistry, key: str
    ) -> None:
        """``build(include_structural=...)`` is not the only way in — a
        caller can put any of these in ``filters=`` directly."""
        builder = _three_axis_builder(registry)
        value: Any = [] if key == "seed_ids" else True
        pack = builder.build(
            "caching",
            budget=PackBudget(max_items=50, max_tokens=100_000),
            filters={key: value},
        )
        assert sorted(i.item_id for i in pack.items) == ["d1", "n1", "v1"]

    def test_keyword_axis_alone(self, registry: StoreRegistry) -> None:
        items = KeywordSearch(registry.knowledge.document_store).search(
            "caching", filters={"include_structural": True}
        )
        assert [i.item_id for i in items] == ["d1"]

    def test_semantic_axis_alone(self, registry: StoreRegistry) -> None:
        items = SemanticSearch(registry.knowledge.vector_store, _embed).search(
            "caching", filters={"include_structural": True}
        )
        assert [i.item_id for i in items] == ["v1"]


class TestRealMetadataFiltersStillApply:
    """Stripping controls must not become "ignore filters"."""

    def test_a_genuine_metadata_filter_still_excludes(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put(
            "d2", "caching from another source", {"source_system": "dbt"}
        )
        items = KeywordSearch(registry.knowledge.document_store).search(
            "caching",
            filters={"include_structural": True, "source_system": "dbt"},
        )
        assert [i.item_id for i in items] == ["d2"]

    def test_graph_axis_still_reads_the_control_key(
        self, registry: StoreRegistry
    ) -> None:
        """The keys are stripped for the *stores*, never for GraphSearch."""
        registry.knowledge.graph_store.upsert_node(
            node_id="struct-1",
            node_type=wk.OBSERVATION,
            properties={"name": "plumbing", "description": "a structural row"},
            node_role="structural",
        )
        graph = GraphSearch(registry.knowledge.graph_store)
        assert "struct-1" not in {i.item_id for i in graph.search("caching")}
        assert "struct-1" in {
            i.item_id
            for i in graph.search("caching", filters={"include_structural": True})
        }
