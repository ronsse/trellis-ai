"""``SQLiteTraceStore`` compares ``created_at`` by instant, old rows included.

The column is TEXT, and builds before this one wrote whatever offset (or none)
the trace carried. The cross-backend half lives in
``contracts/trace_store_contract.py``; this file holds what only SQLite has: a
file an earlier build wrote, and the SQL that reads an instant out of its text.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.sqlite.trace import SQLiteTraceStore, _instant_key

#: The ``traces`` table exactly as the build before this one created it.
_OLD_SCHEMA = """
    CREATE TABLE traces (
        trace_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        intent TEXT NOT NULL,
        domain TEXT,
        agent_id TEXT,
        outcome_status TEXT,
        trace_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_traces_created ON traces(created_at);
"""

_ROW = (
    "SELECT trace_id, source, intent, domain, agent_id, outcome_status,"
    " trace_json, created_at FROM traces ORDER BY trace_id"
)


def _trace(intent: str, created_at: datetime) -> Trace:
    return Trace(
        trace_id=f"old-{intent}",
        source=TraceSource.AGENT,
        intent=intent,
        context=TraceContext(agent_id="agent-1", domain="platform"),
        created_at=created_at,
        updated_at=created_at,
    )


def _write_as_an_earlier_build(db: Path, traces: list[Trace]) -> None:
    """Lay down *traces* the way ``append`` did before: the text as given."""
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_SCHEMA)
    for t in traces:
        conn.execute(
            "INSERT INTO traces (trace_id, source, intent, domain, agent_id,"
            " outcome_status, trace_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                t.trace_id,
                t.source.value,
                t.intent,
                "platform",
                "agent-1",
                None,
                t.model_dump_json(),
                t.created_at.isoformat(),
            ),
        )
    conn.commit()
    conn.close()


def _rows(db: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return [tuple(r) for r in conn.execute(_ROW)]
    finally:
        conn.close()


#: One instant, 2026-03-04T05:06:07Z, in every dress an earlier build wrote it.
_NOON = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


@pytest.fixture
def old_file(tmp_path: Path) -> Path:
    db = tmp_path / "traces.db"
    _write_as_an_earlier_build(
        db,
        [
            # In instant order: a (05:06:07.000), b (.250), c (.500), d (05:06:08).
            _trace("a-plus14", _NOON.astimezone(timezone(timedelta(hours=14)))),
            _trace(
                "b-minus11",
                (_NOON + timedelta(milliseconds=250)).astimezone(
                    timezone(timedelta(hours=-11))
                ),
            ),
            _trace("c-utc", _NOON + timedelta(milliseconds=500)),
            _trace("d-naive", (_NOON + timedelta(seconds=1)).replace(tzinfo=None)),
        ],
    )
    return db


def test_rows_written_by_an_earlier_build_compare_by_instant(old_file: Path) -> None:
    store = SQLiteTraceStore(old_file)
    try:
        assert [t.intent for t in store.query()] == [
            "d-naive",
            "c-utc",
            "b-minus11",
            "a-plus14",
        ]
        # The +14:00 row's digits read 19:06:07 and the -11:00 row's 18:06:07
        # the day before, so a text compare finds neither of these.
        window = store.query(
            since=_NOON + timedelta(milliseconds=100),
            until=_NOON + timedelta(milliseconds=600),
        )
        assert [t.intent for t in window] == ["c-utc", "b-minus11"]
        assert [t.intent for t in store.query(until=_NOON)] == ["a-plus14"]
        assert [t.intent for t in store.query(since=_NOON + timedelta(seconds=1))] == [
            "d-naive"
        ]
    finally:
        store.close()


def test_opening_an_old_file_rewrites_no_stored_row(old_file: Path) -> None:
    before = _rows(old_file)
    SQLiteTraceStore(old_file).close()
    assert _rows(old_file) == before


def test_the_migration_is_idempotent(old_file: Path) -> None:
    SQLiteTraceStore(old_file).close()
    store = SQLiteTraceStore(old_file)
    try:
        names = [r["name"] for r in store._conn.execute("PRAGMA table_xinfo(traces)")]
        assert names.count("created_at_utc") == 1
        assert len(store.query()) == 4
    finally:
        store.close()


def test_an_earlier_build_can_still_write_the_migrated_file(old_file: Path) -> None:
    SQLiteTraceStore(old_file).close()
    # What the previous build's ``append`` ran: it names its columns.
    late = _trace("e-late", _NOON + timedelta(hours=1))
    _write_rows = sqlite3.connect(old_file)
    _write_rows.execute(
        "INSERT INTO traces (trace_id, source, intent, domain, agent_id,"
        " outcome_status, trace_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            late.trace_id,
            "agent",
            late.intent,
            "platform",
            "agent-1",
            None,
            late.model_dump_json(),
            late.created_at.isoformat(),
        ),
    )
    _write_rows.commit()
    _write_rows.close()
    store = SQLiteTraceStore(old_file)
    try:
        assert store.query(limit=1)[0].intent == "e-late"
    finally:
        store.close()


def test_a_new_file_gets_the_same_column_and_index(tmp_path: Path) -> None:
    store = SQLiteTraceStore(tmp_path / "fresh.db")
    try:
        names = {r["name"] for r in store._conn.execute("PRAGMA table_xinfo(traces)")}
        indexes = {r["name"] for r in store._conn.execute("PRAGMA index_list(traces)")}
        assert "created_at_utc" in names
        assert "idx_traces_created_at_utc" in indexes
    finally:
        store.close()


def test_append_writes_the_utc_text_and_trace_json_keeps_the_original(
    tmp_path: Path,
) -> None:
    store = SQLiteTraceStore(tmp_path / "t.db")
    try:
        behind = timezone(timedelta(hours=-11))
        offset = _trace("offset", _NOON.astimezone(behind))
        naive = _trace("naive", _NOON.replace(tzinfo=None))
        store.append(offset)
        store.append(naive)
        stored = dict(
            store._conn.execute("SELECT trace_id, created_at FROM traces").fetchall()
        )
        assert stored[offset.trace_id] == "2026-03-04T05:06:07+00:00"
        assert stored[naive.trace_id] == "2026-03-04T05:06:07+00:00"
        # The trace itself is not rewritten: it reads back as it was given.
        got = store.get(offset.trace_id)
        assert got is not None
        assert got.created_at.utcoffset() == timedelta(hours=-11)
        got_naive = store.get(naive.trace_id)
        assert got_naive is not None
        assert got_naive.created_at.tzinfo is None
    finally:
        store.close()


_OFFSETS = [
    UTC,
    timezone(timedelta(hours=-12)),
    timezone(timedelta(hours=-11)),
    timezone(timedelta(hours=-9, minutes=-30)),
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=12, minutes=45)),
    timezone(timedelta(hours=14)),
    None,
]
_MICROS = [0, 1, 123456, 999499, 999500, 999999]


@pytest.mark.parametrize("zone", _OFFSETS, ids=str)
def test_the_sql_key_equals_the_python_key(tmp_path: Path, zone: timezone | None):
    """One reading of an instant in two languages, across the carry cases.

    ``999500`` and ``999999`` microseconds are the ones a ``strftime`` that
    rounds to the millisecond would push into the next second.
    """
    store = SQLiteTraceStore(tmp_path / "keys.db")
    try:
        for micro in _MICROS:
            # Two stamps a whole day apart so a day-boundary carry shows too.
            for base in (
                datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
                datetime(2026, 3, 4, 0, 0, 0, tzinfo=UTC),
            ):
                moment = base.replace(microsecond=micro, tzinfo=zone)
                store._conn.execute(
                    "INSERT INTO traces (trace_id, source, intent, trace_json,"
                    " created_at) VALUES (?, 'agent', 'k', '{}', ?)",
                    (f"k-{micro}-{base.day}", moment.isoformat()),
                )
                got = store._conn.execute(
                    "SELECT created_at_utc FROM traces WHERE trace_id = ?",
                    (f"k-{micro}-{base.day}",),
                ).fetchone()[0]
                assert got == _instant_key(moment), moment.isoformat()
    finally:
        store.close()


def test_text_sqlite_cannot_read_stays_visible(tmp_path: Path) -> None:
    """An offset beyond +/-14:00 is not a zone ``strftime`` accepts. Only a
    Python caller can have written one, and ``append`` no longer does, but a
    row an earlier build wrote must not fall out of every bounded query on a
    NULL: it keeps the order its digits give it."""
    db = tmp_path / "odd.db"
    odd = _trace(
        "odd",
        datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=23, minutes=59))),
    )
    _write_as_an_earlier_build(db, [odd])
    store = SQLiteTraceStore(db)
    try:
        assert [t.intent for t in store.query()] == ["odd"]
        around = store.query(
            since=datetime(2026, 3, 4, 5, 0, tzinfo=UTC),
            until=datetime(2026, 3, 4, 6, 0, tzinfo=UTC),
        )
        assert [t.intent for t in around] == ["odd"]
    finally:
        store.close()
