"""``SQLiteEventLog`` compares ``occurred_at`` by instant, old rows included.

The column is TEXT, and builds before this one wrote whatever offset (or none)
the event carried. The cross-backend half lives in
``contracts/event_log_contract.py``; this file holds what only SQLite has: a
file an earlier build wrote, the migration that adds the indexed column, and
the query plans that still use an index. The expression itself is shared with
the trace store, whose file pins it across offsets.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import _OCCURRED_AT_UTC_SQL, SQLiteEventLog

#: The ``events`` table exactly as the build before this one created it.
_OLD_SCHEMA = """
    CREATE TABLE events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        source TEXT NOT NULL,
        entity_id TEXT,
        entity_type TEXT,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        schema_version TEXT
    );
    CREATE INDEX idx_events_occurred_at ON events(occurred_at);
    CREATE INDEX idx_events_type ON events(event_type);
    CREATE INDEX idx_events_entity ON events(entity_id);
    CREATE INDEX idx_events_source ON events(source);
"""

_INDEX = "idx_events_occurred_at_utc"

#: ``source`` -> the text an earlier build stored, in the order they happened.
_OLD_TEXTS = {
    "ahead": "2026-03-04T20:00:00+14:00",  # 06:00Z; its digits are the largest
    "whole": "2026-03-04T10:00:00+00:00",
    "frac": "2026-03-04T10:00:00.250000+00:00",
    "naive": "2026-03-04T12:00:00",
    "behind": "2026-03-04T06:00:00-11:00",  # 17:00Z; its digits are the smallest
}
_IN_ORDER = list(_OLD_TEXTS)
_NOON = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def _write_as_an_earlier_build(
    db: Path, texts: dict[str, str] | None = None, *, journal: str = "DELETE"
) -> None:
    """Lay down one event per entry of *texts*, the text as given."""
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.executescript(_OLD_SCHEMA)
    for source, text in (texts or _OLD_TEXTS).items():
        conn.execute(
            "INSERT INTO events (event_id, event_type, source, entity_id,"
            " entity_type, occurred_at, recorded_at, payload_json, metadata_json,"
            " schema_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"old-{source}",
                str(EventType.TRACE_INGESTED),
                source,
                None,
                None,
                text,
                text,
                "{}",
                "{}",
                "0.1.0",
            ),
        )
    conn.commit()
    conn.close()


def _rows(db: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT event_id, event_type, source, entity_id, entity_type,"
            " occurred_at, recorded_at, payload_json, metadata_json,"
            " schema_version FROM events ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def old_file(tmp_path: Path) -> Path:
    db = tmp_path / "old.db"
    _write_as_an_earlier_build(db)
    return db


def _sources(events: list[Event]) -> list[str]:
    return [e.source for e in events]


def _check_reads_by_instant(store: SQLiteEventLog) -> None:
    assert _sources(store.get_events()) == _IN_ORDER
    assert _sources(store.get_events(order="desc")) == _IN_ORDER[::-1]
    # Inclusive at the very instant: 10:00:00Z is ``whole``, to the microsecond.
    ten = datetime(2026, 3, 4, 10, 0, tzinfo=UTC)
    assert _sources(store.get_events(until=ten)) == ["ahead", "whole"]
    assert _sources(store.get_events(since=ten)) == ["whole", "frac", "naive", "behind"]
    tick = timedelta(microseconds=1)
    assert _sources(store.get_events(since=ten + tick)) == ["frac", "naive", "behind"]
    # A bound given in another zone means its instant.
    assert _sources(
        store.get_events(since=_NOON.astimezone(timezone(timedelta(hours=-11))))
    ) == [
        "naive",
        "behind",
    ]
    assert store.count(since=_NOON) == 2
    assert store.count(since=_NOON.replace(tzinfo=None)) == 2


def test_rows_written_by_an_earlier_build_compare_by_instant(old_file: Path) -> None:
    store = SQLiteEventLog(old_file)
    try:
        assert store._has_occurred_at_utc
        _check_reads_by_instant(store)
    finally:
        store.close()


def test_opening_an_old_file_rewrites_no_stored_row(old_file: Path) -> None:
    before = _rows(old_file)
    SQLiteEventLog(old_file).close()
    assert _rows(old_file) == before


def test_the_migration_is_idempotent(old_file: Path) -> None:
    SQLiteEventLog(old_file).close()
    with capture_logs() as logs:
        again = SQLiteEventLog(old_file)
    try:
        assert again._has_occurred_at_utc
        assert not [e for e in logs if "migrat" in e["event"]]
        _check_reads_by_instant(again)
    finally:
        again.close()


def test_an_earlier_build_can_still_write_the_migrated_file(old_file: Path) -> None:
    SQLiteEventLog(old_file).close()
    # The previous build's ``INSERT`` names its columns, so the new one is fine.
    _write_old = sqlite3.connect(old_file)
    _write_old.execute(
        "INSERT INTO events (event_id, event_type, source, occurred_at,"
        " recorded_at, schema_version) VALUES ('later', ?, 'later',"
        " '2026-03-04T23:00:00+00:00', ?, '0.1.0')",
        (str(EventType.TRACE_INGESTED), "2026-03-04T23:00:00+00:00"),
    )
    _write_old.commit()
    _write_old.close()
    store = SQLiteEventLog(old_file)
    try:
        assert _sources(store.get_events())[-1] == "later"
        assert _sources(store.get_events(since=_NOON + timedelta(hours=5))) == [
            "behind",
            "later",
        ]
    finally:
        store.close()


def test_a_new_file_gets_the_same_column_and_index(tmp_path: Path) -> None:
    store = SQLiteEventLog(tmp_path / "new.db")
    try:
        assert store._has_occurred_at_utc
        columns = {r["name"] for r in store._conn.execute("PRAGMA table_xinfo(events)")}
        assert "occurred_at_utc" in columns
        indexes = {
            r["name"]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        # The old index stays, because an earlier build expects it.
        assert {"idx_events_occurred_at", _INDEX} <= indexes
    finally:
        store.close()


def test_append_writes_the_text_it_was_given_and_get_event_returns_it(
    tmp_path: Path,
) -> None:
    store = SQLiteEventLog(tmp_path / "new.db")
    try:
        behind = timezone(timedelta(hours=-11))
        aware = Event(
            event_type=EventType.TRACE_INGESTED,
            source="aware",
            occurred_at=_NOON.astimezone(behind),
        )
        naive = Event(
            event_type=EventType.TRACE_INGESTED,
            source="naive",
            occurred_at=_NOON.replace(tzinfo=None),
        )
        store.append(aware)
        store.append(naive)
        stored = dict(
            store._conn.execute("SELECT event_id, occurred_at FROM events").fetchall()
        )
        assert stored[aware.event_id] == aware.occurred_at.isoformat()
        assert stored[naive.event_id] == naive.occurred_at.isoformat()
        got = store.get_event(aware.event_id)
        assert got is not None
        assert got.occurred_at.utcoffset() == timedelta(hours=-11)
        got_naive = store.get_event(naive.event_id)
        assert got_naive is not None
        assert got_naive.occurred_at.tzinfo is None
    finally:
        store.close()


def test_queries_use_the_index_on_the_instant(tmp_path: Path) -> None:
    """The plan of each statement the store sends searches an index on the
    time, as the plain ``occurred_at`` statements did before."""
    store = SQLiteEventLog(tmp_path / "plans.db")
    try:
        for hour in range(3):
            store.emit(EventType.TRACE_INGESTED, "src", entity_id=f"e{hour}")
        sent: list[str] = []
        store._conn.set_trace_callback(sent.append)
        store.get_events(since=_NOON, until=_NOON + timedelta(days=9000), limit=5)
        store.get_events(since=_NOON, order="desc", limit=5)
        store.get_events(limit=5)
        store.count(since=_NOON)
        store._conn.set_trace_callback(None)
        selects = [s for s in sent if s.startswith("SELECT") and "FROM events" in s]
        assert len(selects) == 4
        for sql in selects:
            plan = " | ".join(
                r[3] for r in store._conn.execute("EXPLAIN QUERY PLAN " + sql)
            )
            assert _INDEX in plan, (sql, plan)
            assert "TEMP B-TREE" not in plan, (sql, plan)
    finally:
        store.close()


def test_the_inline_expression_is_the_column(tmp_path: Path) -> None:
    """The unindexed path evaluates ``_OCCURRED_AT_UTC_SQL`` itself, so the two
    must agree on every row."""
    db = tmp_path / "inline.db"
    _write_as_an_earlier_build(db)
    store = SQLiteEventLog(db)
    try:
        rows = store._conn.execute(
            f"SELECT occurred_at_utc, ({_OCCURRED_AT_UTC_SQL}) FROM events"  # noqa: S608
        ).fetchall()
        assert len(rows) == len(_OLD_TEXTS)
        assert all(col == inline for col, inline in rows)
    finally:
        store.close()


def test_an_old_row_holding_now_does_not_stop_the_store_opening(
    tmp_path: Path,
) -> None:
    # ``strftime`` reads ``now`` as the current time, which a generated column
    # refuses by raising; one such old row must not fail every open.
    db = tmp_path / "now.db"
    _write_as_an_earlier_build(db, {**_OLD_TEXTS, "weird": "now"})
    store = SQLiteEventLog(db)
    try:
        assert store._has_occurred_at_utc
        # The row itself is unreadable as an ``Event`` (it never was one), so
        # count rather than load.
        assert store.count() == len(_OLD_TEXTS) + 1
        assert store.count(since=datetime(2026, 3, 4, 17, 0, tzinfo=UTC)) == 2
        store.emit(EventType.TRACE_INGESTED, "after")
    finally:
        store.close()


# ----------------------------------------------------------------------
# Migration is best effort: opening never depends on being able to write
# ----------------------------------------------------------------------

_SKIPPED = {"event_log_schema_migration_skipped", "event_log_index_creation_skipped"}


def _skips(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] in _SKIPPED]


def _has_column(db: Path) -> bool:
    conn = sqlite3.connect(db)
    try:
        return "occurred_at_utc" in {
            r[1] for r in conn.execute("PRAGMA table_xinfo(events)")
        }
    finally:
        conn.close()


def test_an_old_file_under_a_writer_lock_opens_without_waiting_it_out(
    tmp_path: Path,
) -> None:
    db = tmp_path / "locked.db"
    _write_as_an_earlier_build(db, journal="WAL")
    # Another connection mid-transaction, as a busy writer would be.
    holder = sqlite3.connect(db, timeout=0.1, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with capture_logs() as logs:
            store = SQLiteEventLog(db)
        elapsed = time.monotonic() - started
        try:
            # The connection waits ten seconds for a lock; opening must not.
            assert elapsed < 5
            (skipped,) = _skips(logs)
            assert skipped["event"] == "event_log_schema_migration_skipped"
            assert skipped["db_path"] == str(db)
            assert not store._has_occurred_at_utc
            _check_reads_by_instant(store)
            # The short wait was for the attempt only.
            (timeout_ms,) = store._conn.execute("PRAGMA busy_timeout").fetchone()
            assert timeout_ms == 10000
        finally:
            store.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not _has_column(db)

    # The next open, with the lock gone, migrates it.
    again = SQLiteEventLog(db)
    try:
        assert again._has_occurred_at_utc
        _check_reads_by_instant(again)
    finally:
        again.close()
    assert _has_column(db)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs a mode bit that stops this user writing",
)
def test_a_read_only_file_still_opens_and_reads_by_instant(tmp_path: Path) -> None:
    db = tmp_path / "ro.db"
    _write_as_an_earlier_build(db)
    db.chmod(0o444)
    try:
        with capture_logs() as logs:
            store = SQLiteEventLog(db)
        try:
            assert [e["event"] for e in _skips(logs)] == [
                "event_log_schema_migration_skipped"
            ]
            assert not store._has_occurred_at_utc
            _check_reads_by_instant(store)
        finally:
            store.close()
    finally:
        db.chmod(0o644)
