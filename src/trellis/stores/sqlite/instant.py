"""Compare a TEXT timestamp column by instant, not by its text.

SQLite has no timestamp type: ``traces.created_at`` and ``events.occurred_at``
hold whatever ``datetime.isoformat()`` the writer produced, with its own offset
(or none) and a fraction that is absent when it is zero. A text comparison, or an
``ORDER BY`` on the column, then sorts by wall-clock digits. This module holds
the one expression that reads the instant out of that text, the bound that goes
with it, and the additive migration that indexes it, for every store that keeps
such a column.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import structlog

from trellis.stores.base.trace import as_utc

logger = structlog.get_logger(__name__)

#: How long opening a store waits for the write lock the migration needs. The
#: migration is optional (see :func:`migrate_instant_column`), so a reader does
#: not wait out the connection's own timeout behind a job that holds the lock.
MIGRATION_BUSY_TIMEOUT_MS = 250


class MigrationEvents(NamedTuple):
    """The log event names one store's migration emits, so each store's stay
    greppable as plain strings."""

    migrating: str
    skipped: str
    index_skipped: str


def instant_key_sql(column: str) -> str:
    """SQL yielding the instant of ``column``'s text as fixed-width UTC.

    ``YYYY-MM-DDTHH:MM:SS.ffffff``, so plain string order is chronological order
    whatever offset (or none) the text was written with. A naive text is read as
    UTC, as :func:`~trellis.stores.base.trace.as_utc` does.

    Python writes the column with ``isoformat()``: the wall clock to the
    second, then a fraction that is absent or exactly six digits, then an offset
    that is absent or ``+HH:MM``, with ``:SS`` and ``.ffffff`` when it has them.
    That fixes where the offset starts. ``strftime`` is handed the wall clock
    alone, and the offset's sign, hours, minutes and seconds go in as one
    modifier of the opposite sign (``-05:30:15`` for ``+05:30:15``), so any
    offset Python can write is read, not only the ones ``strftime`` accepts in
    the text. The fraction never reaches ``strftime``, which rounds to the
    millisecond, and is re-attached verbatim, so the key keeps the microsecond.

    Three limits. An offset's own fraction (``+05:30:15.5``, which a stdlib
    ``timezone(timedelta(seconds=15, microseconds=500000))`` writes) is dropped,
    so that key is within a second of the instant. Text Python cannot write is
    keyed deterministically by what this expression makes of it, not always by
    its raw digits: ``.123+00:00`` keeps them, so such a row stays in a bounded
    query instead of dropping out on a NULL, while ``00.5+5:0`` becomes
    ``00.5+5:00``. An old row past what ``datetime`` can name in UTC
    (``0001-01-01T00:00+05:30``, ``9999-12-31T23:00-05:00``) has a key outside
    the edge ``as_utc`` clamps a bound to, so a query bounded by its own stamp
    can miss it.

    The ``GLOB`` keeps text that does not start with a year away from
    ``strftime``: ``strftime`` reads ``now`` as the current time, which a
    generated column and its index refuse by raising, and it stops at a NUL, so
    ``now\\x00x`` is read as ``now`` and ``lower(column) = 'now'`` would miss it.
    One such old row would otherwise fail every open.
    """
    offset = (
        f"substr({column}, CASE WHEN substr({column}, 20, 1) = '.' THEN 27 ELSE 20 END)"
    )
    return f"""CASE WHEN {column} GLOB '[0-9][0-9][0-9][0-9]-*' THEN
    COALESCE(
        strftime(
            '%Y-%m-%dT%H:%M:%S',
            substr({column}, 1, 19),
            CASE
                WHEN {offset} IN ('', 'Z') THEN '+00:00'
                WHEN substr({offset}, 1, 1) = '+'
                    THEN '-' || substr({offset}, 2, 8)
                WHEN substr({offset}, 1, 1) = '-'
                    THEN '+' || substr({offset}, 2, 8)
            END
        )
        || '.'
        || substr(
            CASE WHEN substr({column}, 20, 1) = '.'
                THEN substr({column}, 21, 6) ELSE '' END
            || '000000',
            1, 6
        ),
        {column}
    )
ELSE {column} END"""


def instant_key(moment: datetime) -> str:
    """*moment* in the form :func:`instant_key_sql` yields, for a bound."""
    return as_utc(moment).isoformat(timespec="microseconds")[: -len("+00:00")]


def migrate_instant_column(
    conn: sqlite3.Connection,
    *,
    db_path: Path | str,
    table: str,
    source_column: str,
    instant_column: str,
    index: str,
    columns: Callable[[], set[str]],
    events: MigrationEvents,
) -> bool:
    """Additive migration: index ``source_column`` by instant, not by text.

    Rows written by earlier builds carry whatever offset (or none) the writer
    had, so comparing and ordering the text is wrong across offsets. A virtual
    generated column derives the instant from that text on read, which covers
    the old rows without touching one: no stored value is rewritten, and an
    older build that opens the file afterwards still reads and writes it (its
    ``INSERT`` names its columns). Idempotent.

    Best effort. Opening a store must not depend on being able to migrate it: a
    read-only file, or one another connection holds the write lock on, would
    otherwise stop a reader that opened fine before. A statement that fails with
    ``OperationalError`` logs a warning and the next open tries again. Returns
    whether the column exists afterwards; when it does not, the caller evaluates
    :func:`instant_key_sql` inline, which returns the same rows and is merely
    unindexed. ``columns`` lists the table's columns, generated ones included
    (``PRAGMA table_xinfo``; ``table_info`` hides them).
    """
    present = columns()
    indexed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index,),
    ).fetchone()
    if instant_column in present and indexed:
        return True

    (prior_timeout_ms,) = conn.execute("PRAGMA busy_timeout").fetchone()
    conn.execute(f"PRAGMA busy_timeout={MIGRATION_BUSY_TIMEOUT_MS}")
    try:
        if instant_column not in present:
            logger.info(events.migrating)
            try:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {instant_column} TEXT "
                    f"GENERATED ALWAYS AS ({instant_key_sql(source_column)}) VIRTUAL"
                )
            except sqlite3.OperationalError as exc:
                # A concurrent opener may have added it first. The re-read can
                # be refused too (a journal-mode file under another
                # connection's exclusive lock), and then the column counts as
                # absent: opening must not raise.
                try:
                    added = instant_column in columns()
                except sqlite3.OperationalError:
                    added = False
                if not added:
                    logger.warning(
                        events.skipped,
                        db_path=str(db_path),
                        error=str(exc),
                    )
                    return False
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {index} ON {table}({instant_column})"
            )
        except sqlite3.OperationalError as exc:
            logger.warning(
                events.index_skipped,
                db_path=str(db_path),
                error=str(exc),
            )
        conn.commit()
        return True
    finally:
        conn.execute(f"PRAGMA busy_timeout={int(prior_timeout_ms)}")
