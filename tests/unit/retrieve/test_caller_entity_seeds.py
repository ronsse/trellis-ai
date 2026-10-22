"""A sectioned pack's ``entity_ids`` seed the graph axis.

``SectionRequest.entity_ids`` has always routed items to a section
(``TierMapper``). It named nothing to the graph axis, so a caller that
passed the entity it was about to touch still got the newest-nodes window
and a precedent attached to that entity was unreachable once enough newer
nodes existed. ``PackBuilder.build_sectioned`` now hands the union of the
sections' ids to :class:`GraphSearch` as seeds, alongside whatever the
:class:`NamespaceSeedExtractor` derives from the intent.

The properties pinned here, in the order a regression would hurt:

* the named entity's attached precedent is served, and says why
  (``graph_selection == "caller_seeded"``);
* a pack that names no entity builds exactly as it did;
* naming an entity the graph does not hold costs nothing;
* the extractor keeps contributing, and an explicit ``seed_ids`` still wins;
* the control key never reaches a document or vector store, which would
  read it as hard metadata equality and empty the axis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from trellis.retrieve.builder_factory import GRAPH_SEEDING_ENV, build_pack_builder
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import (
    DEFAULT_MAX_SEEDS,
    EXTRACTED_SEED_DEPTH,
    GRAPH_CALLER_SEED_IDS_KEY,
    GRAPH_CONTROL_FILTER_KEYS,
    GRAPH_SELECTION_CALLER_SEEDED,
    GRAPH_SELECTION_RECENCY_WINDOW,
    GRAPH_SELECTION_SEEDED,
    GRAPH_SLICE_PRIORITISED_KEY,
    GraphSearch,
    KeywordSearch,
)
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path

#: Names nothing the extractor can resolve.
BLIND_INTENT = "do the next step"

ENTITY = "tbl:ledger"
PRECEDENT = "precedent:ledger-rule"
OTHER_ENTITY = "tbl:orders"
OTHER_PRECEDENT = "precedent:orders-rule"

#: More than ``limit`` (20) newer nodes, so the recency window cannot reach
#: anything written before them.
_NEWER = 30

#: A named hub with more neighbours than any graph-axis ``limit`` the
#: sectioned builder uses, so its neighbourhood is sliced.
_HUB_NEIGHBOURS = 60
LATE_PRECEDENT = "precedent:zz-late-rule"


@pytest.fixture(autouse=True)
def _seeding_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GRAPH_SEEDING_ENV, raising=False)


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _node(
    registry: StoreRegistry, node_id: str, node_type: str, name: str | None = None
) -> None:
    registry.knowledge.graph_store.upsert_node(
        node_id,
        node_type,
        {"name": name or node_id, "description": f"{name or node_id} " * 8},
    )


def _precedent_on(registry: StoreRegistry, entity: str, precedent: str) -> None:
    _node(registry, entity, "table")
    _node(registry, precedent, "precedent")
    registry.knowledge.graph_store.upsert_edge(
        precedent, entity, "precedent_applies_to"
    )


def _bury(registry: StoreRegistry) -> None:
    """Write enough newer nodes that the recency window reaches none of the
    nodes written before this call."""
    for i in range(_NEWER):
        _node(registry, f"concept:filler-{i}", "concept", f"filler {i}")


def _build(
    registry: StoreRegistry,
    intent: str = BLIND_INTENT,
    *,
    entity_ids: list[str] | None = None,
    sections: list[SectionRequest] | None = None,
) -> dict[str, PackItem]:
    """Every served item of a sectioned pack, by id."""
    builder = build_pack_builder(registry, surface="test")
    sections = sections or [SectionRequest(name="all", entity_ids=entity_ids or [])]
    pack = builder.build_sectioned(intent, sections=sections)
    return {item.item_id: item for s in pack.sections for item in s.items}


class TestNamedEntitySeedsTheGraphAxis:
    def test_the_attached_precedent_is_served_and_says_why(
        self, registry: StoreRegistry
    ) -> None:
        _precedent_on(registry, ENTITY, PRECEDENT)
        _bury(registry)

        served = _build(registry, entity_ids=[ENTITY])

        assert PRECEDENT in served
        assert ENTITY in served
        assert (
            served[PRECEDENT].metadata["graph_selection"]
            == GRAPH_SELECTION_CALLER_SEEDED
        )

    def test_a_named_hub_keeps_a_late_sorting_precedent(
        self, registry: StoreRegistry
    ) -> None:
        """A caller-named hub whose neighbourhood overflows the limit gets the
        same precedent partition a derived seed does. The stores return the
        neighbourhood in ``(depth, node_id)`` order and the precedent's id
        sorts after every neighbour's, so slicing it as returned drops it."""
        graph = registry.knowledge.graph_store
        _node(registry, ENTITY, "table")
        for i in range(_HUB_NEIGHBOURS):
            _node(registry, f"concept:hub-{i:03d}", "concept", f"hub {i}")
            graph.upsert_edge(f"concept:hub-{i:03d}", ENTITY, "related_to")
        _node(registry, LATE_PRECEDENT, "precedent")
        graph.upsert_edge(LATE_PRECEDENT, ENTITY, "precedent_applies_to")

        served = _build(registry, entity_ids=[ENTITY])

        assert LATE_PRECEDENT in served
        meta = served[LATE_PRECEDENT].metadata
        assert meta["graph_selection"] == GRAPH_SELECTION_CALLER_SEEDED
        assert meta[GRAPH_SLICE_PRIORITISED_KEY] is True

    def test_the_control_without_entity_ids_buries_it(
        self, registry: StoreRegistry
    ) -> None:
        """Same graph, same intent: the precedent really is out of reach."""
        _precedent_on(registry, ENTITY, PRECEDENT)
        _bury(registry)

        served = _build(registry)

        assert PRECEDENT not in served
        graph_stamps = {
            i.metadata["graph_selection"]
            for i in served.values()
            if i.metadata.get("source_strategy") == "graph"
        }
        assert graph_stamps == {GRAPH_SELECTION_RECENCY_WINDOW}

    def test_ids_are_unioned_across_sections(self, registry: StoreRegistry) -> None:
        _precedent_on(registry, ENTITY, PRECEDENT)
        _precedent_on(registry, OTHER_ENTITY, OTHER_PRECEDENT)
        _bury(registry)

        served = _build(
            registry,
            sections=[
                SectionRequest(name="a", entity_ids=[ENTITY]),
                SectionRequest(name="b", entity_ids=[OTHER_ENTITY]),
            ],
        )

        assert {PRECEDENT, OTHER_PRECEDENT} <= set(served)

    def test_the_extractor_keeps_contributing(self, registry: StoreRegistry) -> None:
        """Union, not replacement: an intent that names ``domain:fincore``
        and an entity that is named in ``entity_ids`` get both
        neighbourhoods."""
        _precedent_on(registry, ENTITY, PRECEDENT)
        _node(registry, "domain:fincore", "domain", "fincore")
        _node(registry, "act:fincore-1", "activity", "reconcile fincore books")
        registry.knowledge.graph_store.upsert_edge(
            "domain:fincore", "act:fincore-1", "relates_to"
        )
        _bury(registry)

        served = _build(registry, "what happened in fincore", entity_ids=[ENTITY])

        # The stamp describes the expansion (one ``get_subgraph`` call over
        # the whole seed set), not the single item, so it is not asserted
        # per neighbourhood here.
        assert {PRECEDENT, "act:fincore-1"} <= set(served)

    def test_it_applies_with_graph_seeding_switched_off(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TRELLIS_GRAPH_SEEDING`` governs seeds *derived from prose*. An
        entity the caller named is an instruction, not an inference."""
        monkeypatch.setenv(GRAPH_SEEDING_ENV, "off")
        _precedent_on(registry, ENTITY, PRECEDENT)
        _bury(registry)

        assert PRECEDENT in _build(registry, entity_ids=[ENTITY])


class TestAPackThatNamesNothingIsUnchanged:
    def test_no_control_key_is_sent_to_any_strategy(
        self, registry: StoreRegistry
    ) -> None:
        seen: list[dict[str, Any] | None] = []

        class _Spy:
            name = "spy"

            def search(
                self,
                query: str,
                *,
                limit: int = 20,
                filters: dict[str, Any] | None = None,
            ) -> list[PackItem]:
                seen.append(filters)
                return []

        builder = PackBuilder(strategies=[_Spy()])
        builder.build_sectioned(
            "anything",
            sections=[SectionRequest(name="a"), SectionRequest(name="b")],
        )
        builder.build_sectioned(
            "anything",
            sections=[SectionRequest(name="a", entity_ids=["x"])],
        )

        assert seen[0] is None
        assert seen[1] == {GRAPH_CALLER_SEED_IDS_KEY: ["x"]}

    def test_the_graph_axis_still_serves_its_recency_window(
        self, registry: StoreRegistry
    ) -> None:
        _precedent_on(registry, ENTITY, PRECEDENT)
        _bury(registry)

        served = _build(registry, entity_ids=[])

        graph_items = [
            i for i in served.values() if i.metadata.get("source_strategy") == "graph"
        ]
        assert graph_items
        assert {i.metadata["graph_selection"] for i in graph_items} == {
            GRAPH_SELECTION_RECENCY_WINDOW
        }


class TestAnUnknownEntityCostsNothing:
    def test_the_axis_falls_back_to_the_recency_window(
        self, registry: StoreRegistry
    ) -> None:
        """An axis emptied by a typo'd id would be worse than not naming it."""
        _bury(registry)

        served = _build(registry, entity_ids=["tbl:does-not-exist"])

        graph_items = [
            i for i in served.values() if i.metadata.get("source_strategy") == "graph"
        ]
        assert graph_items
        assert {i.metadata["graph_selection"] for i in graph_items} == {
            GRAPH_SELECTION_RECENCY_WINDOW
        }

    def test_an_extractor_seed_is_credited_to_the_extractor(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry, "domain:fincore", "domain", "fincore")
        _node(registry, "act:fincore-1", "activity", "reconcile fincore books")
        registry.knowledge.graph_store.upsert_edge(
            "domain:fincore", "act:fincore-1", "relates_to"
        )

        served = _build(
            registry, "what happened in fincore", entity_ids=["tbl:does-not-exist"]
        )

        assert (
            served["act:fincore-1"].metadata["graph_selection"]
            == GRAPH_SELECTION_SEEDED
        )


class _RecordingStore:
    """Graph store stand-in: answers every seed as a live node, records the call."""

    def __init__(self) -> None:
        self.subgraph_calls: list[dict[str, Any]] = []
        self.query_calls = 0

    def get_subgraph(
        self,
        seed_ids: list[str],
        depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> dict[str, Any]:
        self.subgraph_calls.append({"seed_ids": list(seed_ids), "depth": depth})
        return {
            "nodes": [
                {"node_id": s, "node_type": "concept", "properties": {"name": s}}
                for s in seed_ids
            ],
            "edges": [],
        }

    def execute_node_query(self, query: Any) -> list[dict[str, Any]]:
        self.query_calls += 1
        return []

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.query_calls += 1
        return []


class TestGraphSearchSeedResolution:
    def test_caller_seeds_expand_at_the_derived_depth(self) -> None:
        store = _RecordingStore()

        GraphSearch(store).search("q", filters={GRAPH_CALLER_SEED_IDS_KEY: ["a", "b"]})

        (call,) = store.subgraph_calls
        assert call == {"seed_ids": ["a", "b"], "depth": EXTRACTED_SEED_DEPTH}

    def test_an_explicit_depth_wins(self) -> None:
        store = _RecordingStore()

        GraphSearch(store).search(
            "q", filters={GRAPH_CALLER_SEED_IDS_KEY: ["a"], "depth": 3}
        )

        assert store.subgraph_calls[0]["depth"] == 3

    def test_explicit_seed_ids_win_and_drop_the_caller_seeds(self) -> None:
        store = _RecordingStore()

        GraphSearch(store).search(
            "q",
            filters={"seed_ids": ["named"], GRAPH_CALLER_SEED_IDS_KEY: ["hint"]},
        )

        (call,) = store.subgraph_calls
        assert call["seed_ids"] == ["named"]
        assert call["depth"] == 2

    def test_caller_seeds_precede_extractor_seeds_without_duplicates(self) -> None:
        class _Extractor:
            def extract(self, intent: str) -> list[str]:
                return ["derived", "a"]

        store = _RecordingStore()

        GraphSearch(store, seed_extractor=_Extractor()).search(
            "q", filters={GRAPH_CALLER_SEED_IDS_KEY: ["a", "b", "a"]}
        )

        assert store.subgraph_calls[0]["seed_ids"] == ["a", "b", "derived"]

    def test_the_named_seeds_are_capped_at_the_bfs_root_bound(self) -> None:
        store = _RecordingStore()
        named = [f"n{i}" for i in range(DEFAULT_MAX_SEEDS + 5)]

        GraphSearch(store).search("q", filters={GRAPH_CALLER_SEED_IDS_KEY: named})

        assert store.subgraph_calls[0]["seed_ids"] == named[:DEFAULT_MAX_SEEDS]

    def test_the_merged_set_is_capped_caller_seeds_first(self) -> None:
        """Caller and extractor seeds are one ``get_subgraph`` call, so the
        bound is on the merged set: capping each side alone would let the two
        reach twice the number of BFS roots."""

        class _Extractor:
            def extract(self, intent: str) -> list[str]:
                return [f"d{i}" for i in range(DEFAULT_MAX_SEEDS)]

        store = _RecordingStore()
        named = [f"n{i}" for i in range(DEFAULT_MAX_SEEDS - 3)]

        GraphSearch(store, seed_extractor=_Extractor()).search(
            "q", filters={GRAPH_CALLER_SEED_IDS_KEY: named}
        )

        seeds = store.subgraph_calls[0]["seed_ids"]
        assert len(seeds) == DEFAULT_MAX_SEEDS
        assert seeds[: len(named)] == named
        assert seeds[len(named) :] == ["d0", "d1", "d2"]

    def test_an_extractor_alone_is_capped_as_before(self) -> None:
        class _Extractor:
            def extract(self, intent: str) -> list[str]:
                return [f"d{i}" for i in range(DEFAULT_MAX_SEEDS)]

        store = _RecordingStore()

        GraphSearch(store, seed_extractor=_Extractor()).search("q")

        assert len(store.subgraph_calls[0]["seed_ids"]) == DEFAULT_MAX_SEEDS

    @pytest.mark.parametrize("junk", [None, "tbl:x", 42, [], [None, "", 7]])
    def test_a_malformed_value_costs_the_hint_not_the_pack(self, junk: Any) -> None:
        store = _RecordingStore()

        items = GraphSearch(store).search(
            "q", filters={GRAPH_CALLER_SEED_IDS_KEY: junk}
        )

        assert items == []
        assert store.subgraph_calls == []
        assert store.query_calls >= 1

    def test_the_control_key_is_on_the_allow_list(self) -> None:
        assert GRAPH_CALLER_SEED_IDS_KEY in GRAPH_CONTROL_FILTER_KEYS


class TestTheControlKeyNeverReachesADocumentStore:
    def test_the_keyword_axis_is_not_emptied(self, registry: StoreRegistry) -> None:
        """A document store compiles an unknown filter key to hard metadata
        equality, which matches no row — the #404 shape, one layer up."""
        _precedent_on(registry, ENTITY, PRECEDENT)
        registry.knowledge.document_store.put(
            "doc-1", "how to reconcile the ledger", {"title": "ledger doc"}
        )
        builder = PackBuilder(
            strategies=[
                KeywordSearch(registry.knowledge.document_store),
                GraphSearch(registry.knowledge.graph_store),
            ]
        )

        pack = builder.build_sectioned(
            "reconcile the ledger",
            sections=[SectionRequest(name="all", entity_ids=[ENTITY])],
        )

        served = {i.item_id for s in pack.sections for i in s.items}
        assert "doc-1" in served
        assert PRECEDENT in served
