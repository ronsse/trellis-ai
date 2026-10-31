"""``SQLiteTraceStore`` compares ``created_at`` by instant, old rows included.

The column is TEXT, and builds before this one wrote whatever offset (or none)
the trace carried. The cross-backend half lives in
``contracts/trace_store_contract.py``; this file holds what only SQLite has: a
file an earlier build wrote, the SQL that reads an instant out of its text, the
migration that adds the column, and ``as_utc``, which every store shares.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.base.trace import as_utc
from trellis.stores.sqlite.trace import (
    _CREATED_AT_UTC_SQL,
    SQLiteTraceStore,
    _instant_key,
)

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
    CREATE INDEX idx_traces_source ON traces(source);
    CREATE INDEX idx_traces_domain ON traces(domain);
    CREATE INDEX idx_traces_agent ON traces(agent_id);
    CREATE INDEX idx_traces_created ON traces(created_at);
    CREATE INDEX idx_traces_outcome ON traces(outcome_status);
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


def _write_as_an_earlier_build(
    db: Path,
    traces: list[Trace],
    *,
    journal: str = "DELETE",
    texts: dict[str, str] | None = None,
) -> None:
    """Lay down *traces* the way ``append`` did before: the text as given.

    *texts* names a ``created_at`` text to store in place of a trace's own, for
    text that ``isoformat()`` would never write.
    """
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode={journal}")
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
                (texts or {}).get(t.trace_id, t.created_at.isoformat()),
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


def _old_traces() -> list[Trace]:
    return [
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
    ]


@pytest.fixture
def old_file(tmp_path: Path) -> Path:
    db = tmp_path / "traces.db"
    _write_as_an_earlier_build(db, _old_traces())
    return db


def _check_reads_by_instant(store: SQLiteTraceStore) -> None:
    """What ``old_file`` holds, found and ordered by instant, bounded or not."""
    assert [t.intent for t in store.query()] == [
        "d-naive",
        "c-utc",
        "b-minus11",
        "a-plus14",
    ]
    # The +14:00 row's digits read 19:06:07 and the -11:00 row's 18:06:07 the
    # day before, so a text compare finds neither of these.
    window = store.query(
        since=_NOON + timedelta(milliseconds=100),
        until=_NOON + timedelta(milliseconds=600),
    )
    assert [t.intent for t in window] == ["c-utc", "b-minus11"]
    assert [t.intent for t in store.query(until=_NOON)] == ["a-plus14"]
    assert [t.intent for t in store.query(since=_NOON + timedelta(seconds=1))] == [
        "d-naive"
    ]


def test_rows_written_by_an_earlier_build_compare_by_instant(old_file: Path) -> None:
    store = SQLiteTraceStore(old_file)
    try:
        _check_reads_by_instant(store)
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
    # Past what ``strftime`` accepts in the text, down to what Python can write.
    timezone(timedelta(hours=15)),
    timezone(timedelta(hours=-14, minutes=-59)),
    timezone(timedelta(hours=23, minutes=59)),
    timezone(timedelta(hours=-23, minutes=-59, seconds=-59)),
    timezone(timedelta(hours=5, minutes=30, seconds=15)),
    timezone(timedelta(seconds=-1)),
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


def _key(store: SQLiteTraceStore, created_at: str) -> str:
    store._conn.execute(
        "INSERT INTO traces (trace_id, source, intent, trace_json, created_at)"
        " VALUES (?, 'agent', 'k', '{}', ?)",
        (f"key-{created_at!r}", created_at),
    )
    return store._conn.execute(
        "SELECT created_at_utc FROM traces WHERE trace_id = ?",
        (f"key-{created_at!r}",),
    ).fetchone()[0]


def test_an_offset_past_fourteen_hours_is_found_by_instant(tmp_path: Path) -> None:
    """``strftime`` rejects an offset past +/-14:59 in the text, but Python can
    write one, and an earlier build did."""
    db = tmp_path / "far.db"
    _write_as_an_earlier_build(
        db,
        [
            # 05:00Z, but its digits read 20:00 that evening.
            _trace(
                "far-ahead",
                datetime(2026, 3, 4, 20, 0, tzinfo=timezone(timedelta(hours=15))),
            ),
            # 23:59Z, but its digits read 00:00 the same morning.
            _trace(
                "far-behind",
                datetime(
                    2026,
                    3,
                    4,
                    0,
                    0,
                    tzinfo=timezone(timedelta(hours=-23, minutes=-59)),
                ),
            ),
        ],
    )
    store = SQLiteTraceStore(db)
    try:
        assert [t.intent for t in store.query()] == ["far-behind", "far-ahead"]
        morning = store.query(
            since=datetime(2026, 3, 4, 4, tzinfo=UTC),
            until=datetime(2026, 3, 4, 6, tzinfo=UTC),
        )
        assert [t.intent for t in morning] == ["far-ahead"]
        night = store.query(since=datetime(2026, 3, 4, 23, tzinfo=UTC))
        assert [t.intent for t in night] == ["far-behind"]
    finally:
        store.close()


def test_an_offset_fraction_moves_the_key_by_less_than_a_second(tmp_path: Path):
    """The one place the key is not Python's: ``+05:30:15.5``, which only a
    hand-written ``tzinfo`` produces. Its fraction is dropped, and this pins how
    far that can move the key so a change to it is noticed."""
    store = SQLiteTraceStore(tmp_path / "frac.db")
    try:
        for sign in (1, -1):
            zone = timezone(
                sign * timedelta(hours=5, minutes=30, seconds=15, microseconds=500000)
            )
            moment = datetime(2026, 3, 4, 5, 6, 7, 250000, tzinfo=zone)
            got = datetime.fromisoformat(_key(store, moment.isoformat()) + "+00:00")
            want = datetime.fromisoformat(_instant_key(moment) + "+00:00")
            assert timedelta(0) < abs(got - want) < timedelta(seconds=1)
    finally:
        store.close()


def test_text_python_cannot_write_stays_visible(tmp_path: Path) -> None:
    """Such a row keeps the order its digits give it, rather than dropping out
    of every bounded query on a NULL. Python writes a six-digit fraction, so a
    three-digit one beside an offset is text only another writer produces."""
    db = tmp_path / "odd.db"
    odd = _trace("odd", datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC))
    _write_as_an_earlier_build(
        db, [odd], texts={odd.trace_id: "2026-03-04T05:06:07.123+00:00"}
    )
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


#: Texts ``strftime`` would read as the current time, which a generated column
#: and its index refuse to evaluate. The NUL ends the string for ``strftime``,
#: so the last one is ``now`` to it and a different string to ``lower()``.
_NOW_TEXTS = ["now", "NOW", "now\x00x"]


@pytest.mark.parametrize("text", _NOW_TEXTS, ids=repr)
def test_an_old_row_holding_now_does_not_stop_the_store_opening(
    tmp_path: Path, text: str
) -> None:
    db = tmp_path / "now.db"
    now_row = _trace("now-row", _NOON)
    _write_as_an_earlier_build(
        db, [*_old_traces(), now_row], texts={now_row.trace_id: text}
    )
    store = SQLiteTraceStore(db)
    try:
        assert store._has_created_at_utc
        assert len(store.query()) == 5
        # Bounded, the four real rows are found by instant and the odd one is
        # not given an instant it does not have.
        bounded = store.query(since=_NOON, until=_NOON + timedelta(seconds=1))
        assert [t.intent for t in bounded] == [
            "d-naive",
            "c-utc",
            "b-minus11",
            "a-plus14",
        ]
    finally:
        store.close()


@pytest.mark.parametrize("text", _NOW_TEXTS, ids=repr)
def test_inserting_now_into_a_migrated_store_does_not_fail(
    tmp_path: Path, text: str
) -> None:
    store = SQLiteTraceStore(tmp_path / "ins.db")
    try:
        # The index on the column evaluates it at insert time.
        assert _key(store, text) == text
        (count,) = store._conn.execute("SELECT count(*) FROM traces").fetchone()
        assert count == 1
    finally:
        store.close()


# ----------------------------------------------------------------------
# Opening a store must not depend on being able to migrate it
# ----------------------------------------------------------------------

_SKIPPED = {"trace_schema_migration_skipped", "trace_index_creation_skipped"}


def _skips(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] in _SKIPPED]


def _hold_the_write_lock(db: Path) -> sqlite3.Connection:
    """Another connection mid-transaction, as a nightly job would be."""
    holder = sqlite3.connect(db, timeout=0.1, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def _release(holder: sqlite3.Connection) -> None:
    holder.execute("ROLLBACK")
    holder.close()


def _has_column(db: Path) -> bool:
    conn = sqlite3.connect(db)
    try:
        return "created_at_utc" in {
            r[1] for r in conn.execute("PRAGMA table_xinfo(traces)")
        }
    finally:
        conn.close()


def _has_index(db: Path) -> bool:
    conn = sqlite3.connect(db)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'idx_traces_created_at_utc'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_an_old_file_under_a_writer_lock_opens_without_waiting_it_out(
    tmp_path: Path,
) -> None:
    db = tmp_path / "locked.db"
    _write_as_an_earlier_build(db, _old_traces(), journal="WAL")
    holder = _hold_the_write_lock(db)
    try:
        started = time.monotonic()
        with capture_logs() as logs:
            store = SQLiteTraceStore(db)
        elapsed = time.monotonic() - started
        try:
            # The connection waits ten seconds for a lock; opening must not.
            assert elapsed < 5
            (skipped,) = _skips(logs)
            assert skipped["event"] == "trace_schema_migration_skipped"
            assert skipped["db_path"] == str(db)
            assert not store._has_created_at_utc
            _check_reads_by_instant(store)
            # The short wait was for the attempt only.
            (timeout_ms,) = store._conn.execute("PRAGMA busy_timeout").fetchone()
            assert timeout_ms == 10000
        finally:
            store.close()
    finally:
        _release(holder)
    assert not _has_column(db)

    # The next open, with the lock gone, migrates it.
    again = SQLiteTraceStore(db)
    try:
        assert again._has_created_at_utc
        _check_reads_by_instant(again)
    finally:
        again.close()
    assert _has_column(db)
    assert _has_index(db)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs a mode bit that stops this user writing",
)
@pytest.mark.parametrize("migrated", [False, True], ids=["old", "migrated"])
@pytest.mark.parametrize(
    ("journal", "dir_read_only"),
    [("DELETE", False), ("DELETE", True), ("WAL", False)],
    ids=["delete", "delete-in-read-only-dir", "wal"],
)
def test_a_read_only_file_still_opens_and_reads_by_instant(
    tmp_path: Path, journal: str, dir_read_only: bool, migrated: bool
) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    db = folder / "traces.db"
    _write_as_an_earlier_build(db, _old_traces(), journal=journal)
    if migrated:
        SQLiteTraceStore(db).close()
        # Opening a store puts the file in WAL; put it back as it was given.
        raw = sqlite3.connect(db)
        raw.execute(f"PRAGMA journal_mode = {journal}")
        raw.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    db.chmod(0o444)
    if dir_read_only:
        folder.chmod(0o555)
    try:
        with capture_logs() as logs:
            store = SQLiteTraceStore(db)
        try:
            _check_reads_by_instant(store)
            # An already-migrated file needs no write, so none is attempted.
            assert bool(_skips(logs)) is (not migrated)
            assert store._has_created_at_utc is migrated
        finally:
            store.close()
    finally:
        folder.chmod(0o755)
        db.chmod(0o644)


def test_an_already_migrated_file_opens_under_a_writer_lock_without_a_warning(
    old_file: Path,
) -> None:
    SQLiteTraceStore(old_file).close()
    holder = _hold_the_write_lock(old_file)
    try:
        with capture_logs() as logs:
            store = SQLiteTraceStore(old_file)
        try:
            # A write attempt behind that lock would have failed and said so.
            assert _skips(logs) == []
            assert store._has_created_at_utc
            _check_reads_by_instant(store)
        finally:
            store.close()
    finally:
        _release(holder)


def test_a_missing_index_alone_is_retried_on_the_next_open(old_file: Path) -> None:
    SQLiteTraceStore(old_file).close()
    raw = sqlite3.connect(old_file)
    raw.execute("DROP INDEX idx_traces_created_at_utc")
    raw.commit()
    raw.close()

    holder = _hold_the_write_lock(old_file)
    try:
        with capture_logs() as logs:
            store = SQLiteTraceStore(old_file)
        try:
            (skipped,) = _skips(logs)
            assert skipped["event"] == "trace_index_creation_skipped"
            # The column is there, so it is the column that answers: unindexed.
            assert store._has_created_at_utc
            _check_reads_by_instant(store)
        finally:
            store.close()
    finally:
        _release(holder)
    assert not _has_index(old_file)

    SQLiteTraceStore(old_file).close()
    assert _has_index(old_file)


def test_losing_the_race_to_add_the_column_is_not_an_error(
    old_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another opener added the column between this one reading the columns and
    running its own ``ALTER``, which therefore fails with a duplicate column."""
    SQLiteTraceStore(old_file).close()
    real = SQLiteTraceStore._trace_columns
    reads: list[None] = []

    def stale_once(self: SQLiteTraceStore) -> set[str]:
        reads.append(None)
        return set() if len(reads) == 1 else real(self)

    monkeypatch.setattr(SQLiteTraceStore, "_trace_columns", stale_once)
    store = SQLiteTraceStore(old_file)
    try:
        assert len(reads) == 2
        assert store._has_created_at_utc
        _check_reads_by_instant(store)
    finally:
        store.close()


