"""Precedent promote, demote and reinstate say so when the row is a redaction tombstone.

A redaction blanks a precedent's document row and archives it. Three lifecycle
verbs then meet that row:

* ``precedent.promote`` is refused over it, as it always was, but with the
  seam's message about a re-ingest flag that the command cannot set. It now
  refuses with ``precedent_document_redacted`` and names the real remedy.
* ``precedent.demote`` and ``precedent.reinstate`` skip the row, as they always
  did, but left no record of the skip. They now add a result warning and a
  ``document_row`` event value. A reinstate whose whole job is the row stamp
  (``document_resync_only``) is refused instead of reporting a repair it did
  not make.

No pack gains or loses an item: the row stays withheld either way.

Defective subjects (each hand-edit is restored by re-editing):

* drop the tombstone check in ``_write_precedent_document``;
* collapse ``_stamp_precedent_document`` back to a ``bool``;
* drop the ``strict`` raise in ``PrecedentReinstateHandler._restamp_document``;
* set ``document_row`` on the ``"no_row"`` outcome too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.redaction_tombstone import make_tombstone, tombstone_metadata
from trellis.core.document_write import is_redaction_tombstone, put_document
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

TITLE = "Quokka widget pooling"
DESCRIPTION = "Reuse pooled widgets across requests."
NODE_ID = "precedent://learning/quokka"
CODE = "precedent_document_redacted"
SKIPPED = "skipped_redaction_tombstone"


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _execute(
    registry: StoreRegistry, operation: Operation, args: dict[str, Any]
) -> CommandResult:
    return build_curate_executor(registry, clearance="restricted").execute(
        Command(
            operation=operation,
            args=args,
            target_id=NODE_ID,
            target_type="entity",
            requested_by="test",
        )
    )


def _promote(registry: StoreRegistry) -> CommandResult:
    return _execute(
        registry,
        Operation.PRECEDENT_PROMOTE,
        {"title": TITLE, "description": DESCRIPTION, "entity_type": "precedent"},
    )


def _demote(registry: StoreRegistry) -> CommandResult:
    return _execute(
        registry,
        Operation.PRECEDENT_DEMOTE,
        {"target_id": NODE_ID, "reason": "superseded"},
    )


def _reinstate(registry: StoreRegistry) -> CommandResult:
    return _execute(
        registry,
        Operation.PRECEDENT_REINSTATE,
        {"target_id": NODE_ID, "reason": "reviewed again"},
    )


def _node(registry: StoreRegistry) -> None:
    registry.knowledge.graph_store.upsert_node(
        NODE_ID, "precedent", {"name": TITLE, "description": DESCRIPTION}
    )


def _row(registry: StoreRegistry) -> dict[str, Any] | None:
    return registry.knowledge.document_store.get(NODE_ID)


def _tombstone(registry: StoreRegistry) -> dict[str, Any]:
    """Blank the node's document row the way a redaction does; the row now."""
    make_tombstone(
        registry.knowledge.document_store, registry.knowledge.vector_store, NODE_ID
    )
    row = _row(registry)
    assert row is not None
    assert is_redaction_tombstone(row)
    return row


def _blank_existing_row(registry: StoreRegistry) -> dict[str, Any]:
    """Overwrite a live row with the blank archived one, as a redaction does."""
    put_document(
        registry.knowledge.document_store,
        registry.knowledge.vector_store,
        NODE_ID,
        "",
        tombstone_metadata(),
        preserve_updated_at=False,
    )
    row = _row(registry)
    assert row is not None
    assert is_redaction_tombstone(row)
    return row


def _events(registry: StoreRegistry, event_type: EventType) -> list[Any]:
    return registry.operational.event_log.get_events(event_type=event_type)


def _node_state(registry: StoreRegistry) -> str | None:
    node = registry.knowledge.graph_store.get_node(NODE_ID)
    assert node is not None
    record = node["properties"].get(LIFECYCLE_KEY)
    return record.get("state") if isinstance(record, dict) else None


def _skip_warnings(result: CommandResult) -> list[str]:
    return [
        w
        for w in result.warnings
        if w.startswith("precedent_document_stamp_skipped_redacted")
    ]


class TestPromoteOverATombstone:
    def test_is_refused_with_its_own_code_and_a_message_it_can_act_on(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry)
        before = _tombstone(registry)

        result = _promote(registry)

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CODE
        assert NODE_ID in result.message
        assert "redaction.apply" in result.message
        assert "reingest-redacted" not in result.message
        assert "reingest_redacted" not in result.message
        assert _row(registry) == before

    def test_emits_no_promoted_event(self, registry: StoreRegistry) -> None:
        # The write runs before the emit, so a raise from it leaves no event: a
        # promotion that did not land must not read as one in the log.
        _node(registry)
        _tombstone(registry)

        _promote(registry)

        assert not _events(registry, EventType.PRECEDENT_PROMOTED)

    def test_a_live_row_is_still_written(self, registry: StoreRegistry) -> None:
        _node(registry)
        registry.knowledge.document_store.put(NODE_ID, "stale text", {})

        result = _promote(registry)

        assert result.status == CommandStatus.SUCCESS
        row = _row(registry)
        assert row is not None
        assert row["content"] == f"{TITLE}\n\n{DESCRIPTION}"
        assert _events(registry, EventType.PRECEDENT_PROMOTED)


