"""The graph axis's seeds are recorded once per pack, not once per served item.

``GraphSearch`` stamps its whole seed list on every item it returns, because
that is how ``PackBuilder`` learns what the axis expanded from. Once lifted
into ``PACK_ASSEMBLED.seed_entity_ids`` the stamp is a duplicate, and a REST or
SDK caller would receive it again on every graph item. These tests pin that the
builder lifts first, removes second, and does both before dedup and the budget
cut, on the flat path and the sectioned one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GRAPH_SEED_IDS_KEY, SearchStrategy
from trellis.schemas.pack import PackBudget, PackItem, SectionRequest
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

_SEEDS = ["seed:b", "seed:a"]  # deliberately not sorted: order is the record


class _Stub(SearchStrategy):
    """Hands back the *same* ``PackItem`` objects on every call, as a cache would."""

    def __init__(self, strategy_name: str, items: list[PackItem]) -> None:
        self._name = strategy_name
        self._items = items

    @property
    def name(self) -> str:
        return self._name

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        return list(self._items)


def _item(
    item_id: str,
    *,
    score: float = 0.5,
    excerpt: str | None = None,
    **metadata: Any,
) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="entity",
        excerpt=excerpt if excerpt is not None else f"content of {item_id}",
        relevance_score=score,
        metadata=metadata,
    )


def _graph_item(item_id: str, **kwargs: Any) -> PackItem:
    return _item(item_id, **{GRAPH_SEED_IDS_KEY: list(_SEEDS)}, **kwargs)


@pytest.fixture
def log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    event_log = SQLiteEventLog(tmp_path / "events.db")
    yield event_log
    event_log.close()


def _payload(log: SQLiteEventLog) -> dict[str, Any]:
    events = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=1)
    assert len(events) == 1
    return events[0].payload


def _builder(log: SQLiteEventLog, *strategies: SearchStrategy) -> PackBuilder:
    return PackBuilder(strategies=list(strategies), event_log=log)


class TestFlatPack:
    def test_a_served_graph_item_carries_no_seed_list_and_the_event_keeps_it(
        self, log: SQLiteEventLog
    ) -> None:
        builder = _builder(
            log,
            _Stub(
                "graph",
                [
                    _graph_item("g1", node_type="concept"),
                    _graph_item("g2", score=0.4, node_type="concept"),
                ],
            ),
        )

        pack = builder.build("anything")

        assert [i.item_id for i in pack.items] == ["g1", "g2"]
        assert all(GRAPH_SEED_IDS_KEY not in i.metadata for i in pack.items)
        # The rest of the item's metadata is untouched.
        assert all(i.metadata["node_type"] == "concept" for i in pack.items)
        payload = _payload(log)
        assert payload["seed_entity_ids"] == _SEEDS
        assert GRAPH_SEED_IDS_KEY not in json.dumps(payload)

    def test_an_unseeded_pack_still_records_an_empty_seed_list(
        self, log: SQLiteEventLog
    ) -> None:
        pack = _builder(log, _Stub("graph", [_item("g1")])).build("anything")

        assert [i.item_id for i in pack.items] == ["g1"]
        assert _payload(log)["seed_entity_ids"] == []

    def test_the_strategys_own_items_are_not_edited(self, log: SQLiteEventLog) -> None:
        stored = _graph_item("g1")
        builder = _builder(log, _Stub("graph", [stored]))

        pack = builder.build("anything")
        again = builder.build("anything")

        assert GRAPH_SEED_IDS_KEY in stored.metadata
        assert stored.metadata[GRAPH_SEED_IDS_KEY] == _SEEDS
        # A second build from the same objects still lifts the seeds.
        assert GRAPH_SEED_IDS_KEY not in pack.items[0].metadata
        assert GRAPH_SEED_IDS_KEY not in again.items[0].metadata
        events = log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=5)
        assert [e.payload["seed_entity_ids"] for e in events] == [_SEEDS, _SEEDS]

    def test_only_the_graph_axis_is_stripped(self, log: SQLiteEventLog) -> None:
        """A document's own property of the same name is its data, not ours."""
        doc = _item("d1", **{GRAPH_SEED_IDS_KEY: ["document-own"]})
        builder = _builder(
            log, _Stub("keyword", [doc]), _Stub("graph", [_graph_item("g1")])
        )

        pack = builder.build("anything")

        served = {i.item_id: i for i in pack.items}
        assert served["d1"].metadata[GRAPH_SEED_IDS_KEY] == ["document-own"]
        assert GRAPH_SEED_IDS_KEY not in served["g1"].metadata
        assert _payload(log)["seed_entity_ids"] == _SEEDS


