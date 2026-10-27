"""``submit_learning_promotion`` skips a precedent already holding the promotion (#983).

The skip is for one state only: current, carrying every value the payload
writes, with each named edge and a ``PRECEDENT_PROMOTED`` event in place. Every
other near-miss must still write, because a ``promoted_incomplete`` row is
mended by running it again and a re-scored candidate must still land.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.learning.scoring import (
    ALREADY_PROMOTED,
    preview_learning_promotion,
    promotion_verdict,
    submit_learning_promotion,
)
from trellis.mutate import Command, Operation, build_curate_executor
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

ENTITY_ID = "precedent://learning/rerun"
TARGET_ID = "entity://target"


def _entity_payload(**properties: Any) -> dict[str, Any]:
    return {
        "entity_type": "precedent",
        "entity_id": ENTITY_ID,
        "name": "Rerun precedent",
        "properties": {
            "description": "same",
            "approved_rationale": None,
            "approved_at": "2026-01-01T00:00:00+00:00",
            **properties,
        },
    }


def _edge_payloads() -> list[dict[str, Any]]:
    return [
        {
            "source_id": ENTITY_ID,
            "target_id": TARGET_ID,
            "edge_kind": "precedent_applies_to",
            "properties": {"source_of_truth": "reviewed_promotion"},
        }
    ]


class _Stores:
    def __init__(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        self.registry = StoreRegistry(stores_dir=stores_dir)
        self.executor = build_curate_executor(self.registry)
        self.graph = self.registry.knowledge.graph_store
        self.events = self.registry.operational.event_log
        self.graph.upsert_node(TARGET_ID, "entity", {"name": "target"})

    def submit(
        self,
        payload: dict[str, Any] | None = None,
        edges: list[dict[str, Any]] | None = None,
        *,
        with_event_log: bool = True,
    ) -> dict[str, Any]:
        return submit_learning_promotion(
            self.executor,
            payload or _entity_payload(),
            _edge_payloads() if edges is None else edges,
            requested_by="test",
            graph_store=self.graph,
            event_log=self.events if with_event_log else None,
        )

    def writes(self) -> tuple[int, int, int, int]:
        """Versions, promote events, executed commands and edges at the id."""
        return (
            len(self.graph.get_node_history(ENTITY_ID)),
            len(
                self.events.get_events(
                    event_type=EventType.PRECEDENT_PROMOTED, entity_id=ENTITY_ID
                )
            ),
            len(self.events.get_events(event_type=EventType.MUTATION_EXECUTED)),
            len(self.graph.get_edges(ENTITY_ID, direction="outgoing")),
        )


@pytest.fixture
def stores(tmp_path: Path) -> _Stores:
    return _Stores(tmp_path)


def test_an_identical_resubmission_is_already_promoted_and_issues_no_command(
    stores: _Stores,
) -> None:
    first = stores.submit()
    assert first["status"] == "promoted", first
    before = stores.writes()
    assert before[0] == 1
    assert before[1] == 1
    assert before[3] == 1

    # ``approved_at`` is stamped from the clock on every build, so it must not
    # count as a difference.
    second = stores.submit(
        _entity_payload(approved_at="2026-02-02T00:00:00+00:00"),
    )

    assert second["status"] == ALREADY_PROMOTED
    assert second["entity_id"] == ENTITY_ID
    assert "nothing was written" in second["message"]
    # Not one executor command ran: no new version, no second promote event,
    # and no audit row for a write that did not happen.
    assert stores.writes() == before
    assert promotion_verdict([second]) == "ok"


def test_the_preview_reports_the_same_skip_the_submission_answers(
    stores: _Stores,
) -> None:
    payload, edges = _entity_payload(), _edge_payloads()
    assert (
        preview_learning_promotion(
            payload, edges, graph_store=stores.graph, event_log=stores.events
        )
        is None
    )
    stores.submit()

    preview = preview_learning_promotion(
        payload, edges, graph_store=stores.graph, event_log=stores.events
    )

    assert preview is not None
    assert preview["status"] == ALREADY_PROMOTED
    assert stores.submit()["status"] == preview["status"]


def test_without_an_event_log_the_event_cannot_be_checked_so_the_write_goes_ahead(
    stores: _Stores,
) -> None:
    stores.submit()

    second = stores.submit(with_event_log=False)

    assert second["status"] == "promoted"
    assert stores.writes()[:2] == (2, 2)


def test_changed_content_still_writes(stores: _Stores) -> None:
    stores.submit()

    second = stores.submit(_entity_payload(description="re-scored"))

    assert second["status"] == "promoted"
    node = stores.graph.get_node(ENTITY_ID)
    assert node is not None
    assert node["properties"]["description"] == "re-scored"
    assert stores.writes()[:2] == (2, 2)


def test_a_changed_name_still_writes(stores: _Stores) -> None:
    stores.submit()
    payload = _entity_payload()
    payload["name"] = "Renamed precedent"

    assert stores.submit(payload)["status"] == "promoted"


def test_a_missing_promote_event_is_mended_by_running_it_again(
    stores: _Stores,
) -> None:
    """The entity landed and the event did not: not "already promoted"."""
    payload = _entity_payload()
    created = stores.executor.execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={
                "entity_type": "precedent",
                "entity_id": ENTITY_ID,
                "name": payload["name"],
                "properties": dict(payload["properties"]),
            },
            target_type="entity",
            requested_by="test",
        )
    )
    assert created.status.value == "success", created.message
    assert stores.writes()[1] == 0

    second = stores.submit(payload, edges=[])

    assert second["status"] == "promoted"
    assert stores.writes()[1] == 1


def test_a_missing_edge_is_mended_by_running_it_again(stores: _Stores) -> None:
    assert stores.submit(edges=[])["status"] == "promoted"
    assert stores.writes()[3] == 0

    second = stores.submit()

    assert second["status"] == "promoted"
    assert stores.writes()[3] == 1
    # Now complete: the next run leaves it alone.
    assert stores.submit()["status"] == ALREADY_PROMOTED


def test_a_miner_candidate_row_is_not_the_promotion_event(stores: _Stores) -> None:
    payload = _entity_payload()
    stores.executor.execute(
        Command(
            operation=Operation.ENTITY_CREATE,
            args={
                "entity_type": "precedent",
                "entity_id": ENTITY_ID,
                "name": payload["name"],
                "properties": dict(payload["properties"]),
            },
            target_type="entity",
            requested_by="test",
        )
    )
    stores.events.emit(
        EventType.PRECEDENT_PROMOTED,
        source="precedent_miner",
        entity_id=ENTITY_ID,
        entity_type="precedent",
        payload={"title": "mined", "description": "mined"},
    )

    assert stores.submit(payload, edges=[])["status"] == "promoted"


def test_a_node_that_is_not_a_precedent_is_never_skipped(stores: _Stores) -> None:
    stores.graph.upsert_node(ENTITY_ID, "entity", {"name": "Rerun precedent"})
    event_log = MagicMock()
    event_log.get_events.return_value = [
        MagicMock(source="mutation_executor", payload={})
    ]

    outcome = preview_learning_promotion(
        _entity_payload(),
        [],
        graph_store=stores.graph,
        event_log=event_log,
    )

    # The id holds an ``entity`` node, not a precedent, and the event log says a
    # promotion exists: the content comparison alone must not skip the write.
    assert outcome is None


def test_a_backend_that_drops_null_properties_still_matches() -> None:
    """Some graph backends cannot store a null property, so it reads back absent."""
    graph = MagicMock()
    graph.get_node.return_value = {
        "node_id": ENTITY_ID,
        "node_type": "precedent",
        "properties": {"name": "Rerun precedent", "description": "same"},
    }
    graph.get_edges.return_value = []
    event_log = MagicMock()
    event_log.get_events.return_value = [
        MagicMock(source="mutation_executor", payload={})
    ]

    outcome = preview_learning_promotion(
        _entity_payload(), [], graph_store=graph, event_log=event_log
    )

    assert outcome is not None
    assert outcome["status"] == ALREADY_PROMOTED


def test_a_property_the_node_gained_later_does_not_block_the_skip() -> None:
    graph = MagicMock()
    graph.get_node.return_value = {
        "node_id": ENTITY_ID,
        "node_type": "precedent",
        "properties": {
            "name": "Rerun precedent",
            "description": "same",
            "approved_rationale": None,
            "lifecycle": {"state": "current"},
        },
    }
    graph.get_edges.return_value = []
    event_log = MagicMock()
    event_log.get_events.return_value = [
        MagicMock(source="mutation_executor", payload={})
    ]

    outcome = preview_learning_promotion(
        _entity_payload(), [], graph_store=graph, event_log=event_log
    )

    assert outcome is not None
    assert outcome["status"] == ALREADY_PROMOTED


@pytest.mark.parametrize(
    "outcomes",
    [
        [{"status": ALREADY_PROMOTED}],
        [{"status": ALREADY_PROMOTED}, {"status": ALREADY_PROMOTED}],
    ],
)
def test_already_promoted_rows_alone_are_an_ok_run(
    outcomes: list[dict[str, Any]],
) -> None:
    assert promotion_verdict(outcomes) == "ok"


def test_already_promoted_does_not_hide_a_refusal_or_a_failure() -> None:
    skipped = {"status": ALREADY_PROMOTED}
    assert promotion_verdict([skipped, {"status": "missing_candidate"}]) == "refused"
    assert promotion_verdict([skipped, {"status": "entity_failed"}]) == "failed"
    assert promotion_verdict([skipped], unnamed_count=1) == "refused"
