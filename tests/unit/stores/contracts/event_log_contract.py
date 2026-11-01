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
from datetime import UTC, datetime, timedelta, timezone

import pytest

from trellis.core.base import utc_now
from trellis.core.hashing import content_hash
from trellis.core.text_safety import REPLACEMENT_CHAR
from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import Event, EventLog, EventType, scan_events


def _sleep_for_ordering() -> None:
    """Sleep long enough that two appends get distinct ``occurred_at`` timestamps.

    SQLite stores ``occurred_at`` as ISO strings and Postgres as
    ``TIMESTAMPTZ``; both have sub-millisecond resolution but the tests
    avoid relying on that. 5ms is plenty.
    """
    time.sleep(0.005)


def _event_at(source: str, occurred_at: datetime) -> Event:
    return Event(
        event_type=EventType.TRACE_INGESTED,
        source=source,
        occurred_at=occurred_at,
    )


def _future_event_at(source: str, occurred_at: datetime) -> Event:
    """An event as a build newer than this one would have written it.

    The ``event_type`` is a string this build's :class:`EventType` lacks.
    ``Event(...)`` refuses such a type, so the model is constructed without
    validation; ``append`` stores ``str(event_type)``, which is exactly what a
    newer build's own append stores.
    """
    return Event.model_construct(
        event_type="future.kind",
        source=source,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
    )


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
    # get_event — single lookup by id, independent of any list window
    # ------------------------------------------------------------------

    def test_get_event_returns_the_matching_event(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "ingest", entity_id="trace_1")
        target = store.emit(
            EventType.ENTITY_CREATED,
            "curate",
            entity_id="ent_42",
            payload={"name": "auth"},
        )
        store.emit(EventType.TRACE_INGESTED, "ingest", entity_id="trace_2")

        got = store.get_event(target.event_id)
        assert got is not None
        assert got.event_id == target.event_id
        assert got.event_type == EventType.ENTITY_CREATED
        assert got.payload == {"name": "auth"}

    def test_get_event_missing_id_returns_none(self, store: EventLog) -> None:
        store.emit(EventType.TRACE_INGESTED, "ingest")
        assert store.get_event("no-such-event-id") is None

    def test_get_event_on_empty_store_returns_none(self, store: EventLog) -> None:
        assert store.get_event("anything") is None

    def test_get_event_reaches_outside_a_recent_window(self, store: EventLog) -> None:
        """A row buried under many newer ones is still found by id.

        This is the behaviour a bounded list view cannot offer: a window
        capped at ``limit`` can only page back so far, while ``get_event``
        must reach the whole log regardless of how much has been appended
        since.
        """
        old = store.emit(EventType.TRACE_INGESTED, "ingest", entity_id="old")
        for _ in range(5):
            store.emit(EventType.ENTITY_CREATED, "curate")

        got = store.get_event(old.event_id)
        assert got is not None
        assert got.event_id == old.event_id
        # Confirm the window really would have missed it: a 1-row desc
        # window surfaces only the newest event, not ``old``.
        windowed = store.get_events(limit=1, order="desc")
        assert windowed[0].event_id != old.event_id

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
    # Time-range queries compare by instant, whatever offset was written
    # ------------------------------------------------------------------

    def test_since_finds_an_event_written_with_a_negative_offset(
        self, store: EventLog
    ) -> None:
        # One hour ago, written as eleven hours behind UTC: the wall-clock
        # digits read twelve hours ago, which a text comparison against a
        # two-hours-ago bound would call too old.
        instant = utc_now().replace(microsecond=0) - timedelta(hours=1)
        behind = timezone(timedelta(hours=-11))
        event = _event_at("behind", instant.astimezone(behind))
        store.append(event)
        bound = utc_now() - timedelta(hours=2)
        assert [e.event_id for e in store.get_events(since=bound)] == [event.event_id]
        assert store.count(since=bound) == 1

    def test_until_finds_an_event_written_with_a_positive_offset(
        self, store: EventLog
    ) -> None:
        # One hour ago, written as fourteen hours ahead of UTC: the digits
        # read thirteen hours from now, which a text comparison against an
        # ``until`` of now would call too new.
        instant = utc_now().replace(microsecond=0) - timedelta(hours=1)
        ahead = timezone(timedelta(hours=14))
        event = _event_at("ahead", instant.astimezone(ahead))
        store.append(event)
        found = store.get_events(until=utc_now())
        assert [e.event_id for e in found] == [event.event_id]

    def test_a_bound_given_with_an_offset_means_its_instant(
        self, store: EventLog
    ) -> None:
        instant = utc_now().replace(microsecond=0) - timedelta(hours=1)
        store.append(_event_at("utc", instant))
        ahead = timezone(timedelta(hours=14))
        behind = timezone(timedelta(hours=-11))
        # Both bounds name an instant 30 minutes either side of the event.
        found = store.get_events(
            since=(instant - timedelta(minutes=30)).astimezone(behind),
            until=(instant + timedelta(minutes=30)).astimezone(ahead),
        )
        assert [e.source for e in found] == ["utc"]
        # One 30 minutes past it, in the same dress, excludes it.
        late = (instant + timedelta(minutes=30)).astimezone(behind)
        assert store.get_events(since=late) == []
        assert store.count(since=late) == 0
        early = (instant - timedelta(minutes=30)).astimezone(ahead)
        assert store.get_events(until=early) == []

    def test_results_are_ordered_by_instant_across_offsets(
        self, store: EventLog
    ) -> None:
        now = utc_now().replace(microsecond=0)
        # ``earlier`` happened first but its digits are the larger: written
        # fourteen hours ahead of UTC, they read twelve hours from now.
        earlier = (now - timedelta(hours=2)).astimezone(timezone(timedelta(hours=14)))
        later = now - timedelta(hours=1)
        store.append(_event_at("later", later))
        store.append(_event_at("earlier", earlier))
        assert [e.source for e in store.get_events()] == ["earlier", "later"]
        assert [e.source for e in store.get_events(order="desc")] == [
            "later",
            "earlier",
        ]
        # ``limit`` cuts the ordered list, not the digits.
        assert [e.source for e in store.get_events(limit=1)] == ["earlier"]
        assert [e.source for e in store.get_events(order="desc", limit=1)] == ["later"]

    def test_bounds_are_inclusive_to_the_microsecond(self, store: EventLog) -> None:
        instant = datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC)
        written = instant.astimezone(timezone(timedelta(hours=5, minutes=30)))
        store.append(_event_at("tick", written))
        tick = timedelta(microseconds=1)
        assert len(store.get_events(since=instant, until=instant)) == 1
        assert store.count(since=instant) == 1
        assert store.get_events(since=instant + tick) == []
        assert store.count(since=instant + tick) == 0
        assert store.get_events(until=instant - tick) == []

    def test_a_whole_second_event_sits_between_microsecond_bounds(
        self, store: EventLog
    ) -> None:
        # ``isoformat`` drops a zero fraction, so this one is written without
        # one; the bounds are written with six digits.
        instant = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        store.append(_event_at("whole", instant))
        tick = timedelta(microseconds=1)
        found = store.get_events(since=instant - tick, until=instant + tick)
        assert [e.source for e in found] == ["whole"]
        assert store.get_events(since=instant + tick) == []
        assert store.get_events(until=instant - tick) == []

    def test_a_naive_occurred_at_is_read_as_utc(self, store: EventLog) -> None:
        # ``Event`` accepts a naive ``occurred_at`` and Postgres reads one as
        # UTC in its default session, so the store does too: the event is
        # found by the instant its digits name in UTC, naive bound or aware.
        now = utc_now().replace(microsecond=0)
        naive = (now - timedelta(hours=1)).replace(tzinfo=None)
        store.append(_event_at("naive", naive))
        around = {
            "since": now - timedelta(hours=2),
            "until": now - timedelta(minutes=30),
        }
        assert [e.source for e in store.get_events(**around)] == ["naive"]
        naive_around = {k: v.replace(tzinfo=None) for k, v in around.items()}
        assert [e.source for e in store.get_events(**naive_around)] == ["naive"]
        assert store.get_events(since=now - timedelta(minutes=30)) == []
        assert store.get_events(until=now - timedelta(hours=2)) == []
        assert store.count(since=around["since"]) == 1
        # Both bounds are inclusive at the very instant, which a text compare
        # misses: "...T05:06:07" sorts before "...T05:06:07+00:00".
        at = naive.replace(tzinfo=UTC)
        assert [e.source for e in store.get_events(since=at, until=at)] == ["naive"]

    def test_naive_and_aware_events_query_together_in_order(
        self, store: EventLog
    ) -> None:
        now = utc_now().replace(microsecond=0)
        store.append(_event_at("aware", now - timedelta(hours=2)))
        store.append(
            _event_at("old naive", (now - timedelta(hours=3)).replace(tzinfo=None))
        )
        store.append(
            _event_at("new naive", (now - timedelta(hours=1)).replace(tzinfo=None))
        )
        found = store.get_events(since=now - timedelta(hours=4), until=now)
        assert [e.source for e in found] == ["old naive", "aware", "new naive"]

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
    # Filter by payload key
    # ------------------------------------------------------------------
    #
    # ``payload_filters`` had no contract coverage until #461, only
    # backend-local tests — and the SQLite ones are the only ones CI runs on
    # a pull request. The capture-health banner
    # (:mod:`trellis.ops.capture_health`) decides "has this write surface
    # landed anything?" purely by this filter, so a backend that accepted
    # the argument and ignored it would clear the banner on someone else's
    # write and report a dark capture path as healthy. That is a silent
    # false negative in a watchdog, which is worse than the watchdog not
    # existing; it belongs in the contract every backend must pass.

    def test_filter_by_payload_key_returns_only_matching(self, store: EventLog) -> None:
        store.emit(
            EventType.MEMORY_STORED, "mcp", payload={"requested_by": "mcp:save_memory"}
        )
        store.emit(
            EventType.MEMORY_STORED, "cli", payload={"requested_by": "cli:ingest"}
        )
        rows = store.get_events(payload_filters={"requested_by": "mcp:save_memory"})
        assert len(rows) == 1
        assert rows[0].payload["requested_by"] == "mcp:save_memory"

    def test_payload_filter_excludes_events_missing_the_key(
        self, store: EventLog
    ) -> None:
        """An absent key must not match, however the backend stores JSON.

        Two of the three ``MEMORY_STORED`` emitters write no
        ``requested_by`` at all; a backend treating "key missing" as a match
        would let a nightly corpus ingest clear an MCP surface's banner.
        """
        store.emit(EventType.MEMORY_STORED, "worker", payload={"doc_id": "d1"})
        rows = store.get_events(payload_filters={"requested_by": "mcp:save_memory"})
        assert rows == []

    def test_payload_filter_is_anded_with_event_type(self, store: EventLog) -> None:
        """The banner asks both questions at once; neither may be dropped."""
        store.emit(
            EventType.MEMORY_STORED, "mcp", payload={"requested_by": "mcp:save_memory"}
        )
        store.emit(
            EventType.MUTATION_EXECUTED,
            "exec",
            payload={"requested_by": "mcp:save_memory"},
        )
        rows = store.get_events(
            event_type=EventType.MEMORY_STORED,
            payload_filters={"requested_by": "mcp:save_memory"},
        )
        assert len(rows) == 1
        assert rows[0].event_type == EventType.MEMORY_STORED

    def test_payload_filters_are_anded_with_each_other(self, store: EventLog) -> None:
        store.emit(
            EventType.MEMORY_STORED,
            "mcp",
            payload={"requested_by": "mcp:save_memory", "status": "ok"},
        )
        store.emit(
            EventType.MEMORY_STORED,
            "mcp",
            payload={"requested_by": "mcp:save_memory", "status": "degraded"},
        )
        rows = store.get_events(
            payload_filters={"requested_by": "mcp:save_memory", "status": "ok"}
        )
        assert len(rows) == 1
        assert rows[0].payload["status"] == "ok"

    def test_payload_filter_honours_limit_of_one(self, store: EventLog) -> None:
        """``limit=1`` is how the banner asks; existence must not need a scan."""
        for _ in range(3):
            store.emit(
                EventType.MEMORY_STORED,
                "mcp",
                payload={"requested_by": "mcp:save_memory"},
            )
        rows = store.get_events(
            payload_filters={"requested_by": "mcp:save_memory"}, limit=1
        )
        assert len(rows) == 1

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

    # ------------------------------------------------------------------
    # NUL byte / lone surrogate scrub — every backend, one seam
    # ------------------------------------------------------------------
    #
    # A Postgres ``text``/``jsonb`` column refuses a raw NUL byte or an
    # unpaired UTF-16 surrogate outright; SQLite accepts both silently.
    # ``EventLog.append`` scrubs ``payload``, ``metadata``, ``entity_id``,
    # ``entity_type`` and ``source`` before any backend ever sees the
    # event, so every backend under this suite must show the same
    # scrubbed result — that is what makes this a *contract* test rather
    # than a SQLite-only one.

    def test_hazard_in_payload_value_is_scrubbed(self, store: EventLog) -> None:
        emitted = store.emit(
            EventType.ENTITY_CREATED, "curate", payload={"name": "bad\x00name"}
        )
        assert emitted.payload == {"name": f"bad{REPLACEMENT_CHAR}name"}
        rows = store.get_events()
        assert rows[0].payload == {"name": f"bad{REPLACEMENT_CHAR}name"}

    def test_hazard_in_metadata_value_is_scrubbed(self, store: EventLog) -> None:
        emitted = store.emit(
            EventType.ENTITY_CREATED,
            "curate",
            metadata={"note": "bad\ud800note"},
        )
        assert emitted.metadata["note"] == f"bad{REPLACEMENT_CHAR}note"
        rows = store.get_events()
        assert rows[0].metadata["note"] == f"bad{REPLACEMENT_CHAR}note"

    def test_hazard_in_entity_id_and_entity_type_is_scrubbed(
        self, store: EventLog
    ) -> None:
        # NUL only, not a lone surrogate: pydantic-core's own ``str``
        # validator already refuses an unpaired surrogate in a plain
        # ``str`` field (confirmed empirically -- ``Event(entity_type=...)``
        # raises ``ValidationError`` before ``append`` ever runs), so that
        # half of the hazard pair never reaches this seam for entity_id /
        # entity_type / source. It does reach ``payload`` and ``metadata``
        # (``dict[str, Any]`` -- pydantic does not deep-validate ``Any``),
        # which is why the surrogate case is covered there instead.
        emitted = store.emit(
            EventType.ENTITY_CREATED,
            "curate",
            entity_id="ent\x00_1",
            entity_type="doc\x00type",
        )
        assert emitted.entity_id == f"ent{REPLACEMENT_CHAR}_1"
        assert emitted.entity_type == f"doc{REPLACEMENT_CHAR}type"
        rows = store.get_events(entity_id=f"ent{REPLACEMENT_CHAR}_1")
        assert len(rows) == 1
        assert rows[0].entity_type == f"doc{REPLACEMENT_CHAR}type"

    def test_hazard_in_source_is_scrubbed(self, store: EventLog) -> None:
        emitted = store.emit(EventType.ENTITY_CREATED, "bad\x00source")
        assert emitted.source == f"bad{REPLACEMENT_CHAR}source"
        rows = store.get_events(source=f"bad{REPLACEMENT_CHAR}source")
        assert len(rows) == 1

    def test_hazard_in_nested_dict_key_is_scrubbed(self, store: EventLog) -> None:
        """A hazard in a *key*, not just a value, nested two levels deep."""
        emitted = store.emit(
            EventType.ENTITY_CREATED,
            "curate",
            payload={"outer": {"bad\x00key": "value", "ok": 1}},
        )
        assert emitted.payload == {
            "outer": {f"bad{REPLACEMENT_CHAR}key": "value", "ok": 1}
        }
        rows = store.get_events()
        assert rows[0].payload == {
            "outer": {f"bad{REPLACEMENT_CHAR}key": "value", "ok": 1}
        }

    def test_hazard_in_list_element_is_scrubbed(self, store: EventLog) -> None:
        emitted = store.emit(
            EventType.ENTITY_CREATED,
            "curate",
            payload={"items": ["clean", "bad\ud800item"]},
        )
        assert emitted.payload == {"items": ["clean", f"bad{REPLACEMENT_CHAR}item"]}
        rows = store.get_events()
        assert rows[0].payload == {"items": ["clean", f"bad{REPLACEMENT_CHAR}item"]}

    def test_direct_append_also_scrubs(self, store: EventLog) -> None:
        """The scrub runs for a direct ``append(Event(...))`` too, not just
        through the ``emit()`` convenience wrapper."""
        event = Event(
            event_type=EventType.ENTITY_CREATED,
            source="curate",
            entity_id="ent_1",
            payload={"name": "bad\x00name"},
        )
        store.append(event)
        # append() scrubs in place, so the caller's own object reflects it.
        assert event.payload == {"name": f"bad{REPLACEMENT_CHAR}name"}
        rows = store.get_events()
        assert rows[0].payload == {"name": f"bad{REPLACEMENT_CHAR}name"}

    def test_clean_payload_is_stored_byte_identical(self, store: EventLog) -> None:
        """A payload with no hazard round-trips with no observable change
        at all — same keys, same values, same shape."""
        clean_payload = {
            "name": "auth-service",
            "tags": ["billing", "core"],
            "nested": {"tier": 1, "owner": "platform"},
        }
        clean_metadata = {"agent": "claude", "flags": {"dry_run": False}}
        emitted = store.emit(
            EventType.ENTITY_CREATED,
            "curate",
            entity_id="ent_clean",
            entity_type="service",
            payload=dict(clean_payload),
            metadata=dict(clean_metadata),
        )
        assert emitted.payload == clean_payload
        assert emitted.entity_id == "ent_clean"
        assert emitted.entity_type == "service"
        rows = store.get_events(entity_id="ent_clean")
        assert len(rows) == 1
        got = rows[0]
        assert got.payload == clean_payload
        # metadata carries the write-provenance stamp too; assert the
        # caller-supplied keys survive untouched rather than equality on
        # the whole dict.
        assert got.metadata["agent"] == "claude"
        assert got.metadata["flags"] == {"dry_run": False}

    def test_filter_by_the_original_unscrubbed_value_still_matches(
        self, store: EventLog
    ) -> None:
        """The filter-value decision: a caller that only ever saw the raw
        string it originally handed ``emit()`` -- never the scrubbed
        version ``append`` actually stored -- must still find the row.
        ``get_events`` scrubs ``entity_id``/``source`` filter values the
        same way ``append`` scrubs the stored field, so the comparison
        is scrubbed-against-scrubbed either way the caller spells it.
        """
        store.emit(
            EventType.ENTITY_CREATED,
            "bad\x00source",
            entity_id="ent\x00_1",
        )
        # Filtering with the *raw* value the caller originally passed in.
        by_raw_entity_id = store.get_events(entity_id="ent\x00_1")
        assert len(by_raw_entity_id) == 1
        by_raw_source = store.get_events(source="bad\x00source")
        assert len(by_raw_source) == 1
        # Filtering with the already-scrubbed value matches identically.
        by_scrubbed_entity_id = store.get_events(entity_id=f"ent{REPLACEMENT_CHAR}_1")
        assert len(by_scrubbed_entity_id) == 1

    def test_has_idempotency_key_matches_original_unscrubbed_key(
        self, store: EventLog
    ) -> None:
        """Same decision, for the idempotency lookup: ``append`` scrubs
        ``payload["idempotency_key"]`` like any other payload value, and
        ``has_idempotency_key`` scrubs its ``key`` argument before
        comparing, so a caller checking with the raw key it originally
        submitted still gets ``True``.

        In the shipped system this hazard never actually reaches here --
        ``CommandValidator.validate`` (``trellis/mutate/commands.py``)
        refuses a hazardous ``idempotency_key`` at Stage 1, before a
        command is ever executed or its event emitted. This test covers
        the defense-in-depth case: calling the event log directly,
        bypassing ``MutationExecutor`` and its Stage 1 validation.
        """
        store.emit(
            EventType.MUTATION_EXECUTED,
            "mutation_executor",
            payload={"idempotency_key": "key\x00_1"},
        )
        assert store.has_idempotency_key("key\x00_1") is True
        assert store.has_idempotency_key(f"key{REPLACEMENT_CHAR}_1") is True
        assert store.has_idempotency_key("key_1") is False

    # ------------------------------------------------------------------
    # A row whose type this build does not know
    # ------------------------------------------------------------------
    #
    # A newer build can append an ``event_type`` an older one lacks. One such
    # row must not fail a read that happens to reach it, and must not change
    # what a reader sees of the rows it can read. The row itself stays stored.

    def test_unknown_event_type_row_is_skipped_by_an_unfiltered_read(
        self, store: EventLog
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        first = _event_at("a", base)
        newer = _future_event_at("newer", base + timedelta(seconds=1))
        last = _event_at("a", base + timedelta(seconds=2))
        for event in (first, newer, last):
            store.append(event)

        assert [e.event_id for e in store.get_events()] == [
            first.event_id,
            last.event_id,
        ]
        assert [e.event_id for e in store.get_events(order="desc")] == [
            last.event_id,
            first.event_id,
        ]
        assert store.get_events(source="newer") == []
        assert [
            e.event_id for e in store.get_events(event_type=EventType.TRACE_INGESTED)
        ] == [first.event_id, last.event_id]
        got = store.get_event(first.event_id)
        assert got is not None
        assert got.event_id == first.event_id
        # Absent to a reader, not an error; the row is still stored.
        assert store.get_event(newer.event_id) is None
        assert store.count() == 3

    def test_unknown_event_type_rows_do_not_use_up_a_page(
        self, store: EventLog
    ) -> None:
        """``limit`` is still met from the rows that can be read.

        A caller that reads ``len(events) >= limit`` as "there may be more"
        (``scan_events`` does) must not see a page the unknown rows shortened
        as the end of the log.
        """
        base = datetime(2026, 1, 1, tzinfo=UTC)
        known = [_event_at("a", base + timedelta(seconds=i)) for i in range(5)]
        for event in known:
            store.append(event)
        # The three newest rows are all unknown: a newest-first page of three
        # holds nothing this build can read.
        for i in range(5, 8):
            store.append(_future_event_at("newer", base + timedelta(seconds=i)))

        page = store.get_events(limit=3, order="desc")
        assert [e.event_id for e in page] == [e.event_id for e in known[:1:-1]]
        # Unknown rows beyond the page are not in the way of an ascending one.
        page = store.get_events(limit=3)
        assert [e.event_id for e in page] == [e.event_id for e in known[:3]]
        # Fewer readable rows than ``limit``: all of them, no more.
        assert len(store.get_events(limit=50)) == 5

    def test_a_page_read_again_keeps_the_callers_filters(self, store: EventLog) -> None:
        """The second read asks for known types within the caller's filter."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        mine = [_event_at("mine", base + timedelta(seconds=i)) for i in range(3)]
        other = _event_at("other", base + timedelta(seconds=10))
        for event in (*mine, other):
            store.append(event)
        store.append(_future_event_at("mine", base + timedelta(seconds=20)))

        page = store.get_events(source="mine", limit=2, order="desc")
        assert [e.event_id for e in page] == [mine[2].event_id, mine[1].event_id]
        page = store.get_events(
            source="mine",
            since=base + timedelta(seconds=1),
            until=base + timedelta(seconds=30),
            limit=1,
            order="desc",
        )
        assert [e.event_id for e in page] == [mine[2].event_id]

    def test_unknown_event_type_rows_do_not_change_what_a_scan_counts(
        self, store: EventLog
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(5):
            store.append(_event_at("a", base + timedelta(seconds=i)))
        typed_before = scan_events(store, event_type=EventType.TRACE_INGESTED)
        untyped_before = scan_events(store, limit=3)

        for i in range(5, 8):
            store.append(_future_event_at("newer", base + timedelta(seconds=i)))

        typed_after = scan_events(store, event_type=EventType.TRACE_INGESTED)
        assert [e.event_id for e in typed_after.events] == [
            e.event_id for e in typed_before.events
        ]
        assert typed_after.coverage == typed_before.coverage
        untyped_after = scan_events(store, limit=3)
        assert [e.event_id for e in untyped_after.events] == [
            e.event_id for e in untyped_before.events
        ]
        assert untyped_after.coverage.scanned == 3
        assert untyped_after.coverage.truncated is True

    def test_unknown_event_type_row_is_not_an_executed_mutation(
        self, store: EventLog
    ) -> None:
        """``has_idempotency_key`` answers from ``mutation.executed`` rows only."""
        future = _future_event_at("mutation_executor", datetime(2026, 1, 1, tzinfo=UTC))
        future.payload = {"idempotency_key": "key_1"}
        store.append(future)
        assert store.has_idempotency_key("key_1") is False

        store.emit(
            EventType.MUTATION_EXECUTED,
            "mutation_executor",
            payload={"idempotency_key": "key_1"},
        )
        assert store.has_idempotency_key("key_1") is True
        assert store.has_idempotency_key("key_2") is False
