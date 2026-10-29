"""A redacted precedent is never promoted again from its learning candidate (#1019).

``redaction.apply`` blanks the precedent's document row and then purges the node.
Once the node is gone, the promotion path found no existing node and read the
candidate as new, so a re-run (``promote-learning``, ``review-ready``, the REST
route or the auto-promote job) wrote the redacted precedent, its row and its
edges back. The refusal is ``refused_redacted``, decided in
``_pre_write_outcome`` so the preview and the submission cannot disagree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.learning.scoring import (
    REFUSED_REDACTED,
    precedent_entity_id_for_candidate,
    preview_learning_promotion,
    promotion_is_refusal,
    promotion_verdict,
    submit_learning_promotion,
)
from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis.mutate.handlers import REDACTION_TOMBSTONE_REASON
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

ENTITY_ID = precedent_entity_id_for_candidate("cand-redact")
TARGET_ID = "entity://target"
REDACTED_TEXT = "the redacted sentence that must never be served again"


def _entity_payload(entity_id: str = ENTITY_ID) -> dict[str, Any]:
    return {
        "entity_type": "precedent",
        "entity_id": entity_id,
        "name": "Redaction precedent",
        "properties": {
            "description": REDACTED_TEXT,
            "approved_rationale": None,
            "approved_at": "2026-01-01T00:00:00+00:00",
        },
    }


def _edge_payloads(entity_id: str = ENTITY_ID) -> list[dict[str, Any]]:
    return [
        {
            "source_id": entity_id,
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
        self.executor = build_curate_executor(self.registry, clearance="restricted")
        self.graph = self.registry.knowledge.graph_store
        self.events = self.registry.operational.event_log
        self.documents = self.registry.knowledge.document_store
        self.graph.upsert_node(TARGET_ID, "entity", {"name": "target"})

    def submit(
        self,
        *,
        entity_id: str = ENTITY_ID,
        with_event_log: bool = True,
        with_document_store: bool = True,
    ) -> dict[str, Any]:
        return submit_learning_promotion(
            self.executor,
            _entity_payload(entity_id),
            _edge_payloads(entity_id),
            requested_by="test",
            graph_store=self.graph,
            event_log=self.events if with_event_log else None,
            document_store=self.documents if with_document_store else None,
        )

    def preview(self, *, entity_id: str = ENTITY_ID) -> dict[str, Any] | None:
        return preview_learning_promotion(
            _entity_payload(entity_id),
            _edge_payloads(entity_id),
            graph_store=self.graph,
            event_log=self.events,
            document_store=self.documents,
        )

    def redact(self, target_id: str = ENTITY_ID) -> None:
        result = self.executor.execute(
            Command(
                operation=Operation.REDACTION_APPLY,
                args={"target_id": target_id, "reason": "owner request"},
                target_id=target_id,
                target_type="entity",
                requested_by="test",
            )
        )
        assert result.status == CommandStatus.SUCCESS, result.message

    def promote_events(self, entity_id: str = ENTITY_ID) -> int:
        return len(
            self.events.get_events(
                event_type=EventType.PRECEDENT_PROMOTED, entity_id=entity_id
            )
        )

    def executed_events(self) -> int:
        return len(self.events.get_events(event_type=EventType.MUTATION_EXECUTED))

    def tombstone_only(self) -> None:
        """What a redaction leaves when it blanked the row and did no more."""
        self.documents.put(
            ENTITY_ID,
            "",
            {LIFECYCLE_KEY: {"deprecation_reason": REDACTION_TOMBSTONE_REASON}},
        )


@pytest.fixture
def stores(tmp_path: Path):
    s = _Stores(tmp_path)
    yield s
    s.registry.close()


def test_a_redacted_precedent_is_refused_and_nothing_is_written(
    stores: _Stores,
) -> None:
    first = stores.submit()
    assert first["status"] == "promoted", first
    row = stores.documents.get(ENTITY_ID)
    assert row is not None
    assert REDACTED_TEXT in row["content"]
    assert stores.promote_events() == 1

    stores.redact()

    # The redaction purged the node: this is the state that read as "new".
    assert stores.graph.get_node(ENTITY_ID) is None
    executed_before = stores.executed_events()

    second = stores.submit()

    assert second["status"] == REFUSED_REDACTED
    assert second["entity_id"] == ENTITY_ID
    assert REDACTED_TEXT not in second["message"]
    assert promotion_is_refusal(second)
    assert promotion_verdict([second]) == "refused"
    # No new node version, no row text, no promote event, no executor command.
    assert stores.graph.get_node(ENTITY_ID) is None
    assert stores.graph.get_node_history(ENTITY_ID) == []
    assert stores.graph.get_edges(ENTITY_ID, direction="outgoing") == []
    row = stores.documents.get(ENTITY_ID)
    assert row is not None
    assert row["content"] == ""
    assert stores.promote_events() == 1
    assert stores.executed_events() == executed_before


def test_the_preview_reports_the_refusal_the_submission_answers(
    stores: _Stores,
) -> None:
    stores.submit()
    assert stores.preview()["status"] == "already_promoted"  # type: ignore[index]

    stores.redact()
    preview = stores.preview()

    assert preview is not None
    assert preview["status"] == REFUSED_REDACTED
    assert preview["status"] == stores.submit()["status"]


def test_a_tombstone_alone_refuses_when_the_node_purge_failed(stores: _Stores) -> None:
    """The row is blanked first; a purge that then fails leaves node and row."""
    assert stores.submit()["status"] == "promoted"
    stores.tombstone_only()
    before = stores.executed_events()

    second = stores.submit()

    assert second["status"] == REFUSED_REDACTED
    assert stores.preview() == second
    row = stores.documents.get(ENTITY_ID)
    assert row is not None
    assert row["content"] == ""
    assert stores.executed_events() == before


def test_a_tombstone_refuses_without_an_event_log(stores: _Stores) -> None:
    """The row is the trace that needs no log, so a caller that passes none
    (or a deployment whose emit failed) is still refused."""
    stores.tombstone_only()

    assert stores.submit(with_event_log=False)["status"] == REFUSED_REDACTED
    assert stores.promote_events() == 0


def test_the_event_alone_refuses_without_a_document_row_to_read(
    stores: _Stores,
) -> None:
    """The event is the trace that needs no row: no document store is passed."""
    stores.submit()
    stores.redact()

    assert stores.submit(with_document_store=False)["status"] == REFUSED_REDACTED


def test_the_event_alone_refuses_when_the_row_is_not_a_tombstone(
    stores: _Stores,
) -> None:
    stores.submit()
    stores.redact()
    stores.documents.put(ENTITY_ID, "someone wrote text back", {})

    assert stores.submit()["status"] == REFUSED_REDACTED


def test_the_lookup_is_one_row_by_type_and_id() -> None:
    graph = MagicMock()
    graph.get_node.return_value = None
    events = MagicMock()
    events.get_events.return_value = [object()]

    outcome = preview_learning_promotion(
        _entity_payload(),
        [],
        graph_store=graph,
        event_log=events,
        document_store=None,
    )

    assert outcome is not None
    assert outcome["status"] == REFUSED_REDACTED
    events.get_events.assert_called_once_with(
        event_type=EventType.REDACTION_APPLIED, entity_id=ENTITY_ID, limit=1
    )
    graph.get_node.assert_not_called()


def test_an_event_log_that_cannot_be_read_fails_the_write_rather_than_passing_it() -> (
    None
):
    events = MagicMock()
    events.get_events.side_effect = RuntimeError("event log unavailable")
    executor = MagicMock()

    with pytest.raises(RuntimeError, match="unavailable"):
        submit_learning_promotion(
            executor,
            _entity_payload(),
            [],
            requested_by="test",
            graph_store=MagicMock(),
            event_log=events,
            document_store=None,
        )

    executor.execute.assert_not_called()


def test_a_redaction_of_another_id_does_not_block_a_promotion(stores: _Stores) -> None:
    stores.graph.upsert_node("bystander", "entity", {"name": "bystander"})
    stores.redact("bystander")

    assert stores.preview() is None
    assert stores.submit()["status"] == "promoted"
