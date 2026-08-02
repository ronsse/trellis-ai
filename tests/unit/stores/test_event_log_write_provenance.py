"""Write-provenance stamping on the event log.

The stamp rides in ``Event.metadata`` — already a free-form
``dict[str, Any]`` on every backend — precisely so that it is additive:
payload models keep their ``extra="forbid"`` contract untouched, and rows
written before the stamp existed still parse. Both halves are asserted
here, because a strict model that rejected historical rows would be a far
worse regression than the missing attribution it was fixing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from trellis.core.write_config import ENV_VAR_BY_FIELD
from trellis.core.write_provenance import (
    WRITE_PROVENANCE_KEY,
    get_write_provenance,
    reset_write_provenance_cache,
)
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def event_log(tmp_path: Path) -> Iterator[SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VAR_BY_FIELD.values():
        monkeypatch.delenv(name, raising=False)
    reset_write_provenance_cache()


class TestEmitStamps:
    def test_emitted_event_carries_the_stamp(self, event_log: SQLiteEventLog) -> None:
        event = event_log.emit(EventType.ENTITY_CREATED, "curate", entity_id="e1")
        assert event.metadata[WRITE_PROVENANCE_KEY] == get_write_provenance()

    def test_stamp_survives_the_store_round_trip(
        self, event_log: SQLiteEventLog
    ) -> None:
        event_log.emit(EventType.ENTITY_CREATED, "curate", entity_id="e1")
        (stored,) = event_log.get_events()
        assert stored.metadata[WRITE_PROVENANCE_KEY]["version"]
        assert stored.metadata[WRITE_PROVENANCE_KEY]["flags_digest"]

    def test_stamp_records_the_flags_in_effect(
        self, event_log: SQLiteEventLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: two writes under different semantics differ."""
        event_log.emit(EventType.MEMORY_STORED, "mcp", entity_id="d1")
        monkeypatch.setenv(ENV_VAR_BY_FIELD["classify_on_ingest"], "1")
        reset_write_provenance_cache()
        event_log.emit(EventType.MEMORY_STORED, "mcp", entity_id="d2")

        first, second = event_log.get_events()
        assert first.metadata[WRITE_PROVENANCE_KEY]["flags"]["classify_on_ingest"] is (
            False
        )
        assert second.metadata[WRITE_PROVENANCE_KEY]["flags"]["classify_on_ingest"] is (
            True
        )
        assert (
            first.metadata[WRITE_PROVENANCE_KEY]["flags_digest"]
            != second.metadata[WRITE_PROVENANCE_KEY]["flags_digest"]
        )

    def test_caller_metadata_is_preserved_alongside(
        self, event_log: SQLiteEventLog
    ) -> None:
        event_log.emit(
            EventType.ENTITY_CREATED,
            "curate",
            metadata={"agent": "claude"},
        )
        (stored,) = event_log.get_events()
        assert stored.metadata["agent"] == "claude"
        assert WRITE_PROVENANCE_KEY in stored.metadata

    def test_typed_payload_is_untouched(self, event_log: SQLiteEventLog) -> None:
        """An ``extra="forbid"`` payload model still round-trips exactly."""
        payload = MemoryOpJudgedPayload(
            op_type=JudgedOpType.RECONCILIATION,
            model_id="hermes3:8b",
            input_digest=InputDigest(hash="abc123", length=42),
            decision="add",
            confidence=0.8,
            subject_ref=SubjectRef(ref_type="doc", ref_id="doc_1"),
        )
        event_log.emit(
            EventType.MEMORY_OP_JUDGED,
            "reconcile",
            payload=payload.model_dump(mode="json"),
        )
        (stored,) = event_log.get_events()
        assert MemoryOpJudgedPayload.model_validate(stored.payload) == payload

    def test_mutation_executor_events_are_stamped(
        self, event_log: SQLiteEventLog
    ) -> None:
        """The governed write pipeline emits through ``emit``, so it inherits."""

        class _Handler:
            def handle(self, command: Command) -> tuple[str | None, str]:
                return "ent_1", "ok"

        executor = MutationExecutor(event_log=event_log)
        executor.register_handler(Operation.ENTITY_CREATE, _Handler())
        result: CommandResult = executor.execute(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={"entity_type": "service", "name": "auth"},
            )
        )
        assert result.status is CommandStatus.SUCCESS
        stored = event_log.get_events(event_type=EventType.MUTATION_EXECUTED)
        assert stored
        assert WRITE_PROVENANCE_KEY in stored[0].metadata


class TestUnstampedRowsStillRead:
    """Every row already in the store predates the stamp."""

    def test_direct_append_is_not_forced_to_supply_one(
        self, event_log: SQLiteEventLog
    ) -> None:
        """The stamp is never a required field."""
        event = Event(event_type=EventType.TRACE_INGESTED, source="ingest")
        event_log.append(event)
        (stored,) = event_log.get_events()
        assert stored.metadata == {}

    def test_historical_row_with_foreign_metadata_parses(
        self, event_log: SQLiteEventLog
    ) -> None:
        """Pre-existing metadata keys read back untouched, stamp or not."""
        event_log.append(
            Event(
                event_type=EventType.FEEDBACK_RECORDED,
                source="feedback",
                entity_id="pack_1",
                payload={"success": True},
                metadata={"agent": "claude", "legacy_key": 7},
            )
        )
        (stored,) = event_log.get_events()
        assert stored.metadata == {"agent": "claude", "legacy_key": 7}
        assert WRITE_PROVENANCE_KEY not in stored.metadata

    def test_raw_legacy_row_written_before_the_column_convention(
        self, tmp_path: Path
    ) -> None:
        """A row inserted straight into the table, as an old build left it."""
        db_path = tmp_path / "events.db"
        log = SQLiteEventLog(db_path)
        try:
            # Establish the schema, then hand-write a stamp-free row the way a
            # pre-provenance build would have.
            log.append(Event(event_type=EventType.SYSTEM_INITIALIZED, source="init"))
            conn = log._conn  # legacy-row simulation: reach past the API on purpose
            conn.execute(
                "UPDATE events SET metadata_json = ?",
                (json.dumps({"agent": "old-build"}),),
            )
            conn.commit()
            (stored,) = log.get_events()
            assert stored.metadata == {"agent": "old-build"}
        finally:
            log.close()

    def test_mixed_stamped_and_unstamped_rows_coexist(
        self, event_log: SQLiteEventLog
    ) -> None:
        event_log.append(Event(event_type=EventType.TRACE_INGESTED, source="ingest"))
        event_log.emit(EventType.TRACE_INGESTED, "ingest")
        rows = event_log.get_events(event_type=EventType.TRACE_INGESTED)
        assert len(rows) == 2
        assert [WRITE_PROVENANCE_KEY in row.metadata for row in rows] == [False, True]
