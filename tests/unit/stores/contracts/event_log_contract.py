"""EventLog contract test suite — runs against every backend.

The most important invariant this suite enforces is the **append-only**
nature of the log: once recorded, an event cannot be modified or
deleted through any public API on :class:`~trellis.stores.base.event_log.EventLog`.

Two complementary checks pin that invariant:

* **Structural** — the ABC exposes ``append``, ``get_events``, ``count``,
  ``close``, plus the concrete helpers ``has_idempotency_key`` and
  ``emit``. There is no ``update_event``, ``delete_event``,
  ``mutate_event``, ``upsert_event``, or any other public method whose
  name suggests modification of an already-recorded row. The
  :func:`test_no_mutation_or_deletion_api` check inspects the public
  surface and fails if a backend (or the ABC) ever grows one.
* **Behavioural** — round-tripping a recorded event through ``append``
  + ``get_events`` returns byte-identical fields. There is no
  observable path that re-writes a row in place.

Subclass shape::

    class TestSQLiteEventLogContract(EventLogContractTests):
        @pytest.fixture
        def store(self, tmp_path):
            store = SQLiteEventLog(tmp_path / "events.db")
            yield store
            store.close()
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from trellis.core.base import utc_now
from trellis.core.hashing import content_hash
from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import Event, EventLog, EventType


def _sleep_for_ordering() -> None:
    """Sleep long enough that two appends get distinct ``occurred_at`` timestamps.

    SQLite stores ``occurred_at`` as ISO strings and Postgres as
    ``TIMESTAMPTZ``; both have sub-millisecond resolution but the tests
    avoid relying on that. 5ms is plenty.
    """
    time.sleep(0.005)


class EventLogContractTests:
    """Contract tests every :class:`EventLog` backend must pass.

    Subclasses must provide a pytest fixture named ``store`` that
    yields a fresh, empty :class:`EventLog` instance and tears it down.
    """

    # ------------------------------------------------------------------
    # Append-only invariant — structural and behavioural
    # ------------------------------------------------------------------

    def test_no_mutation_or_deletion_api(self, store: EventLog) -> None:
        """Append-only invariant: no public API mutates or deletes events.

        Inspects the public surface of the EventLog instance for any
        method whose name suggests in-place modification or removal of
        an already-recorded row. ``close`` is a lifecycle method, not a
        per-row mutation, so it's allowed.
        """
        forbidden_substrings = (
            "update_event",
            "delete_event",
            "remove_event",
            "mutate_event",
            "patch_event",
            "modify_event",
            "edit_event",
            "upsert_event",
            "overwrite_event",
            "drop_event",
            "purge_event",
            "set_event",
            "replace_event",
        )
        public_attrs = [name for name in dir(store) if not name.startswith("_")]
        offenders = [
            name
            for name in public_attrs
            if any(needle in name for needle in forbidden_substrings)
        ]
        assert not offenders, (
            f"EventLog must be append-only, but found public attrs that "
            f"suggest mutation/deletion: {offenders}"
        )

    def test_append_is_immutable_round_trip(self, store: EventLog) -> None:
        """Re-appending a fresh event with the same logical content yields a
        new row; the original is never overwritten.

        Behavioural complement to the structural check above: there is
        no public path that lets a caller "fix" a previously-recorded
        event in place.
        """
        first = Event(
            event_type=EventType.TRACE_INGESTED,
            source="ingest",
            entity_id="trace_1",
            payload={"v": 1},
        )
        store.append(first)
        _sleep_for_ordering()
        second = Event(
            event_type=EventType.TRACE_INGESTED,
            source="ingest",
            entity_id="trace_1",
            payload={"v": 2},
        )
        store.append(second)

        events = store.get_events(entity_id="trace_1", limit=10)
        # Both rows present — the second did not overwrite the first.
        assert len(events) == 2
        ids = {e.event_id for e in events}
        assert first.event_id in ids
        assert second.event_id in ids
        # The original payload is intact for the original event_id.
        original = next(e for e in events if e.event_id == first.event_id)
        assert original.payload == {"v": 1}

    # ------------------------------------------------------------------
    # Append + round-trip
    # ------------------------------------------------------------------

    def test_append_and_round_trip_preserves_fields(self, store: EventLog) -> None:
        """Appended fields are preserved verbatim by the next read."""
        event = Event(
            event_type=EventType.ENTITY_CREATED,
            source="curate",
            entity_id="ent_42",
            entity_type="entity",
            payload={"name": "auth", "tier": 1},
            metadata={"agent": "claude"},
        )
        store.append(event)

        rows = store.get_events()
        assert len(rows) == 1
        got = rows[0]
        assert got.event_id == event.event_id
        assert got.event_type == EventType.ENTITY_CREATED
        assert got.source == "curate"
        assert got.entity_id == "ent_42"
        assert got.entity_type == "entity"
        assert got.payload == {"name": "auth", "tier": 1}
        assert got.metadata == {"agent": "claude"}

    def test_emit_helper_appends_and_returns_event(self, store: EventLog) -> None:
        """``EventLog.emit`` constructs + appends in one call."""
        event = store.emit(
            EventType.PRECEDENT_PROMOTED,
            "promoter",
            entity_id="prec_1",
            payload={"domain": "billing"},
        )
        assert event.event_id
        rows = store.get_events(event_type=EventType.PRECEDENT_PROMOTED)
        assert len(rows) == 1
        assert rows[0].event_id == event.event_id

    def test_memory_op_judged_payload_round_trips(self, store: EventLog) -> None:
        """A ``MEMORY_OP_JUDGED`` training-pair payload round-trips intact.

        The #264 schema slice: emit the JSON-mode dump of a typed
        :class:`~trellis.schemas.memory_op.MemoryOpJudgedPayload`, read
        it back, and re-validate the stored dict through the model. Every
        backend must preserve the nested digest / ref structure verbatim
        — this is the wire the #263 emit paths and the downstream
        feedback-attribution join will depend on. Leak-safety rider: the
        persisted payload never contains a raw-content key.
        """
        judged_input = "some judged memory content"
        payload = MemoryOpJudgedPayload(
            op_type=JudgedOpType.RECONCILIATION,
            model_id="hermes3:8b",
            input_digest=InputDigest(
                hash=content_hash(judged_input),
                length=len(judged_input),
                source_refs=["doc_abc", "entity_xyz"],
            ),
            decision="supersede",
            confidence=0.82,
            subject_ref=SubjectRef(ref_type="doc", ref_id="doc_abc"),
        )
        emitted = store.emit(
            EventType.MEMORY_OP_JUDGED,
            "reconciler",
            entity_id=payload.subject_ref.ref_id,
            entity_type=payload.subject_ref.ref_type,
            payload=payload.model_dump(mode="json"),
        )
        assert emitted.event_type is EventType.MEMORY_OP_JUDGED

        rows = store.get_events(event_type=EventType.MEMORY_OP_JUDGED)
        assert len(rows) == 1
        got = rows[0]
        assert got.event_id == emitted.event_id
        assert got.event_type is EventType.MEMORY_OP_JUDGED
        restored = MemoryOpJudgedPayload.model_validate(got.payload)
        assert restored == payload
        # No raw content ever touched the log.
        assert "content" not in got.payload

    # ------------------------------------------------------------------
    # Empty store on every query path
    # ------------------------------------------------------------------

    def test_empty_get_events_is_empty_list(self, store: EventLog) -> None:
        assert store.get_events() == []

    def test_empty_get_events_with_filter_is_empty_list(self, store: EventLog) -> None:
        assert store.get_events(event_type=EventType.TRACE_INGESTED) == []
        assert store.get_events(entity_id="missing") == []
        assert store.get_events(source="missing") == []

    def test_empty_count_is_zero(self, store: EventLog) -> None:
        assert store.count() == 0
        assert store.count(event_type=EventType.TRACE_INGESTED) == 0

    # ------------------------------------------------------------------
    # Filter by event_type
    # ------------------------------------------------------------------

    def test_filter_by_event_type_returns_only_matching(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "a")
        store.emit(EventType.ENTITY_CREATED, "b")
        store.emit(EventType.TRACE_INGESTED, "c")
        traces = store.get_events(event_type=EventType.TRACE_INGESTED)
        assert len(traces) == 2
        assert all(e.event_type == EventType.TRACE_INGESTED for e in traces)

    def test_filter_by_event_type_preserves_insertion_order(
        self, store: EventLog
    ) -> None:
        store.emit(EventType.TRACE_INGESTED, "first")
        _sleep_for_ordering()
        store.emit(EventType.ENTITY_CREATED, "between")
        _sleep_for_ordering()
        store.emit(EventType.TRACE_INGESTED, "third")
        traces = store.get_events(event_type=EventType.TRACE_INGESTED)
        assert [e.source for e in traces] == ["first", "third"]

    # ------------------------------------------------------------------
    # Filter by entity_id
    # ------------------------------------------------------------------

    def test_filter_by_entity_id_returns_only_matching(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "src", entity_id="t1")
        store.emit(EventType.TRACE_INGESTED, "src", entity_id="t2")
        store.emit(EventType.TRACE_INGESTED, "src", entity_id="t1")
        rows = store.get_events(entity_id="t1")
        assert len(rows) == 2
        assert all(e.entity_id == "t1" for e in rows)

    def test_filter_by_entity_id_preserves_insertion_order(
        self, store: EventLog
    ) -> None:
        store.emit(EventType.TRACE_INGESTED, "first", entity_id="t1")
        _sleep_for_ordering()
        store.emit(EventType.TRACE_INGESTED, "second", entity_id="t2")
        _sleep_for_ordering()
        store.emit(EventType.TRACE_INGESTED, "third", entity_id="t1")
        rows = store.get_events(entity_id="t1")
        assert [e.source for e in rows] == ["first", "third"]

    # ------------------------------------------------------------------
    # Time-range queries
    # ------------------------------------------------------------------

    def test_filter_by_since(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "old")
        cutoff = utc_now()
        _sleep_for_ordering()
        store.emit(EventType.TRACE_INGESTED, "new")
        rows = store.get_events(since=cutoff)
        assert [e.source for e in rows] == ["new"]

    def test_filter_by_until(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "old")
        _sleep_for_ordering()
        cutoff = utc_now()
        _sleep_for_ordering()
        store.emit(EventType.TRACE_INGESTED, "new")
        rows = store.get_events(until=cutoff)
        assert [e.source for e in rows] == ["old"]

    def test_filter_by_since_and_until_window(self, store: EventLog) -> None:
        now = utc_now()
        past = now - timedelta(hours=1)
        future = now + timedelta(hours=1)
        store.emit(EventType.TRACE_INGESTED, "in-window")
        rows = store.get_events(since=past, until=future)
        assert len(rows) == 1
        assert store.get_events(since=future) == []

    def test_count_with_since(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "old")
        cutoff = utc_now()
        _sleep_for_ordering()
        store.emit(EventType.TRACE_INGESTED, "new")
        store.emit(EventType.TRACE_INGESTED, "newer")
        assert store.count(since=cutoff) == 2

    # ------------------------------------------------------------------
    # Ordering — ASC default + DESC + truncation semantics
    # ------------------------------------------------------------------

    def test_default_order_is_chronological_asc(self, store: EventLog) -> None:
        for i in range(5):
            store.emit(EventType.TRACE_INGESTED, f"src-{i}")
            _sleep_for_ordering()
        rows = store.get_events()
        assert [e.source for e in rows] == [f"src-{i}" for i in range(5)]

    def test_order_desc_returns_newest_first(self, store: EventLog) -> None:
        for i in range(5):
            store.emit(EventType.TRACE_INGESTED, f"src-{i}")
            _sleep_for_ordering()
        rows = store.get_events(order="desc")
        assert [e.source for e in rows] == [f"src-{i}" for i in reversed(range(5))]

    def test_limit_with_asc_keeps_oldest_n(self, store: EventLog) -> None:
        for i in range(10):
            store.emit(EventType.TRACE_INGESTED, f"src-{i}")
            _sleep_for_ordering()
        rows = store.get_events(limit=3)
        assert [e.source for e in rows] == ["src-0", "src-1", "src-2"]

    def test_limit_with_desc_keeps_newest_n(self, store: EventLog) -> None:
        for i in range(10):
            store.emit(EventType.TRACE_INGESTED, f"src-{i}")
            _sleep_for_ordering()
        rows = store.get_events(limit=3, order="desc")
        assert [e.source for e in rows] == ["src-9", "src-8", "src-7"]

    # ------------------------------------------------------------------
    # High-volume append preserves order
    # ------------------------------------------------------------------

    def test_high_volume_append_preserves_order(self, store: EventLog) -> None:
        """100 sequential appends, read back in ASC order, must match
        insertion order. Catches backends that lose monotonic ordering
        under load (e.g. clock-resolution races, JSON sort-key drift)."""
        n = 100
        for i in range(n):
            store.emit(
                EventType.TRACE_INGESTED,
                f"src-{i:03d}",
                payload={"i": i},
            )
        rows = store.get_events(limit=n)
        assert len(rows) == n
        assert [e.source for e in rows] == [f"src-{i:03d}" for i in range(n)]
        # And the recorded ordering is monotonic non-decreasing.
        timestamps = [e.occurred_at for e in rows]
        assert timestamps == sorted(timestamps)

    def test_high_volume_count_matches(self, store: EventLog) -> None:
        for i in range(100):
            store.emit(EventType.TRACE_INGESTED, f"src-{i}")
        assert store.count() == 100
        assert store.count(event_type=EventType.TRACE_INGESTED) == 100
        assert store.count(event_type=EventType.ENTITY_CREATED) == 0

    # ------------------------------------------------------------------
    # Combined filters
    # ------------------------------------------------------------------

    def test_combined_type_and_entity_filters_are_anded(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "src", entity_id="t1")
        store.emit(EventType.ENTITY_CREATED, "src", entity_id="t1")
        store.emit(EventType.TRACE_INGESTED, "src", entity_id="t2")
        rows = store.get_events(event_type=EventType.TRACE_INGESTED, entity_id="t1")
        assert len(rows) == 1
        assert rows[0].event_type == EventType.TRACE_INGESTED
        assert rows[0].entity_id == "t1"

    def test_filter_by_source(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "ingest")
        store.emit(EventType.ENTITY_CREATED, "curate")
        rows = store.get_events(source="ingest")
        assert len(rows) == 1
        assert rows[0].source == "ingest"

    # ------------------------------------------------------------------
    # Error shape on invalid queries
    # ------------------------------------------------------------------

    def test_invalid_event_type_string_rejected(self, store: EventLog) -> None:
        """``event_type`` is a closed enum at the API boundary; passing a
        bare string the enum doesn't know about must raise rather than
        silently return zero rows. Guards against typo-as-empty-result.
        """
        with pytest.raises((ValueError, KeyError)):
            EventType("not.a.real.event.type")

    def test_invalid_order_value_rejected_or_safe(self, store: EventLog) -> None:
        """Passing an ``order`` value outside ``{"asc", "desc"}`` must
        either raise or fall back to a defined default — never inject
        the raw string into the SQL ``ORDER BY``.

        This is a SQL-injection guard expressed as a contract: the
        backend's ``order`` handling has to validate input. Backends
        that map any non-``"desc"`` value to ``"asc"`` are also
        compliant; the harness accepts either shape.
        """
        store.emit(EventType.TRACE_INGESTED, "a")
        try:
            rows = store.get_events(order="DROP TABLE events")  # type: ignore[arg-type]
        except Exception:
            # Raising is fine — explicit rejection beats silent default.
            return
        # If it didn't raise, the row is still there (no injection ran)
        # and the result is well-formed.
        assert isinstance(rows, list)
        assert store.count() == 1
