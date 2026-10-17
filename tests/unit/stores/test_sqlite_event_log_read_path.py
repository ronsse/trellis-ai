"""The read path never scrubs -- ``EventLog.append``'s scrub is write-only.

A row written before this scrub existed, or by any writer that reaches the
``events`` table without going through ``Event``/``append`` (a migration,
a backfill script, a hand-run ``INSERT``), carries whatever bytes it was
given. ``get_event``/``get_events`` reconstruct an ``Event`` straight from
the stored row -- see ``SQLiteEventLog._row_to_event`` -- and must hand
that back byte-for-byte, not a newly-scrubbed copy of it. Scrubbing a read
would silently rewrite history for a store whose defining rule is
"traces are immutable" (CLAUDE.md), and would make this suite unable to
tell "the write path scrubs" from "the read path launders".
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from trellis.core.base import utc_now
from trellis.feedback.recording import _feedback_id_in_event_log
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


def test_legacy_hazardous_row_reads_back_unscrubbed(tmp_path: Path) -> None:
    store = SQLiteEventLog(tmp_path / "events.db")
    try:
        # Insert directly via raw SQL, bypassing Event construction and
        # EventLog.append's scrub entirely -- how a row written before
        # this fix shipped, or by any other direct writer, would look.
        #
        # NUL only for entity_id/entity_type/source: those are plain
        # ``str`` fields on ``Event``, and pydantic-core's own string
        # validator refuses to construct a ``str`` field from a lone
        # surrogate (confirmed empirically -- it raises before any
        # scrub code runs). ``_row_to_event`` below reconstructs an
        # ``Event`` the same way, so a row with a lone surrogate in one
        # of those three columns couldn't be read back by any path --
        # it isn't a legacy case this store could ever have produced.
        # ``payload``/``metadata`` are JSON columns backing
        # ``dict[str, Any]`` fields, which pydantic does not deep-
        # validate, so a lone surrogate there is exercised below.
        now = utc_now().isoformat()
        store._conn.execute(
            "INSERT INTO events "
            "(event_id, event_type, source, entity_id, entity_type, "
            "occurred_at, recorded_at, payload_json, metadata_json, "
            "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-1",
                str(EventType.ENTITY_CREATED),
                "legacy\x00source",
                "legacy\x00entity",
                "legacy\x00type",
                now,
                now,
                json.dumps({"name": "legacy\ud800name"}),
                json.dumps({"note": "legacy\x00note"}),
                "1.0",
            ),
        )
        store._conn.commit()

        by_id = store.get_event("legacy-1")
        assert by_id is not None
        assert by_id.source == "legacy\x00source"
        assert by_id.entity_id == "legacy\x00entity"
        assert by_id.entity_type == "legacy\x00type"
        assert by_id.payload == {"name": "legacy\ud800name"}
        assert by_id.metadata == {"note": "legacy\x00note"}

        # get_events() with no filters touches the same reconstruction
        # path for a listing rather than a point lookup.
        rows = store.get_events(event_type=EventType.ENTITY_CREATED)
        assert len(rows) == 1
        assert rows[0].event_id == "legacy-1"
        assert rows[0].payload == {"name": "legacy\ud800name"}
        assert rows[0].entity_id == "legacy\x00entity"

        # Confirm the raw bytes really are what's on disk, independent
        # of Event reconstruction, so this test cannot pass by accident
        # of some other normalization.
        cur = store._conn.execute(
            "SELECT entity_id, payload_json FROM events WHERE event_id = ?",
            ("legacy-1",),
        )
        raw_entity_id, raw_payload_json = cur.fetchone()
        assert raw_entity_id == "legacy\x00entity"
        assert json.loads(raw_payload_json) == {"name": "legacy\ud800name"}
    finally:
        store.close()


def test_legacy_row_is_found_by_the_raw_filter(tmp_path: Path) -> None:
    """A legacy row -- written before ``append`` scrubbed on write, or by
    any direct writer that bypasses it -- stores the *raw* NUL byte in
    ``entity_id``, ``source`` and a payload value. Filtering by that same
    raw string (what any real caller has, since it's the value it
    originally wrote) must still find the row: ``get_events`` matches
    ``col IN (raw, scrubbed)`` whenever scrubbing the filter value would
    change it, not a scrubbed-only equality that only a post-fix write
    could satisfy.
    """
    store = SQLiteEventLog(tmp_path / "events.db")
    try:
        now = utc_now().isoformat()
        store._conn.execute(
            "INSERT INTO events "
            "(event_id, event_type, source, entity_id, entity_type, "
            "occurred_at, recorded_at, payload_json, metadata_json, "
            "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-2",
                str(EventType.ENTITY_CREATED),
                "legacy\x00source",
                "legacy\x00entity",
                None,
                now,
                now,
                json.dumps({"session_id": "legacy\x00session"}),
                "{}",
                "1.0",
            ),
        )
        store._conn.commit()

        assert [e.event_id for e in store.get_events(entity_id="legacy\x00entity")] == [
            "legacy-2"
        ]
        assert [e.event_id for e in store.get_events(source="legacy\x00source")] == [
            "legacy-2"
        ]
        assert [
            e.event_id
            for e in store.get_events(
                payload_filters={"session_id": "legacy\x00session"}
            )
        ] == ["legacy-2"]
        assert store.get_event("legacy-2") is not None
    finally:
        store.close()


def test_legacy_row_not_found_by_a_pure_scrubbed_filter_but_a_fresh_write_is(
    tmp_path: Path,
) -> None:
    """The boundary of the scrub-aware match: a legacy row stores *raw*
    bytes, so a filter value with no hazard character in it (the
    already-replaced form) cannot find it --
    scrubbing is one-directional and U+FFFD cannot be un-replaced back
    into the NUL byte actually on disk. That is not a gap in this fix;
    no real caller searches with a string it never wrote.

    A row written through the real, scrub-aware ``append()`` path (the
    normal case after this fix) stores the *scrubbed* text, so it is
    reachable both by that scrubbed text directly and by the original
    raw value the caller still has -- the same ``IN (raw, scrubbed)``
    match handles both directions for that row.
    """
    store = SQLiteEventLog(tmp_path / "events.db")
    try:
        now = utc_now().isoformat()
        store._conn.execute(
            "INSERT INTO events "
            "(event_id, event_type, source, entity_id, entity_type, "
            "occurred_at, recorded_at, payload_json, metadata_json, "
            "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-3",
                str(EventType.ENTITY_CREATED),
                "legacy",
                "legacy\x00entity",
                None,
                now,
                now,
                "{}",
                "{}",
                "1.0",
            ),
        )
        store._conn.commit()
        assert store.get_events(entity_id="legacy�entity") == []

        fresh = store.emit(
            EventType.ENTITY_CREATED,
            source="fresh",
            entity_id="fresh\x00entity",
        )
        assert [e.event_id for e in store.get_events(entity_id="fresh�entity")] == [
            fresh.event_id
        ]
        assert [e.event_id for e in store.get_events(entity_id="fresh\x00entity")] == [
            fresh.event_id
        ]
    finally:
        store.close()


def test_emit_payload_nul_found_by_the_raw_payload_filter(tmp_path: Path) -> None:
    """Mutant guard: a row written through ``emit`` with a NUL in a
    payload value is stored scrubbed (U+FFFD). Filtering by the
    *original raw* value must still find it -- this fails the moment the
    SQLite ``payload_filters`` match stops scrubbing the filter value,
    since the raw filter would then be compared against the stored
    scrubbed text with no scrub-aware fallback.
    """
    store = SQLiteEventLog(tmp_path / "events.db")
    try:
        event = store.emit(
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            payload={"session_id": "sess\x00ion"},
        )

        found = store.get_events(payload_filters={"session_id": "sess\x00ion"})
        assert [e.event_id for e in found] == [event.event_id]
    finally:
        store.close()


def test_lone_surrogate_filter_matches_scrubbed_row_without_raising(
    tmp_path: Path,
) -> None:
    """``sqlite3`` cannot bind a lone surrogate, so the raw arm of the
    scrub-aware match must be left out for one: the filter finds the
    scrubbed row instead of raising ``UnicodeEncodeError``, for every
    filtered column and the idempotency key.
    """
    store = SQLiteEventLog(tmp_path / "events.db")
    try:
        event = store.emit(
            EventType.MUTATION_EXECUTED,
            # ``Event`` refuses a lone surrogate in its own str fields, so
            # those carry the stored (scrubbed) form; payload values reach
            # ``append`` raw and are scrubbed there.
            source="src\ufffd",
            entity_id="ent\ufffd",
            payload={"idempotency_key": "key\ud800", "pack_id": "pk\udc00"},
        )
        assert [e.event_id for e in store.get_events(entity_id="ent\udfff")] == [
            event.event_id
        ]
        assert [e.event_id for e in store.get_events(source="src\ud800")] == [
            event.event_id
        ]
        found = store.get_events(payload_filters={"pack_id": "pk\udc00"})
        assert [e.event_id for e in found] == [event.event_id]
        assert store.has_idempotency_key("key\ud800")
        assert store.get_events(entity_id="ent\x00\ud800") == []
    finally:
        store.close()


def test_feedback_id_in_event_log_finds_legacy_raw_row(tmp_path: Path) -> None:
    """Real caller: ``_feedback_id_in_event_log`` pushes ``feedback_id``
    into ``payload_filters``. A legacy FEEDBACK_RECORDED row holding a
    raw-NUL ``feedback_id`` (how any row written before this fix, or by
    a direct writer, would look) must still answer True, or
    ``reconcile-feedback`` would re-emit a duplicate.
    """
    store = SQLiteEventLog(tmp_path / "events.db")
    try:
        now = utc_now().isoformat()
        raw_feedback_id = "fb\x00id"
        store._conn.execute(
            "INSERT INTO events "
            "(event_id, event_type, source, entity_id, entity_type, "
            "occurred_at, recorded_at, payload_json, metadata_json, "
            "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-feedback-1",
                str(EventType.FEEDBACK_RECORDED),
                "legacy",
                None,
                None,
                now,
                now,
                json.dumps({"feedback_id": raw_feedback_id}),
                "{}",
                "1.0",
            ),
        )
        store._conn.commit()

        assert _feedback_id_in_event_log(store, raw_feedback_id) is True
    finally:
        store.close()


def test_has_idempotency_key_finds_legacy_raw_row(tmp_path: Path) -> None:
    """Defensive today -- Stage 1 validation refuses a hazardous key before
    the lookup runs -- but the raw arm must hold for a legacy row that
    stored the key with a raw NUL."""
    store = SQLiteEventLog(tmp_path / "events.db")
    try:
        now = utc_now().isoformat()
        store._conn.execute(
            "INSERT INTO events "
            "(event_id, event_type, source, entity_id, entity_type, "
            "occurred_at, recorded_at, payload_json, metadata_json, "
            "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-idem",
                str(EventType.MUTATION_EXECUTED),
                "executor",
                None,
                None,
                now,
                now,
                json.dumps({"idempotency_key": "key\x00one"}),
                "{}",
                "1.0",
            ),
        )
        store._conn.commit()
        assert store.has_idempotency_key("key\x00one") is True
        assert store.has_idempotency_key("key") is False
    finally:
        store.close()


def test_row_to_event_uses_isoformat_timestamps_directly() -> None:
    """Sanity check on the fixture above: the timestamps round-trip
    through ``datetime.fromisoformat`` exactly as ``_row_to_event`` reads
    them, so the raw-SQL fixtures above read a realistic row shape."""
    now = utc_now()
    assert datetime.fromisoformat(now.isoformat()) == now
