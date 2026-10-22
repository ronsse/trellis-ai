"""A hub seed's neighbourhood is prioritised before it is sliced (#949).

``GraphSearch`` slices the seeded neighbourhood to ``limit`` before it scores
anything, and the SQLite and Postgres stores hand that neighbourhood back
ordered by ``(depth, node_id)``. A seed with more than ``limit`` neighbours
therefore fills the slice with whichever ids sort first, and a promoted
precedent (``precedent_applies_to`` the seed) sorts after every id that
begins a-o. The Bolt backends return no order at all (a ``set`` of ids
through an unordered bulk match), so the same slice is arbitrary there.

Scoring every node and slicing afterwards was considered and does not help:
the base score is positional (``1 - i * step``, zero from the twentieth row),
so a row scored late scores nothing. The fix is a stable partition, applied
only when the slice would drop rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import trellis.retrieve.strategies as strategies_mod
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import (
    GRAPH_SELECTION_SEEDED,
    GraphSearch,
)
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path

SEED = "domain:platform"
PRECEDENT = "precedent://learning-1"
APPLIES = "precedent_applies_to"
LIMIT = 20

# Far enough in the future that recency decay clamps to a multiplier of 1.0,
# so two calls over the same rows score identically and a byte comparison of
# two searches is meaningful.
_STAMP = "2999-01-01T00:00:00+00:00"


@pytest.fixture
def graph(tmp_path: Path) -> Any:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir).knowledge.graph_store


def _hub(graph: Any, *, neighbours: int = 30, precedent: bool = True) -> None:
    """A seed with ``neighbours`` ``concept:*`` rows and one precedent."""
    graph.upsert_node(
        node_id=SEED,
        node_type="domain",
        properties={"name": "platform", "description": "the platform domain"},
    )
    for i in range(neighbours):
        node_id = f"concept:c{i:03d}"
        graph.upsert_node(
            node_id=node_id,
            node_type="concept",
            properties={"name": f"concept {i}", "description": f"concept {i}"},
        )
        graph.upsert_edge(node_id, SEED, "related_to")
    if precedent:
        graph.upsert_node(
            node_id=PRECEDENT,
            node_type="precedent",
            properties={"name": "lesson", "description": "a promoted lesson"},
        )
        graph.upsert_edge(PRECEDENT, SEED, APPLIES)


def _seeded(graph: Any, **kwargs: Any) -> list[Any]:
    return GraphSearch(graph).search(
        "anything",
        limit=kwargs.pop("limit", LIMIT),
        filters={"seed_ids": [SEED], "depth": 1, **kwargs},
    )


class _Extractor:
    def __init__(self, seeds: list[str]) -> None:
        self._seeds = seeds

    def extract(self, intent: str) -> list[str]:
        return list(self._seeds)


def _node(node_id: str, node_type: str = "concept", **props: Any) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": node_type,
        "created_at": _STAMP,
        "properties": {"name": node_id, **props},
    }


def _edge(source: str, target: str, kind: str) -> dict[str, Any]:
    return {"source_id": source, "target_id": target, "edge_type": kind}


class _SubgraphStore:
    """Returns one fixed subgraph, in the order the test chose."""

    def __init__(
        self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None
    ) -> None:
        self._nodes = nodes
        self._edges = edges or []

    def get_subgraph(
        self,
        seed_ids: list[str],
        depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> dict[str, Any]:
        return {"nodes": list(self._nodes), "edges": list(self._edges)}


def _search(store: Any, **kwargs: Any) -> list[Any]:
    return GraphSearch(store).search(
        "anything",
        limit=kwargs.pop("limit", LIMIT),
        filters={"seed_ids": [SEED], "depth": 1},
    )


class TestHubSeedKeepsItsPrecedent:
    def test_precedent_on_a_hub_seed_is_served(self, graph: Any) -> None:
        _hub(graph)
        items = _seeded(graph)
        ids = [i.item_id for i in items]
        assert len(ids) == LIMIT
        assert PRECEDENT in ids
        assert SEED in ids

    def test_the_precedent_ranks_with_the_seed_not_behind_the_slice(
        self, graph: Any
    ) -> None:
        # Served is not enough: the pack builder spends its own budget in
        # score order, and a precedent scored at the tail would still lose it.
        _hub(graph)
        ranked = [i.item_id for i in _seeded(graph)]
        assert PRECEDENT in ranked[:3]

    def test_the_flat_pack_serves_the_precedent_through_the_extractor(
        self, graph: Any
    ) -> None:
        # The production route: an injected extractor, one hop, PackBuilder.
        _hub(graph)
        builder = PackBuilder(
            strategies=[GraphSearch(graph, seed_extractor=_Extractor([SEED]))]
        )
        pack = builder.build("anything")
        assert PRECEDENT in {i.item_id for i in pack.items}

    def test_a_seed_the_store_returns_late_is_not_dropped(self) -> None:
        # Bolt returns no order, so the seed can land after its neighbours.
        nodes = [_node(f"concept:c{i:03d}") for i in range(30)] + [_node(SEED)]
        ids = {i.item_id for i in _search(_SubgraphStore(nodes))}
        assert SEED in ids

    def test_a_precedent_returned_first_still_loses_to_nothing(self) -> None:
        # Order independence: put the precedent first, last, and in the
        # middle of the store's answer; it is served each time.
        concepts = [_node(f"concept:c{i:03d}") for i in range(30)]
        precedent = _node(PRECEDENT, "precedent")
        edges = [_edge(PRECEDENT, SEED, APPLIES)]
        for at in (0, 15, 30):
            nodes = [_node(SEED), *concepts]
            nodes.insert(at + 1, precedent)
            ids = {i.item_id for i in _search(_SubgraphStore(nodes, edges))}
            assert PRECEDENT in ids, at

    def test_priority_rows_are_still_capped_at_the_limit(self) -> None:
        precedents = [_node(f"precedent://p{i:03d}", "precedent") for i in range(25)]
        edges = [_edge(p["node_id"], SEED, APPLIES) for p in precedents]
        nodes = [_node(SEED), *precedents, *[_node(f"concept:c{i}") for i in range(5)]]
        items = _search(_SubgraphStore(nodes, edges))
        assert len(items) == LIMIT
        assert not any(i.item_id.startswith("concept:") for i in items)


class TestOnlyTheNamedRelationPromotes:
    def test_another_edge_kind_does_not_promote(self) -> None:
        # Judged by the edge, not by an id that merely looks like a precedent.
        concepts = [_node(f"concept:c{i:03d}") for i in range(30)]
        decoy = _node("precedent://decoy", "precedent")
        nodes = [_node(SEED), *concepts, decoy]
        edges = [_edge("precedent://decoy", SEED, "related_to")]
        ids = {i.item_id for i in _search(_SubgraphStore(nodes, edges))}
        assert "precedent://decoy" not in ids

    def test_a_precedent_attached_to_a_non_seed_is_not_promoted(self) -> None:
        concepts = [_node(f"concept:c{i:03d}") for i in range(30)]
        other = _node("precedent://elsewhere", "precedent")
        nodes = [_node(SEED), *concepts, other]
        edges = [_edge("precedent://elsewhere", "concept:c000", APPLIES)]
        ids = {i.item_id for i in _search(_SubgraphStore(nodes, edges))}
        assert "precedent://elsewhere" not in ids

    def test_the_direction_is_precedent_to_target(self) -> None:
        # A seed that *is* a precedent must not promote what it applies to.
        concepts = [_node(f"concept:c{i:03d}") for i in range(30)]
        target = _node("zzz-target")
        nodes = [_node(SEED), *concepts, target]
        edges = [_edge(SEED, "zzz-target", APPLIES)]
        ids = {i.item_id for i in _search(_SubgraphStore(nodes, edges))}
        assert "zzz-target" not in ids


class TestSelectionStamp:
    def test_a_reordered_slice_records_the_rule(self, graph: Any) -> None:
        _hub(graph)
        items = _seeded(graph)
        assert {i.metadata["graph_selection"] for i in items} == {
            strategies_mod.GRAPH_SELECTION_SEEDED_PRIORITY
        }

    def test_the_new_value_is_distinct_from_the_old_ones(self) -> None:
        values = {
            strategies_mod.GRAPH_SELECTION_SEEDED,
            strategies_mod.GRAPH_SELECTION_RECENCY_WINDOW,
            strategies_mod.GRAPH_SELECTION_SEEDED_PRIORITY,
        }
        assert len(values) == 3

    def test_a_hub_with_nothing_to_promote_keeps_the_old_value(
        self, graph: Any
    ) -> None:
        # The partition is a no-op here (the seed is already first), so
        # claiming the rule applied would be a statement about the wiring.
        _hub(graph, precedent=False)
        items = _seeded(graph)
        assert {i.metadata["graph_selection"] for i in items} == {
            GRAPH_SELECTION_SEEDED
        }


def _identity(nodes: list[dict[str, Any]], **_: Any) -> list[dict[str, Any]]:
    return nodes


class TestNoHubIsByteIdentical:
    """Below the limit, and for a hub with nothing to promote, nothing moves."""

    @staticmethod
    def _dump(items: list[Any]) -> list[dict[str, Any]]:
        return [i.model_dump(mode="json") for i in items]

    def test_a_neighbourhood_within_the_limit_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A precedent that sorts last and a seed that does not sort first:
        # a partition applied unconditionally would move both.
        nodes = [_node(f"concept:c{i:03d}") for i in range(10)]
        nodes += [_node(SEED), _node(PRECEDENT, "precedent")]
        store = _SubgraphStore(nodes, [_edge(PRECEDENT, SEED, APPLIES)])
        with_fix = self._dump(_search(store))
        monkeypatch.setattr(strategies_mod, "_prioritise_seeded_nodes", _identity)
        without = self._dump(_search(store))
        assert with_fix == without
        assert [i["item_id"] for i in with_fix[:1]] == ["concept:c000"]
        assert {i["metadata"]["graph_selection"] for i in with_fix} == {
            GRAPH_SELECTION_SEEDED
        }

    def test_exactly_the_limit_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nodes = [_node(f"concept:c{i:03d}") for i in range(LIMIT - 2)]
        nodes += [_node(SEED), _node(PRECEDENT, "precedent")]
        assert len(nodes) == LIMIT
        store = _SubgraphStore(nodes, [_edge(PRECEDENT, SEED, APPLIES)])
        with_fix = self._dump(_search(store))
        monkeypatch.setattr(strategies_mod, "_prioritise_seeded_nodes", _identity)
        assert with_fix == self._dump(_search(store))

    def test_a_hub_with_nothing_to_promote_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nodes = [_node(SEED), *[_node(f"concept:c{i:03d}") for i in range(40)]]
        store = _SubgraphStore(nodes)
        with_fix = self._dump(_search(store))
        monkeypatch.setattr(strategies_mod, "_prioritise_seeded_nodes", _identity)
        assert with_fix == self._dump(_search(store))
        assert len(with_fix) == LIMIT

    def test_the_unseeded_branch_never_prioritises(
        self, graph: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Position there is recency, which is information; do not reorder it.
        _hub(graph, neighbours=5)

        def _boom(*_: Any, **__: Any) -> None:
            msg = "the recency window must not be prioritised"
            raise AssertionError(msg)

        monkeypatch.setattr(strategies_mod, "_prioritise_seeded_nodes", _boom)
        assert GraphSearch(graph).search("anything", limit=3)


class TestDomainBoostReadsThePromotedField:
    """A promoted precedent stores ``primary_domain``, not ``domain``."""

    @staticmethod
    def _score(props: dict[str, Any], *, domain: str | None) -> float:
        nodes = [_node("only", **props)]
        filters: dict[str, Any] = {"seed_ids": ["only"], "depth": 1}
        if domain:
            filters["domain"] = domain
        (item,) = GraphSearch(_SubgraphStore(nodes)).search(
            "anything", limit=LIMIT, filters=filters
        )
        return item.relevance_score

    def test_primary_domain_alone_earns_the_boost(self) -> None:
        plain = self._score({}, domain="platform")
        boosted = self._score({"primary_domain": "platform"}, domain="platform")
        assert boosted == pytest.approx(plain * strategies_mod.GRAPH_DOMAIN_MATCH_BOOST)

    def test_the_scalar_domain_still_earns_it(self) -> None:
        plain = self._score({}, domain="platform")
        boosted = self._score({"domain": "platform"}, domain="platform")
        assert boosted == pytest.approx(plain * strategies_mod.GRAPH_DOMAIN_MATCH_BOOST)

    def test_a_different_primary_domain_earns_nothing(self) -> None:
        plain = self._score({}, domain="platform")
        assert self._score({"primary_domain": "data"}, domain="platform") == plain

    def test_no_requested_domain_means_no_boost(self) -> None:
        plain = self._score({}, domain=None)
        assert self._score({"primary_domain": "platform"}, domain=None) == plain