def test_concurrent_opens_of_an_old_file_all_work(tmp_path: Path) -> None:
    errors: list[BaseException] = []
    rows = [_trace(f"r{n}", _NOON + timedelta(seconds=n)) for n in range(300)]
    for round_ in range(8):
        db = tmp_path / f"race{round_}.db"
        _write_as_an_earlier_build(db, rows, journal="WAL")
        barrier = threading.Barrier(12)

        def open_it(db: Path = db, barrier: threading.Barrier = barrier) -> None:
            try:
                barrier.wait()
                store = SQLiteTraceStore(db)
                try:
                    assert len(store.query(limit=5)) == 5
                finally:
                    store.close()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_it) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert _has_column(db)
    assert errors == []


def test_the_inline_expression_is_the_column(tmp_path: Path) -> None:
    """The unindexed path evaluates ``_CREATED_AT_UTC_SQL`` itself, so the two
    must give one answer for every shape the column test covers."""
    store = SQLiteTraceStore(tmp_path / "same.db")
    try:
        for n, text in enumerate(
            [
                "2026-03-04T05:06:07+14:00",
                "2026-03-04T05:06:07.999999-11:00",
                "2026-03-04T05:06:07",
                "2026-03-04T05:06:07+23:59:59",
                "now",
                "2026-03-04T05:06:07.123+00:00",
            ]
        ):
            _key(store, text)
            (column, inline) = store._conn.execute(
                f"SELECT created_at_utc, ({_CREATED_AT_UTC_SQL}) FROM traces"  # noqa: S608
                " WHERE created_at = ?",
                (text,),
            ).fetchone()
            assert column == inline, (n, text)
    finally:
        store.close()


