"""The graph axis's selection contract (#371).

`GraphSearch` has two branches and only one of them is a search. These
tests pin which one runs when, that the recency branch is genuinely
query-independent rather than merely undertested, and that a seeding miss
degrades to the recency branch instead of costing the caller an axis.

They also pin the two wiring decisions `build_strategies` makes: the graph
axis is always built (no embedder required), and a resolvable
``embedding_fn`` never silently turns on seeding.
"""

from __future__ import annotations

from typing import Any

import pytest

from trellis.retrieve.pack_builder import _item_attribution
from trellis.retrieve.strategies import (
    GRAPH_SELECTION_RECENCY_WINDOW,
    GRAPH_SELECTION_SEEDED,
    GraphSearch,
    GraphSeedExtractor,
    SemanticSearch,
    build_strategies,
)
from trellis.schemas.pack import PackItem


class _RecordingGraphStore:
    """Records how it was called so a test can assert on the query path."""

    def __init__(self, nodes: list[dict[str, Any]] | None = None) -> None:
        self._nodes = nodes if nodes is not None else _default_nodes()
        self.query_calls: list[dict[str, Any]] = []
        self.subgraph_calls: list[dict[str, Any]] = []

    def query(
        self,
        node_type: str | None = None,
        properties: dict[str, Any] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.query_calls.append(
            {"node_type": node_type, "properties": properties, "limit": limit}
        )
        return list(self._nodes)

    def execute_node_query(self, query: Any) -> list[dict[str, Any]]:
        self.query_calls.append({"dsl": query, "limit": query.limit})
        return list(self._nodes)

    def get_subgraph(
        self,
        seed_ids: list[str],
        depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> dict[str, Any]:
        self.subgraph_calls.append(
            {"seed_ids": list(seed_ids), "depth": depth, "edge_types": edge_types}
        )
        return {
            "nodes": [
                {
                    "node_id": f"seeded-{s}",
                    "node_type": "concept",
                    "properties": {"name": f"seeded {s}"},
                }
                for s in seed_ids
            ],
            "edges": [],
        }


def _default_nodes() -> list[dict[str, Any]]:
    return [
        {
            "node_id": f"n{i}",
            "node_type": "concept",
            "properties": {"name": f"node {i}"},
        }
        for i in range(3)
    ]


class _StubExtractor:
    """Minimal :class:`GraphSeedExtractor` that records what it was asked."""

    def __init__(
        self,
        seeds: list[str] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self._seeds = seeds or []
        self._raises = raises
        self.intents: list[str] = []

    def extract(self, intent: str) -> list[str]:
        self.intents.append(intent)
        if self._raises:
            msg = "embedder unavailable"
            raise RuntimeError(msg)
        return list(self._seeds)


class TestSelectionStamp:
    """Which branch ran is a fact about the pack, not about the wiring."""

    def test_unseeded_items_are_stamped_recency_window(self) -> None:
        store = _RecordingGraphStore()
        items = GraphSearch(store).search("anything")
        assert items
        assert all(
            i.metadata["graph_selection"] == GRAPH_SELECTION_RECENCY_WINDOW
            for i in items
        )

    def test_seeded_items_are_stamped_seeded(self) -> None:
        store = _RecordingGraphStore()
        items = GraphSearch(store).search("anything", filters={"seed_ids": ["a"]})
        assert items
        assert all(
            i.metadata["graph_selection"] == GRAPH_SELECTION_SEEDED for i in items
        )

    def test_stored_property_cannot_misreport_the_selection(self) -> None:
        """Entity types are open strings, so a node may carry this key.

        How *this* search chose its candidates is not something a stored
        property gets a vote on.
        """
        store = _RecordingGraphStore(
            [
                {
                    "node_id": "liar",
                    "node_type": "concept",
                    "properties": {
                        "name": "n",
                        "graph_selection": GRAPH_SELECTION_SEEDED,
                    },
                }
            ]
        )
        items = GraphSearch(store).search("anything")
        assert items[0].metadata["graph_selection"] == GRAPH_SELECTION_RECENCY_WINDOW

    def test_attribution_forwards_the_stamp_to_the_pack_event(self) -> None:
        item = PackItem(
            item_id="n1",
            item_type="entity",
            excerpt="x",
            relevance_score=1.0,
            metadata={"graph_selection": GRAPH_SELECTION_RECENCY_WINDOW},
        )
        assert (
            _item_attribution(item)["graph_selection"] == GRAPH_SELECTION_RECENCY_WINDOW
        )

    def test_attribution_omits_the_stamp_for_non_graph_items(self) -> None:
        item = PackItem(
            item_id="d1",
            item_type="document",
            excerpt="x",
            relevance_score=1.0,
            metadata={"source_strategy": "keyword"},
        )
        assert "graph_selection" not in _item_attribution(item)


class TestRecencyWindowIsQueryIndependent:
    """Not an accident of test coverage — the branch has no query input."""

    def test_different_queries_return_identical_items(self) -> None:
        store = _RecordingGraphStore()
        strategy = GraphSearch(store)
        a = strategy.search("how do I rotate the signing key")
        b = strategy.search("what did the kids eat for breakfast")
        assert [i.item_id for i in a] == [i.item_id for i in b]
        assert [i.relevance_score for i in a] == [i.relevance_score for i in b]

    def test_no_call_carries_the_query_text(self) -> None:
        store = _RecordingGraphStore()
        GraphSearch(store).search("a very distinctive phrase")
        assert store.query_calls
        assert not any(
            "a very distinctive phrase" in repr(call) for call in store.query_calls
        )

    def test_candidate_window_is_a_fixed_row_count(self) -> None:
        """The over-fetch is the whole window, so it scales with ``limit``.

        Pinned because it is the number that makes coverage decay as 1/N as
        the graph grows — nothing about it is a function of graph size.
        """
        store = _RecordingGraphStore()
        GraphSearch(store).search("q", limit=7)
        assert store.query_calls[-1]["limit"] == 28


class TestSeedExtractorWiring:
    def test_extractor_receives_the_query_and_seeds_the_subgraph(self) -> None:
        store = _RecordingGraphStore()
        extractor = _StubExtractor(["e1", "e2"])
        items = GraphSearch(store, seed_extractor=extractor).search("rotate the key")
        assert extractor.intents == ["rotate the key"]
        assert store.subgraph_calls[0]["seed_ids"] == ["e1", "e2"]
        assert not store.query_calls
        assert {i.item_id for i in items} == {"seeded-e1", "seeded-e2"}

    def test_empty_seed_set_falls_back_to_the_recency_window(self) -> None:
        """A seeding miss must not cost the caller an axis it had before."""
        store = _RecordingGraphStore()
        items = GraphSearch(store, seed_extractor=_StubExtractor([])).search("q")
        assert store.query_calls
        assert not store.subgraph_calls
        assert items
        assert items[0].metadata["graph_selection"] == GRAPH_SELECTION_RECENCY_WINDOW

    def test_raising_extractor_falls_back_rather_than_failing_the_pack(self) -> None:
        store = _RecordingGraphStore()
        strategy = GraphSearch(store, seed_extractor=_StubExtractor(raises=True))
        items = strategy.search("q")
        assert items
        assert items[0].metadata["graph_selection"] == GRAPH_SELECTION_RECENCY_WINDOW

    def test_explicit_seed_ids_win_over_the_extractor(self) -> None:
        """The neighbourhood routes asked for a specific set; honour it."""
        store = _RecordingGraphStore()
        extractor = _StubExtractor(["derived"])
        GraphSearch(store, seed_extractor=extractor).search(
            "q", filters={"seed_ids": ["explicit"]}
        )
        assert extractor.intents == []
        assert store.subgraph_calls[0]["seed_ids"] == ["explicit"]

    def test_depth_and_edge_types_still_reach_the_store_when_derived(self) -> None:
        store = _RecordingGraphStore()
        GraphSearch(store, seed_extractor=_StubExtractor(["e1"])).search(
            "q", filters={"depth": 1, "edge_types": ["mentions"]}
        )
        assert store.subgraph_calls[0]["depth"] == 1
        assert store.subgraph_calls[0]["edge_types"] == ["mentions"]

    def test_stub_satisfies_the_protocol(self) -> None:
        assert isinstance(_StubExtractor(), GraphSeedExtractor)


class _Knowledge:
    def __init__(self) -> None:
        self.document_store = object()
        self.graph_store = _RecordingGraphStore()
        self.vector_store = object()


class _Registry:
    """Stand-in for StoreRegistry.

    Deliberately not a MagicMock: ``build_strategies`` reads
    ``getattr(registry, "embedding_fn", None)`` and a mock answers every
    attribute truthfully-shaped, which would make the no-embedder case
    untestable.
    """

    def __init__(self, embedding_fn: Any | None = None) -> None:
        self.knowledge = _Knowledge()
        if embedding_fn is not None:
            self.embedding_fn = embedding_fn


def _graph_axis(strategies: list[Any]) -> GraphSearch:
    graph = [s for s in strategies if isinstance(s, GraphSearch)]
    assert len(graph) == 1
    return graph[0]


class TestBuildStrategiesSeeding:
    def test_no_embedder_deployment_still_gets_an_unseeded_graph_axis(self) -> None:
        strategies = build_strategies(_Registry())  # type: ignore[arg-type]
        assert not any(isinstance(s, SemanticSearch) for s in strategies)
        assert _graph_axis(strategies)._seed_extractor is None

    def test_a_resolvable_embedder_does_not_turn_seeding_on(self) -> None:
        """Pins the #371 refusal.

        Deriving the extractor from ``embedding_fn`` was measured against
        the reference deployment's own stores and produced 0 seeds on 37/37
        real intents, while coupling an axis documented as always-available
        to an optional dependency. Both halves of that are behaviour, so
        both are pinned.
        """
        strategies = build_strategies(_Registry(lambda _text: [0.1, 0.2]))  # type: ignore[arg-type]
        assert any(isinstance(s, SemanticSearch) for s in strategies)
        assert _graph_axis(strategies)._seed_extractor is None

    def test_explicit_extractor_is_wired_through(self) -> None:
        extractor = _StubExtractor(["e1"])
        strategies = build_strategies(
            _Registry(),  # type: ignore[arg-type]
            graph_seed_extractor=extractor,
        )
        assert _graph_axis(strategies)._seed_extractor is extractor

    def test_explicit_extractor_needs_no_embedder(self) -> None:
        """Seeding and the semantic axis are independent capabilities."""
        strategies = build_strategies(
            _Registry(),  # type: ignore[arg-type]
            graph_seed_extractor=_StubExtractor(["e1"]),
        )
        assert not any(isinstance(s, SemanticSearch) for s in strategies)
        items = _graph_axis(strategies).search("q")
        assert items[0].metadata["graph_selection"] == GRAPH_SELECTION_SEEDED


@pytest.mark.parametrize("seeds", [[], ["a"]])
def test_selection_stamp_is_always_present(seeds: list[str]) -> None:
    """Neither branch may serve an item whose selection mode is unknown."""
    store = _RecordingGraphStore()
    filters = {"seed_ids": seeds} if seeds else None
    items = GraphSearch(store).search("q", filters=filters)
    assert items
    assert all("graph_selection" in i.metadata for i in items)