class TestSeedsOutliveTheCutsOnGraphItems:
    """Seeds are lifted at the collect seam, before dedup and the budget.

    Today's behaviour, pinned rather than changed: a pack that served only
    documents still records what its graph axis expanded from, because the
    learning loop attaches a promoted precedent to the seeded entities and a
    document is exactly the item that can be served when no graph item made
    the cut.
    """

    def test_every_graph_item_cut_by_dedup_still_records_the_seeds(
        self, log: SQLiteEventLog
    ) -> None:
        winner = _item("shared", score=0.9, excerpt="keyword copy")
        loser = _graph_item("shared", score=0.1)
        builder = _builder(log, _Stub("keyword", [winner]), _Stub("graph", [loser]))

        pack = builder.build("anything")

        assert [i.item_id for i in pack.items] == ["shared"]
        assert pack.items[0].excerpt == "keyword copy"
        reasons = {r.reason for r in pack.retrieval_report.rejected_items}
        assert "dedup" in reasons
        assert _payload(log)["seed_entity_ids"] == _SEEDS

    def test_every_graph_item_cut_by_max_items_still_records_the_seeds(
        self, log: SQLiteEventLog
    ) -> None:
        builder = _builder(
            log,
            _Stub("keyword", [_item("d1", score=0.9)]),
            _Stub("graph", [_graph_item("g1", score=0.1)]),
        )

        pack = builder.build("anything", budget=PackBudget(max_items=1))

        assert [i.item_id for i in pack.items] == ["d1"]
        assert [
            r.reason for r in pack.retrieval_report.rejected_items if r.item_id == "g1"
        ] == ["max_items"]
        assert _payload(log)["seed_entity_ids"] == _SEEDS

    def test_every_graph_item_cut_by_the_token_budget_still_records_the_seeds(
        self, log: SQLiteEventLog
    ) -> None:
        body = "x" * 40  # about ten tokens each
        builder = _builder(
            log,
            _Stub("keyword", [_item("d1", score=0.9, excerpt=body)]),
            _Stub("graph", [_graph_item("g1", score=0.1, excerpt=body)]),
        )

        pack = builder.build("anything", budget=PackBudget(max_items=10, max_tokens=12))

        assert [i.item_id for i in pack.items] == ["d1"]
        assert [
            r.reason for r in pack.retrieval_report.rejected_items if r.item_id == "g1"
        ] == ["token_budget"]
        assert _payload(log)["seed_entity_ids"] == _SEEDS


class TestSectionedPack:
    def test_a_served_graph_item_carries_no_seed_list(
        self, log: SQLiteEventLog
    ) -> None:
        builder = _builder(
            log,
            _Stub("keyword", [_item("d1", score=0.9)]),
            _Stub("graph", [_graph_item("g1"), _graph_item("g2", score=0.4)]),
        )

        pack = builder.build_sectioned(
            "anything",
            sections=[
                SectionRequest(name="first"),
                SectionRequest(name="second"),
            ],
        )

        served = [i for section in pack.sections for i in section.items]
        assert {i.item_id for i in served} >= {"g1", "g2"}
        assert all(GRAPH_SEED_IDS_KEY not in i.metadata for i in served)
        assert GRAPH_SEED_IDS_KEY not in json.dumps(_payload(log))

    def test_the_strategys_own_items_are_not_edited(self, log: SQLiteEventLog) -> None:
        stored = _graph_item("g1")

        _builder(log, _Stub("graph", [stored])).build_sectioned(
            "anything", sections=[SectionRequest(name="all")]
        )

        assert stored.metadata[GRAPH_SEED_IDS_KEY] == _SEEDS