# ----------------------------------------------------------------------
# as_utc, which every store and the embed worker share
# ----------------------------------------------------------------------

_MIN = datetime.min.replace(tzinfo=UTC)
_MAX = datetime.max.replace(tzinfo=UTC)


def test_as_utc_clamps_an_instant_datetime_cannot_name() -> None:
    assert (
        as_utc(datetime.min.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))))
        == _MIN
    )
    assert as_utc(datetime.min.replace(tzinfo=timezone(timedelta(hours=14)))) == _MIN
    assert as_utc(datetime.max.replace(tzinfo=timezone(timedelta(hours=-11)))) == _MAX
    # The edges themselves, and the far side of them, are not clamped.
    assert as_utc(_MIN) == _MIN
    assert as_utc(_MAX) == _MAX
    inside = datetime.max.replace(tzinfo=timezone(timedelta(hours=5)))
    assert as_utc(inside) == inside


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="no tzset on this platform")
def test_a_naive_stamp_is_utc_whatever_the_process_time_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a UTC host reading a naive stamp as local time gives the same answer,
    so pin a zone 13 hours ahead (POSIX form, so no tzdata is needed)."""
    monkeypatch.setenv("TZ", "WSST-13")
    time.tzset()
    try:
        assert datetime(2026, 1, 1).astimezone().utcoffset() == timedelta(hours=13)
        naive = datetime(2026, 3, 4, 5, 6, 7)  # noqa: DTZ001
        assert as_utc(naive) == datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        assert _instant_key(naive) == "2026-03-04T05:06:07.000000"
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize(
    "moment",
    [
        datetime.min.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(timedelta(hours=-11))),
    ],
    ids=["min+05:30", "min+14:00", "max-11:00"],
)
def test_a_stamp_past_the_representable_edge_is_stored_and_found(
    tmp_path: Path, moment: datetime
) -> None:
    store = SQLiteTraceStore(tmp_path / "edge.db")
    try:
        trace = _trace("edge", moment)
        store.append(trace)
        found = store.query(since=moment)
        assert [t.trace_id for t in found] == [trace.trace_id]
        # What was stored reads back as it was given, and the key is Python's.
        got = store.get(trace.trace_id)
        assert got is not None
        assert got.created_at == moment
        stored = store._conn.execute(
            "SELECT created_at, created_at_utc FROM traces"
        ).fetchone()
        assert stored["created_at_utc"] == _instant_key(moment)
    finally:
        store.close()
