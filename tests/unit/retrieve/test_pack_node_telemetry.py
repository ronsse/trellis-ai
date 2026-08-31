"""``node_type`` / ``node_role`` reach ``PACK_ASSEMBLED.injected_items[]`` (#375).

Gate 4 of ``docs/design/plan-375-graph-candidates.md``: every per-type number
in that plan required joining a served ``item_id`` back to ``nodes WHERE
valid_to IS NULL``. The join happened to hold, but it reads a *mutable* table
to describe a *past* serving. These tests pin the two properties that make the
forwarded fields worth more than that join:

* the value on the event is the value on the **row**, not a value a stored
  property (``properties`` is an open bag) was able to forge; and
* an item that is not a graph node carries **no** value — the key is absent
  rather than defaulted, so a per-role split can never be a statement about
  the filler. That is the failure #363 / #385 / #388 each shipped a version of.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.observation_strategy import ObservationSearch
from trellis.retrieve.pack_builder import PackBuilder, _item_attribution
from trellis.retrieve.strategies import GraphSearch
from trellis.schemas.pack import PackItem
from trellis.schemas.well_known import HAS_OBSERVATION, OBSERVATION
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


class _StubGraphStore:
    """Minimal graph store returning a fixed node list from ``query``."""

    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self._nodes = nodes

    def query(
        self,
        node_type: str | None = None,
        properties: dict[str, Any] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return list(self._nodes)


class _StubStrategy:
    """Returns fixed items; enough for ``PackBuilder`` to assemble a pack."""

    def __init__(self, name: str, items: list[PackItem]) -> None:
        self._name = name
        self._items = items

    @property
    def name(self) -> str:
        return self._name

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[PackItem]:
        return list(self._items)


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


class TestGraphSearchStampsTheRow:
    """The stamp reports the row, and nothing stored can overrule it."""

    def test_node_type_and_role_are_stamped(self) -> None:
        store = _StubGraphStore(
            [
                {
                    "node_id": "n1",
                    "node_type": "gotcha",
                    "node_role": "curated",
                    "properties": {"name": "uv run rewrites the lockfile"},
                }
            ]
        )
        item = GraphSearch(store).search("anything")[0]
        assert item.metadata["node_type"] == "gotcha"
        assert item.metadata["node_role"] == "curated"

    def test_missing_role_reports_the_store_default(self) -> None:
        """``node_role`` defaults to ``semantic`` in the store, not here.

        A backend row that omits the column is a ``semantic`` row — the
        default lives in ``GraphStore.upsert_node``. Reporting it is not
        filling in an unknown.
        """
        store = _StubGraphStore(
            [{"node_id": "n1", "node_type": "concept", "properties": {"name": "n"}}]
        )
        item = GraphSearch(store).search("anything")[0]
        assert item.metadata["node_role"] == "semantic"

    def test_stored_properties_cannot_misreport_type_or_role(self) -> None:
        """``properties`` is an open bag; these two are facts about the row.

        Before #375 the property spread ran *after* these keys, so a node
        carrying either name overrode the real value — which would also have
        let a structural row hide from ``PackBuilder``'s defence-in-depth
        filter, since that filter reads ``metadata["node_role"]``.
        """
        store = _StubGraphStore(
            [
                {
                    "node_id": "liar",
                    "node_type": "Activity",
                    "node_role": "structural",
                    "properties": {
                        "name": "n",
                        "node_type": "gotcha",
                        "node_role": "curated",
                        "node_type_canonical": "gotcha",
                    },
                }
            ]
        )
        item = GraphSearch(store, curated_boost=1.0).search(
            "anything", filters={"include_structural": True}
        )[0]
        assert item.metadata["node_type"] == "Activity"
        assert item.metadata["node_role"] == "structural"
        assert item.metadata["node_type_canonical"] != "gotcha"

    def test_forged_role_cannot_defeat_the_structural_filter(self) -> None:
        """The stamp is read back as a decision, not only as telemetry."""
        store = _StubGraphStore(
            [
                {
                    "node_id": "hidden",
                    "node_type": "concept",
                    "node_role": "structural",
                    "properties": {"name": "n", "node_role": "semantic"},
                }
            ]
        )
        assert GraphSearch(store).search("anything") == []


class TestAttributionForwardsToTheEvent:
    """``_item_attribution`` is the seam onto ``injected_items[]``."""

    def test_graph_item_forwards_both_fields(self) -> None:
        item = PackItem(
            item_id="n1",
            item_type="entity",
            excerpt="x",
            relevance_score=1.0,
            metadata={
                "source_strategy": "graph",
                "node_type": "gotcha",
                "node_role": "semantic",
            },
        )
        attribution = _item_attribution(item)
        assert attribution["node_type"] == "gotcha"
        assert attribution["node_role"] == "semantic"

    def test_non_graph_item_carries_neither_key(self) -> None:
        """Absent, not defaulted — a document has no role to report."""
        item = PackItem(
            item_id="d1",
            item_type="document",
            excerpt="x",
            relevance_score=1.0,
            metadata={"source_strategy": "keyword", "title": "A doc"},
        )
        attribution = _item_attribution(item)
        assert "node_type" not in attribution
        assert "node_role" not in attribution

    def test_observation_items_carry_them_too(self) -> None:
        """The observation axis is the other graph-backed strategy."""
        observation = {
            "node_id": "o1",
            "node_type": OBSERVATION,
            "node_role": "semantic",
            "properties": {
                "subject_entity_id": "e1",
                "subject_entity_type": "Dataset",
                "observer_agent_id": "test-agent",
                "content": "the nightly job writes nothing on a cold cache",
                "confidence": 0.9,
                "observed_at": "2026-08-30T00:00:00+00:00",
            },
        }
        store = MagicMock()
        del store.get_nodes_bulk
        store.get_edges.return_value = [
            {"source_id": "e1", "target_id": "o1", "edge_type": HAS_OBSERVATION}
        ]
        store.get_node.return_value = observation

        items = ObservationSearch(store).search(
            "anything", filters={"subject_entity_id": "e1"}
        )
        assert items, "observation axis returned nothing to attribute"
        attribution = _item_attribution(items[0])
        assert attribution["node_type"] == OBSERVATION
        assert attribution["node_role"] == "semantic"


class TestPackAssembledPayload:
    """End to end: the fields are on the emitted event, per item."""

    def test_payload_splits_graph_items_from_documents(
        self, event_log: SQLiteEventLog
    ) -> None:
        graph_item = PackItem(
            item_id="n1",
            item_type="entity",
            excerpt="a graph node with a real excerpt",
            relevance_score=0.9,
            metadata={
                "source_strategy": "graph",
                "node_type": "gotcha",
                "node_role": "semantic",
            },
        )
        doc_item = PackItem(
            item_id="d1",
            item_type="document",
            excerpt="a document with a real excerpt",
            relevance_score=0.8,
            metadata={"source_strategy": "keyword"},
        )
        builder = PackBuilder(
            strategies=[_StubStrategy("stub", [graph_item, doc_item])],
            event_log=event_log,
        )
        builder.build("q")

        events = event_log.get_events(event_type=EventType.PACK_ASSEMBLED, limit=10)
        assert len(events) == 1
        rows = {r["item_id"]: r for r in events[0].payload["injected_items"]}
        assert rows["n1"]["node_type"] == "gotcha"
        assert rows["n1"]["node_role"] == "semantic"
        assert "node_type" not in rows["d1"]
        assert "node_role" not in rows["d1"]
