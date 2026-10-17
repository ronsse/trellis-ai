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


def test_legacy_row_entity_id_filter_cannot_match_without_scrub_cooperation(
    tmp_path: Path,
) -> None:
    """Documents a known, out-of-scope-for-this-fix corollary: filtering
    by ``entity_id`` scrubs the filter value (see ``get_events``'s
    docstring) so it agrees with what ``append`` would have stored. A
    legacy row that predates the scrub stored the *raw* value, so neither
    the raw filter nor the scrubbed filter matches it by ``entity_id`` --
    only an unfiltered read (above) or the primary-key lookup sees it.
    This is a pre-existing gap in finding old rows, not a new one this
    fix introduces, and fixing it is retroactive backfill, not scrubbing.
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
                "curate",
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

        assert store.get_events(entity_id="legacy\x00entity") == []
        assert store.get_events(entity_id="legacy�entity") == []
        assert store.get_event("legacy-2") is not None
    finally:
        store.close()


def test_row_to_event_uses_isoformat_timestamps_directly() -> None:
    """Sanity check on the fixture above: the timestamps round-trip
    through ``datetime.fromisoformat`` exactly as ``_row_to_event`` reads
    them, so the two tests above are reading a realistic row shape."""
    now = utc_now()
    assert datetime.fromisoformat(now.isoformat()) == now