class TestDemoteOverATombstone:
    def test_demotes_the_node_and_records_the_skipped_row(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry)
        before = _tombstone(registry)

        result = _demote(registry)

        assert result.status == CommandStatus.SUCCESS
        assert _node_state(registry) == "demoted"
        assert _row(registry) == before
        (warning,) = _skip_warnings(result)
        assert NODE_ID in warning
        assert "redaction.apply" in warning
        (event,) = _events(registry, EventType.PRECEDENT_DEMOTED)
        assert event.payload["document_row"] == SKIPPED

    def test_no_row_records_nothing(self, registry: StoreRegistry) -> None:
        _node(registry)
        assert _row(registry) is None

        result = _demote(registry)

        assert result.status == CommandStatus.SUCCESS
        assert _node_state(registry) == "demoted"
        assert not _skip_warnings(result)
        (event,) = _events(registry, EventType.PRECEDENT_DEMOTED)
        assert "document_row" not in event.payload
        assert _row(registry) is None

    def test_a_live_row_records_nothing_and_is_stamped(
        self, registry: StoreRegistry
    ) -> None:
        _node(registry)
        assert _promote(registry).status == CommandStatus.SUCCESS

        result = _demote(registry)

        assert result.status == CommandStatus.SUCCESS
        assert not _skip_warnings(result)
        (event,) = _events(registry, EventType.PRECEDENT_DEMOTED)
        assert "document_row" not in event.payload
        row = _row(registry)
        assert row is not None
        assert row["metadata"][LIFECYCLE_KEY]["state"] == "demoted"


class TestReinstateOverATombstone:
    def _demoted_then_redacted(self, registry: StoreRegistry) -> dict[str, Any]:
        _node(registry)
        assert _promote(registry).status == CommandStatus.SUCCESS
        assert _demote(registry).status == CommandStatus.SUCCESS
        return _blank_existing_row(registry)

    def test_fail_soft_reinstates_the_node_and_records_the_skipped_row(
        self, registry: StoreRegistry
    ) -> None:
        before = self._demoted_then_redacted(registry)

        result = _reinstate(registry)

        assert result.status == CommandStatus.SUCCESS
        assert _node_state(registry) == "current"
        assert _row(registry) == before
        (warning,) = _skip_warnings(result)
        assert NODE_ID in warning
        assert "redaction.apply" in warning
        (event,) = _events(registry, EventType.PRECEDENT_REINSTATED)
        assert event.payload["document_row"] == SKIPPED
        assert "document_sync_failed" not in event.payload
        assert "document_resync_only" not in event.payload

    def test_no_row_records_nothing(self, registry: StoreRegistry) -> None:
        _node(registry)
        assert _demote(registry).status == CommandStatus.SUCCESS
        assert _row(registry) is None

        result = _reinstate(registry)

        assert result.status == CommandStatus.SUCCESS
        assert _node_state(registry) == "current"
        assert not _skip_warnings(result)
        (event,) = _events(registry, EventType.PRECEDENT_REINSTATED)
        assert "document_row" not in event.payload

    def test_a_resync_only_repair_is_refused_rather_than_reported_as_done(
        self, registry: StoreRegistry
    ) -> None:
        # ``document_resync_only`` needs the node current and the row stamped
        # ``demoted``. The row a redaction writes is stamped ``archived``, so
        # this shape is hand-built: a demoted stamp that is also blank and
        # carries the redaction reason.
        _node(registry)
        put_document(
            registry.knowledge.document_store,
            registry.knowledge.vector_store,
            NODE_ID,
            "",
            {
                LIFECYCLE_KEY: Lifecycle(
                    state="demoted", deprecation_reason="redacted"
                ).model_dump(mode="json")
            },
            preserve_updated_at=False,
        )
        before = _row(registry)
        assert before is not None
        assert is_redaction_tombstone(before)

        result = _reinstate(registry)

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CODE
        assert _row(registry) == before
        assert not _events(registry, EventType.PRECEDENT_REINSTATED)

    def test_a_real_tombstone_never_enters_the_resync_only_repair(
        self, registry: StoreRegistry
    ) -> None:
        # The tombstone redaction writes is stamped ``archived``, so a current
        # node beside it is refused by the entry check before any stamp runs.
        _node(registry)
        before = _tombstone(registry)

        result = _reinstate(registry)

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "target_not_demoted"
        assert _row(registry) == before
