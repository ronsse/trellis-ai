"""``submit_learning_promotion`` skips a precedent already holding the promotion (#983).

The skip is for one state only: current, carrying every value the payload
writes, with each named edge (and the properties it names), the precedent's
document row and a ``PRECEDENT_PROMOTED`` event in place. Every other near-miss
must still write, because a ``promoted_incomplete`` row is mended by running it
again and a re-scored candidate must still land.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.learning.scoring import (
    ALREADY_PROMOTED,
    precedent_document_content,
    preview_learning_promotion,
    promotion_verdict,
    submit_learning_promotion,
)
from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
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
        self.documents = self.registry.knowledge.document_store
        self.graph.upsert_node(TARGET_ID, "entity", {"name": "target"})

    def submit(
        self,
        payload: dict[str, Any] | None = None,
        edges: list[dict[str, Any]] | None = None,
        *,
        with_event_log: bool = True,
        with_document_store: bool = True,
    ) -> dict[str, Any]:
        return submit_learning_promotion(
            self.executor,
            payload or _entity_payload(),
            _edge_payloads() if edges is None else edges,
            requested_by="test",
            graph_store=self.graph,
            event_log=self.events if with_event_log else None,
            document_store=self.documents if with_document_store else None,
        )

    def preview(
        self,
        payload: dict[str, Any] | None = None,
        edges: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        return preview_learning_promotion(
            payload or _entity_payload(),
            _edge_payloads() if edges is None else edges,
            graph_store=self.graph,
            event_log=self.events,
            document_store=self.documents,
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
    assert stores.preview() is None
    stores.submit()

    preview = stores.preview()

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


def test_a_demoted_then_reinstated_precedent_resubmitted_is_already_promoted(
    stores: _Stores,
) -> None:
    """Demote and reinstate each rewrite the node and add a ``lifecycle`` stamp.

    The stamp is a key the payload does not carry, so it must not read as a
    difference once the state is current again, and the promote event is still
    in place. While demoted the same submission is a refusal, not a skip.
    """
    assert stores.submit()["status"] == "promoted"
    demoted = stores.executor.execute(
        Command(
            operation=Operation.PRECEDENT_DEMOTE,
            args={"target_id": ENTITY_ID, "reason": "unhelpful in review"},
            target_id=ENTITY_ID,
            target_type="precedent",
            requested_by="test",
        )
    )
    assert demoted.status == CommandStatus.SUCCESS, demoted.message
    assert stores.submit()["status"] == "refused_demoted"

    reinstated = stores.executor.execute(
        Command(
            operation=Operation.PRECEDENT_REINSTATE,
            args={"target_id": ENTITY_ID, "reason": "re-reviewed and still useful"},
            target_id=ENTITY_ID,
            target_type="precedent",
            requested_by="test",
        )
    )
    assert reinstated.status == CommandStatus.SUCCESS, reinstated.message
    node = stores.graph.get_node(ENTITY_ID)
    assert node is not None
    assert node["properties"]["lifecycle"]["state"] == "current"
    before = stores.writes()

    second = stores.submit()

    assert second["status"] == ALREADY_PROMOTED
    assert stores.writes() == before
    preview = stores.preview()
    assert preview is not None
    assert preview["status"] == ALREADY_PROMOTED


def test_a_float_and_a_list_property_round_trip_so_a_rerun_skips_them(
    stores: _Stores,
) -> None:
    """A value the store hands back changed would make every re-run write again.

    ``0.1 + 0.2`` is the float whose repr is not short, and the list is not
    sorted, so a backend that rounded or reordered would show here. SQLite
    stores properties as JSON and keeps both exactly; this is the contract the
    skip rests on, pinned for the one backend CI runs.
    """
    properties = {"score": 0.1 + 0.2, "tags": ["b", "a", "c"], "nested": [[2, 1]]}
    assert stores.submit(_entity_payload(**properties))["status"] == "promoted"
    node = stores.graph.get_node(ENTITY_ID)
    assert node is not None
    for key, value in properties.items():
        assert node["properties"][key] == value
    before = stores.writes()

    second = stores.submit(_entity_payload(**properties))

    assert second["status"] == ALREADY_PROMOTED
    assert stores.writes() == before


def test_a_reordered_list_is_a_change_and_writes(stores: _Stores) -> None:
    """List order is content: the comparison is plain equality, not set-like."""
    assert stores.submit(_entity_payload(tags=["a", "b"]))["status"] == "promoted"

    second = stores.submit(_entity_payload(tags=["b", "a"]))

    assert second["status"] == "promoted"
    node = stores.graph.get_node(ENTITY_ID)
    assert node is not None
    assert node["properties"]["tags"] == ["b", "a"]


def test_a_key_since_dropped_from_the_payload_is_not_removed_by_a_rerun(
    stores: _Stores,
) -> None:
    """Deliberate (#996): the comparison reads only keys the payload carries.

    ``ENTITY_CREATE`` replaces the whole properties dict, so before #983 a
    re-run removed a key that had left ``precedent_properties``. It is now
    skipped and the stale key stays: a later writer's key is not content this
    promotion owns, and that cannot be told apart from a dropped one.
    """
    assert stores.submit(_entity_payload(legacy="kept"))["status"] == "promoted"

    second = stores.submit()

    assert second["status"] == ALREADY_PROMOTED
    node = stores.graph.get_node(ENTITY_ID)
    assert node is not None
    assert node["properties"]["legacy"] == "kept"


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


def test_an_edge_whose_only_change_is_a_property_is_written_again(
    stores: _Stores,
) -> None:
    """The edge is present, so a presence-only check called this complete (#996)."""
    assert stores.submit()["status"] == "promoted"
    changed = _edge_payloads()
    changed[0]["properties"]["source_of_truth"] = "re-reviewed"
    assert stores.preview(edges=changed) is None

    second = stores.submit(edges=changed)

    assert second["status"] == "promoted"
    edges = stores.graph.get_edges(ENTITY_ID, direction="outgoing")
    assert [e["properties"]["source_of_truth"] for e in edges] == ["re-reviewed"]
    # Now it holds the new properties, so the next run leaves it alone.
    assert stores.submit(edges=changed)["status"] == ALREADY_PROMOTED


def test_a_property_the_edge_gained_later_does_not_block_the_skip(
    stores: _Stores,
) -> None:
    """Same rule as the node: a key the payload does not carry is not compared."""
    assert stores.submit()["status"] == "promoted"
    stores.graph.upsert_edge(
        ENTITY_ID,
        TARGET_ID,
        "precedent_applies_to",
        {"source_of_truth": "reviewed_promotion", "annotated_by": "a later writer"},
    )
    assert stores.graph.get_edges(ENTITY_ID, direction="outgoing")[0]["properties"][
        "annotated_by"
    ]

    assert stores.submit()["status"] == ALREADY_PROMOTED


def test_a_missing_document_row_is_mended_by_running_it_again(
    stores: _Stores,
) -> None:
    """A legacy precedent has its event and no row for keyword retrieval (#996)."""
    assert stores.submit()["status"] == "promoted"
    assert stores.documents.get(ENTITY_ID) is not None
    assert stores.documents.delete(ENTITY_ID)
    assert stores.preview() is None

    second = stores.submit()

    assert second["status"] == "promoted"
    row = stores.documents.get(ENTITY_ID)
    assert row is not None
    assert row["content"] == precedent_document_content("Rerun precedent", "same")
    # Row restored, so the next run leaves it alone.
    assert stores.submit()["status"] == ALREADY_PROMOTED


def test_a_stale_document_row_is_rewritten_by_running_it_again(
    stores: _Stores,
) -> None:
    """The node already matches but its row carries older text: not complete."""
    assert stores.submit()["status"] == "promoted"
    stores.documents.put(ENTITY_ID, "text from an earlier description", {})

    second = stores.submit()

    assert second["status"] == "promoted"
    row = stores.documents.get(ENTITY_ID)
    assert row is not None
    assert row["content"] == precedent_document_content("Rerun precedent", "same")


def test_a_redaction_tombstone_row_is_not_written_back_by_a_re_run(
    stores: _Stores,
) -> None:
    """A redaction blanks the row before it purges the node; if the purge
    fails, a re-run must not restore the redacted text. It is refused as
    redacted (#1019), not skipped as already promoted."""
    from trellis.mutate.handlers import REDACTION_TOMBSTONE_REASON
    from trellis.schemas.classification import LIFECYCLE_KEY

    assert stores.submit()["status"] == "promoted"
    tombstone = {LIFECYCLE_KEY: {"deprecation_reason": REDACTION_TOMBSTONE_REASON}}
    stores.documents.put(ENTITY_ID, "", tombstone)

    second = stores.submit()

    assert second["status"] == "refused_redacted"
    row = stores.documents.get(ENTITY_ID)
    assert row is not None
    assert row["content"] == ""


def test_without_a_document_store_the_row_cannot_be_checked_so_the_write_goes_ahead(
    stores: _Stores,
) -> None:
    stores.submit()

    second = stores.submit(with_document_store=False)

    assert second["status"] == "promoted"
    assert stores.writes()[:2] == (2, 2)


def test_an_id_outside_the_precedent_namespace_expects_no_row(
    stores: _Stores,
) -> None:
    """``PRECEDENT_PROMOTE`` writes no row there, so a missing one is not a gap."""
    outside = "precedent://elsewhere/rerun"
    payload = _entity_payload()
    payload["entity_id"] = outside
    edges = [{**_edge_payloads()[0], "source_id": outside}]
    assert stores.submit(payload, edges)["status"] == "promoted"
    assert stores.documents.get(outside) is None

    assert stores.submit(payload, edges)["status"] == ALREADY_PROMOTED


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
    event_log = _event_log_with_a_promotion()

    outcome = preview_learning_promotion(
        _entity_payload(),
        [],
        graph_store=stores.graph,
        event_log=event_log,
        document_store=None,
    )

    # The id holds an ``entity`` node, not a precedent, and the event log says a
    # promotion exists: the content comparison alone must not skip the write.
    assert outcome is None


def _event_log_with_a_promotion() -> MagicMock:
    """An event log holding one promote event and, as a real one does, no redaction."""

    def _get_events(*, event_type: EventType | None = None, **_: Any) -> list[Any]:
        if event_type == EventType.REDACTION_APPLIED:
            return []
        return [MagicMock(source="mutation_executor", payload={})]

    event_log = MagicMock()
    event_log.get_events.side_effect = _get_events
    return event_log


def _documents_holding_the_row() -> MagicMock:
    """A document store whose row for ``ENTITY_ID`` reads as the promotion wrote it."""
    documents = MagicMock()
    documents.get.return_value = {
        "doc_id": ENTITY_ID,
        "content": precedent_document_content("Rerun precedent", "same"),
    }
    return documents


def test_a_backend_that_drops_null_properties_still_matches() -> None:
    """Some graph backends cannot store a null property, so it reads back absent."""
    graph = MagicMock()
    graph.get_node.return_value = {
        "node_id": ENTITY_ID,
        "node_type": "precedent",
        "properties": {"name": "Rerun precedent", "description": "same"},
    }
    graph.get_edges.return_value = []
    event_log = _event_log_with_a_promotion()

    outcome = preview_learning_promotion(
        _entity_payload(),
        [],
        graph_store=graph,
        event_log=event_log,
        document_store=_documents_holding_the_row(),
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
    event_log = _event_log_with_a_promotion()

    outcome = preview_learning_promotion(
        _entity_payload(),
        [],
        graph_store=graph,
        event_log=event_log,
        document_store=_documents_holding_the_row(),
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
