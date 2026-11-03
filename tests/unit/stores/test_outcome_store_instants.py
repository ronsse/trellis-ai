"""``SQLiteOutcomeStore`` compares ``occurred_at`` by instant, old rows included.

The column is TEXT, and ``OutcomeEvent.occurred_at`` accepts a naive datetime or
any offset, which ``append`` writes as given, so a text comparison or ``ORDER
BY`` sorted by wall-clock digits. This file holds the whole store: the reads
through the public API, a file an earlier build wrote, the migration that adds
the indexed column, and the query plans that use an index. The expression itself
is shared with the event log and the trace store, whose files pin it across
offsets.
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

from trellis.schemas.outcome import ComponentOutcome, OutcomeEvent
from trellis.stores.sqlite.outcome import (
    _CREATE_INDEXES,
    _CREATE_TABLE,
    _OCCURRED_AT_UTC_SQL,
    SQLiteOutcomeStore,
)

_INDEX = "idx_outcomes_occurred_at_utc"
_PARAMS_INDEX = "idx_outcomes_params_occurred_at_utc"
_COMPONENT_INDEX = "idx_outcomes_component_occurred_at_utc"

#: ``event_id`` -> the text an earlier build stored, in the order they happened.
_OLD_TEXTS = {
    "ahead": "2026-03-04T20:00:00+14:00",  # 06:00Z; its digits are the largest
    "whole": "2026-03-04T10:00:00+00:00",
    "frac": "2026-03-04T10:00:00.250000+00:00",
    "naive": "2026-03-04T12:00:00",
    "behind": "2026-03-04T06:00:00-11:00",  # 17:00Z; its digits are the smallest
}
_IN_ORDER = list(_OLD_TEXTS)
_NOON = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
_BEHIND = timezone(timedelta(hours=-11))
_AHEAD = timezone(timedelta(hours=14))


def _outcome(event_id: str, occurred_at: datetime, **fields: str) -> OutcomeEvent:
    return OutcomeEvent(
        event_id=event_id,
        component_id=fields.pop("component_id", "retrieve.strategies.KeywordSearch"),
        occurred_at=occurred_at,
        outcome=ComponentOutcome(success=True, latency_ms=1.0),
        **fields,
    )


def _write_as_an_earlier_build(
    db: Path, texts: dict[str, str] | None = None, *, journal: str = "DELETE"
) -> None:
    """Lay down one outcome per entry of *texts*, the text as given.

    The table and its indexes are the ones this build still creates: the
    migration only adds a column and indexes on top of them.
    """
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.execute(_CREATE_TABLE)
    for sql in _CREATE_INDEXES:
        conn.execute(sql)
    for event_id, text in (texts or _OLD_TEXTS).items():
        conn.execute(
            "INSERT INTO outcomes (event_id, component_id, occurred_at,"
            " recorded_at, success, latency_ms, outcome_json, metadata_json,"
            " schema_version) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                "comp",
                text,
                text,
                1,
                1.0,
                '{"success": true, "latency_ms": 1.0}',
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
            "SELECT event_id, component_id, occurred_at, recorded_at,"
            " outcome_json, metadata_json, schema_version"
            " FROM outcomes ORDER BY event_id"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def old_file(tmp_path: Path) -> Path:
    db = tmp_path / "old.db"
    _write_as_an_earlier_build(db)
    return db


def _ids(events: list[OutcomeEvent]) -> list[str]:
    return [e.event_id for e in events]


def _check_reads_by_instant(store: SQLiteOutcomeStore) -> None:
    assert _ids(store.query()) == _IN_ORDER
    # Inclusive at the very instant: 10:00:00Z is ``whole``, to the microsecond.
    ten = datetime(2026, 3, 4, 10, 0, tzinfo=UTC)
    assert _ids(store.query(until=ten)) == ["ahead", "whole"]
    assert _ids(store.query(since=ten)) == ["whole", "frac", "naive", "behind"]
    tick = timedelta(microseconds=1)
    assert _ids(store.query(since=ten + tick)) == ["frac", "naive", "behind"]
    assert _ids(store.query(since=ten, until=ten)) == ["whole"]
    # A bound given in another zone means its instant.
    assert _ids(store.query(since=_NOON.astimezone(_BEHIND))) == ["naive", "behind"]
    assert _ids(store.query(until=_NOON.astimezone(_AHEAD))) == _IN_ORDER[:-1]
    assert store.count(since=_NOON) == 2
    assert store.count(since=_NOON.replace(tzinfo=None)) == 2
    assert store.count(until=_NOON.astimezone(_BEHIND)) == 4
    assert store.count(since=ten, until=_NOON) == 3


# ----------------------------------------------------------------------
# The public API: what ``append`` writes, ``query`` and ``count`` read back
# ----------------------------------------------------------------------


def test_outcomes_appended_in_any_zone_are_read_by_instant(tmp_path: Path) -> None:
    store = SQLiteOutcomeStore(tmp_path / "new.db")
    try:
        # Written out of order, each as the offset it came with. The instants
        # are 06:00Z, 10:00Z, 11:00Z and 12:00Z; the digits order them 12, 20, 06
        # and 11 (naive).
        for label, stamp in (
            ("behind", _NOON.astimezone(_BEHIND)),
            ("utc", _NOON - timedelta(hours=2)),
            ("naive", (_NOON - timedelta(hours=1)).replace(tzinfo=None)),
            ("ahead", (_NOON - timedelta(hours=6)).astimezone(_AHEAD)),
        ):
            store.append(_outcome(label, stamp))
        assert _ids(store.query()) == ["ahead", "utc", "naive", "behind"]
        assert _ids(store.query(since=_NOON)) == ["behind"]
        assert _ids(store.query(until=_NOON - timedelta(hours=2))) == ["ahead", "utc"]
        in_behind = (_NOON - timedelta(hours=1)).astimezone(_BEHIND)
        assert _ids(store.query(since=in_behind, until=_NOON)) == ["naive", "behind"]
        assert store.count(since=_NOON.astimezone(_AHEAD)) == 1
        assert store.count(until=_NOON.replace(tzinfo=None)) == 4
        assert store.count(since=_NOON - timedelta(hours=2)) == 3
    finally:
        store.close()


def test_append_writes_the_text_it_was_given_and_query_returns_it(
    tmp_path: Path,
) -> None:
    """Comparing by instant must not rewrite what a caller reads back."""
    store = SQLiteOutcomeStore(tmp_path / "text.db")
    try:
        stamp = _NOON.astimezone(timezone(timedelta(hours=5, minutes=30)))
        store.append(_outcome("kept", stamp))
        (got,) = store.query()
        assert got.occurred_at == stamp
        assert got.occurred_at.utcoffset() == stamp.utcoffset()
        assert _rows(tmp_path / "text.db")[0][2] == stamp.isoformat()
    finally:
        store.close()


# ----------------------------------------------------------------------
# A file an earlier build wrote
# ----------------------------------------------------------------------


def test_rows_written_by_an_earlier_build_compare_by_instant(old_file: Path) -> None:
    store = SQLiteOutcomeStore(old_file)
    try:
        assert store._has_occurred_at_utc
        _check_reads_by_instant(store)
    finally:
        store.close()


def test_opening_an_old_file_rewrites_no_stored_row(old_file: Path) -> None:
    before = _rows(old_file)
    SQLiteOutcomeStore(old_file).close()
    assert _rows(old_file) == before


def test_the_migration_is_idempotent(old_file: Path) -> None:
    SQLiteOutcomeStore(old_file).close()
    with capture_logs() as logs:
        again = SQLiteOutcomeStore(old_file)
    try:
        assert again._has_occurred_at_utc
        assert not [e for e in logs if "migrat" in e["event"]]
        _check_reads_by_instant(again)
    finally:
        again.close()


def test_an_earlier_build_can_still_write_the_migrated_file(old_file: Path) -> None:
    SQLiteOutcomeStore(old_file).close()
    # The previous build's ``INSERT`` names its columns, so the new one is fine.
    old = sqlite3.connect(old_file)
    old.execute(
        "INSERT INTO outcomes (event_id, component_id, occurred_at, recorded_at,"
        " success, latency_ms, outcome_json, schema_version) VALUES ('later',"
        " 'comp', '2026-03-04T23:00:00+00:00', '2026-03-04T23:00:00+00:00', 1, 1.0,"
        " '{\"success\": true, \"latency_ms\": 1.0}', '0.1.0')"
    )
    old.commit()
    old.close()
    store = SQLiteOutcomeStore(old_file)
    try:
        assert _ids(store.query())[-1] == "later"
        assert _ids(store.query(since=_NOON + timedelta(hours=5))) == [
            "behind",
            "later",
        ]
    finally:
        store.close()


def test_a_new_file_gets_the_same_column_and_indexes(tmp_path: Path) -> None:
    store = SQLiteOutcomeStore(tmp_path / "new.db")
    try:
        assert store._has_occurred_at_utc
        columns = {
            r["name"] for r in store._conn.execute("PRAGMA table_xinfo(outcomes)")
        }
        assert "occurred_at_utc" in columns
        indexes = {
            r["name"]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        # The old index stays, because an earlier build expects it.
        assert {
            "idx_outcomes_occurred",
            _INDEX,
            _PARAMS_INDEX,
            _COMPONENT_INDEX,
        } <= indexes
    finally:
        store.close()


def test_an_old_row_holding_now_does_not_stop_the_store_opening(
    tmp_path: Path,
) -> None:
    # ``strftime`` reads ``now`` as the current time, which a generated column
    # refuses by raising; one such old row must not fail every open.
    db = tmp_path / "now.db"
    _write_as_an_earlier_build(db, {**_OLD_TEXTS, "weird": "now"})
    store = SQLiteOutcomeStore(db)
    try:
        assert store._has_occurred_at_utc
        # The row itself is unreadable as an ``OutcomeEvent`` (it never was
        # one), so count rather than load.
        assert store.count() == len(_OLD_TEXTS) + 1
        assert store.count(since=datetime(2026, 3, 4, 17, 0, tzinfo=UTC)) == 2
        store.append(_outcome("after", _NOON))
    finally:
        store.close()


def test_the_inline_expression_is_the_column(tmp_path: Path) -> None:
    """The unindexed path evaluates ``_OCCURRED_AT_UTC_SQL`` itself, so the two
    must agree on every row."""
    db = tmp_path / "inline.db"
    _write_as_an_earlier_build(db)
    store = SQLiteOutcomeStore(db)
    try:
        rows = store._conn.execute(
            f"SELECT occurred_at_utc, ({_OCCURRED_AT_UTC_SQL}) FROM outcomes"  # noqa: S608
        ).fetchall()
        assert len(rows) == len(_OLD_TEXTS)
        assert all(col == inline for col, inline in rows)
    finally:
        store.close()


# ----------------------------------------------------------------------
# Query plans and ties
# ----------------------------------------------------------------------


def test_queries_use_an_index_on_the_instant(tmp_path: Path) -> None:
    """The plan of each statement the store sends searches an index on the
    time, and none sorts: a read that names a ``params_version`` or a
    ``component_id`` is served by the index led by it, because the instant is a
    virtual column and is otherwise evaluated for every row that matches."""
    store = SQLiteOutcomeStore(tmp_path / "plans.db")
    try:
        for hour in range(3):
            store.append(_outcome(f"o{hour}", _NOON, params_version="v1"))
        sent: list[str] = []
        store._conn.set_trace_callback(sent.append)
        later = _NOON + timedelta(days=9000)
        comp = "retrieve.strategies.KeywordSearch"
        store.query(limit=5)
        store.query(since=_NOON, until=later, limit=5)
        store.count(since=_NOON, until=later)
        store.query(params_version="v1", since=_NOON, until=later, limit=5)
        store.count(params_version="v1", since=_NOON, until=later)
        store.query(component_id=comp, since=_NOON, limit=5)
        store.query(
            component_id=comp,
            domain="d",
            intent_family="plan",
            phase="retrieve",
            since=_NOON,
            limit=5,
        )
        store.query(component_id=comp, limit=5)
        store._conn.set_trace_callback(None)
        selects = [s for s in sent if s.startswith("SELECT") and "FROM outcomes" in s]
        assert len(selects) == 8
        for sql in selects:
            plan = " | ".join(
                r[3] for r in store._conn.execute("EXPLAIN QUERY PLAN " + sql)
            )
            assert any(i in plan for i in (_INDEX, _PARAMS_INDEX, _COMPONENT_INDEX)), (
                sql,
                plan,
            )
            assert "TEMP B-TREE" not in plan, (sql, plan)
            if "params_version = " in sql:
                assert _PARAMS_INDEX in plan, (sql, plan)
            elif "component_id = " in sql:
                assert _COMPONENT_INDEX in plan, (sql, plan)
    finally:
        store.close()


def _tied_corpus(store: SQLiteOutcomeStore) -> list[OutcomeEvent]:
    """30 outcomes on 5 instants (whole and fractional seconds, all UTC),
    written out of instant order, two components, so every ``limit`` below cuts
    inside a tie."""
    instants = [
        _NOON + timedelta(seconds=2),
        _NOON,
        _NOON + timedelta(seconds=3, microseconds=500000),
        _NOON + timedelta(seconds=1),
        _NOON + timedelta(seconds=3),
    ]
    written = []
    for i in range(30):
        event = _outcome(
            f"t{i:02d}",
            instants[i * 7 % 5],
            component_id="a" if i % 3 else "b",
        )
        store.append(event)
        written.append(event)
    return written


def test_every_plan_cuts_a_tie_the_same_way(tmp_path: Path) -> None:
    """Outcomes at one instant come back in insertion order whether the
    indexes, the inline expression, or the statement the store sent before the
    column existed does the work, so ``limit`` cuts the same rows whichever
    plan runs."""
    store = SQLiteOutcomeStore(tmp_path / "ties.db")
    try:
        written = _tied_corpus(store)
        shapes: list[dict] = [
            {},
            {"since": _NOON + timedelta(seconds=1)},
            {"component_id": "a"},
            {"component_id": "b", "since": _NOON + timedelta(seconds=1)},
        ]
        checked = 0
        for shape in shapes:
            kept = [
                e
                for e in written
                if shape.get("component_id") in (None, e.component_id)
                and shape.get("since", e.occurred_at) <= e.occurred_at
            ]
            want_all = sorted(kept, key=lambda e: e.occurred_at)  # stable
            clauses = ["1=1"]
            params: list = []
            if "component_id" in shape:
                clauses.append("component_id = ?")
                params.append(shape["component_id"])
            if "since" in shape:
                clauses.append("occurred_at >= ?")
                params.append(shape["since"].isoformat())
            for limit in (1, 4, 7, 100):
                want = [e.event_id for e in want_all[:limit]]
                indexed = _ids(store.query(**shape, limit=limit))
                store._has_occurred_at_utc = False
                try:
                    inline = _ids(store.query(**shape, limit=limit))
                finally:
                    store._has_occurred_at_utc = True
                base_sql = (
                    "SELECT event_id, occurred_at FROM outcomes WHERE "  # noqa: S608
                    + " AND ".join(clauses)
                    + " ORDER BY occurred_at ASC LIMIT ?"
                )
                base_rows = store._conn.execute(base_sql, [*params, limit]).fetchall()
                assert indexed == inline == want, (shape, limit)
                # The statement sent before the column existed always agreed on
                # the instants; its ties came back in scan order.
                assert [r[1] for r in base_rows] == [
                    e.occurred_at.isoformat() for e in want_all[:limit]
                ], ("base instants", shape, limit)
                checked += 1
        assert checked == 16
    finally:
        store.close()


def test_a_tie_across_offsets_is_cut_by_insertion_order_on_every_plan(
    tmp_path: Path,
) -> None:
    """The same instant written in four offsets (or none) ties under the
    instant key, and a read that scans ``idx_outcomes_scope`` then sorts hands
    the sort its rows in scope order, not insertion order. ``rowid`` in the
    ``ORDER BY`` is what makes ``limit`` cut the same rows on every plan."""
    zones = [UTC, timezone(timedelta(hours=-5)), _AHEAD, None]
    store = SQLiteOutcomeStore(tmp_path / "offset-ties.db")
    try:
        written: list[tuple[datetime, OutcomeEvent]] = []
        for i in range(48):
            instant = _NOON + timedelta(seconds=(i * 5) % 4)
            zone = zones[(i * 3 + i // 4) % 4]
            stamp = (
                instant.replace(tzinfo=None)
                if zone is None
                else instant.astimezone(zone)
            )
            event = _outcome(
                f"e{i:02d}",
                stamp,
                component_id="a" if i % 2 else "b",
                domain=f"d{i % 3 % 2}",
                intent_family=f"i{i % 5 % 2}",
                tool_name=f"t{i % 7 % 2}",
                phase=f"p{i % 11 % 2}",
            )
            store.append(event)
            written.append((instant, event))
        shapes: list[dict] = [
            {},
            {"component_id": "a"},
            {"domain": "d0"},
            {"component_id": "a", "domain": "d0"},
            {"component_id": "a", "domain": "d1", "intent_family": "i0"},
            {
                "component_id": "b",
                "domain": "d0",
                "intent_family": "i0",
                "tool_name": "t0",
            },
            {"component_id": "b", "domain": "d0", "intent_family": "i1", "phase": "p0"},
            {
                "component_id": "a",
                "domain": "d0",
                "since": _NOON + timedelta(seconds=1),
            },
        ]
        checked = 0
        for shape in shapes:
            kept = [
                (instant, e)
                for instant, e in written
                if all(getattr(e, k) == v for k, v in shape.items() if k != "since")
                and shape.get("since", instant) <= instant
            ]
            want_all = [e.event_id for _, e in sorted(kept, key=lambda p: p[0])]
            for limit in (1, 3, 5, 100):
                for column in (True, False):
                    store._has_occurred_at_utc = column
                    try:
                        got = _ids(store.query(**shape, limit=limit))
                    finally:
                        store._has_occurred_at_utc = True
                    assert got == want_all[:limit], (shape, limit, column)
                    checked += 1
        assert checked == len(shapes) * 4 * 2
    finally:
        store.close()


# ----------------------------------------------------------------------
# Migration is best effort: opening never depends on being able to write
# ----------------------------------------------------------------------

_SKIPPED = {"outcome_schema_migration_skipped", "outcome_index_creation_skipped"}


def _skips(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] in _SKIPPED]


def _has_column(db: Path) -> bool:
    conn = sqlite3.connect(db)
    try:
        return "occurred_at_utc" in {
            r[1] for r in conn.execute("PRAGMA table_xinfo(outcomes)")
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
            store = SQLiteOutcomeStore(db)
        elapsed = time.monotonic() - started
        try:
            # The connection waits ten seconds for a lock; opening must not.
            assert elapsed < 5
            (skipped,) = _skips(logs)
            assert skipped["event"] == "outcome_schema_migration_skipped"
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
    again = SQLiteOutcomeStore(db)
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
            store = SQLiteOutcomeStore(db)
        try:
            assert [e["event"] for e in _skips(logs)] == [
                "outcome_schema_migration_skipped"
            ]
            assert not store._has_occurred_at_utc
            _check_reads_by_instant(store)
        finally:
            store.close()
    finally:
        db.chmod(0o644)
