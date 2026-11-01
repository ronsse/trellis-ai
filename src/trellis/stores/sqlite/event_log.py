"""SQLiteEventLog — SQLite-backed append-only event log."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import structlog

from trellis.core.text_safety import scrub_jsonb_unsafe
from trellis.stores.base.event_log import (
    KNOWN_EVENT_TYPE_VALUES,
    Event,
    EventLog,
    EventOrder,
    EventType,
    partition_known_event_rows,
    warn_skipped_event_types,
)
from trellis.stores.sqlite.base import SQLiteStoreBase
from trellis.stores.sqlite.instant import (
    MigrationEvents,
    instant_key,
    instant_key_sql,
    migrate_instant_column,
)

logger = structlog.get_logger(__name__)


def _scrub_match_clause(column_sql: str, value: Any) -> tuple[str, list[Any]]:
    """Build a SQL fragment that matches ``column_sql`` against ``value``,
    scrubbed-aware.

    A SQLite row written before this module scrubbed on write (or by any
    direct writer that bypasses ``EventLog.append``) can still hold the
    *raw* NUL byte or lone surrogate a caller's filter value carries.
    When scrubbing the filter would change it, match either form --
    ``column_sql IN (?, ?)`` with the raw value first, the scrubbed value
    second -- so a legacy raw-stored row and a post-fix scrubbed-stored
    row are both reachable from the one value a caller actually has.
    When the value has no hazard, scrubbing is a no-op and a plain
    ``column_sql = ?`` is returned unchanged.

    A value holding a lone surrogate gets only the scrubbed equality:
    Python's ``sqlite3`` cannot bind one (UTF-8 encoding raises), so the
    raw arm would turn every such filter into an exception. A legacy
    ``payload_json`` value can still hold one as a JSON ``\\ud800``
    escape; a filter for it now misses that row rather than raising.

    Postgres never needs this: a Postgres ``text``/``jsonb`` column can
    never have stored a raw NUL or lone surrogate in the first place
    (the write raises), so its filters stay a plain scrubbed equality.

    ``payload_filters`` is typed ``dict[str, str]``, but a caller can
    still hand this a non-``str`` value (an ``int``, or a ``float`` NaN,
    whose ``!=``-to-itself skips the ``scrubbed == value`` shortcut
    below). ``scrub_jsonb_unsafe`` only ever touches ``str``/``dict``/
    ``list``/``tuple``, so a non-``str`` value is never a hazard and
    never needs the raw/scrubbed ``IN`` clause; the ``isinstance`` check
    routes it straight to the plain-equality return before the
    ``.encode`` call below, which only ``str`` supports.
    """
    scrubbed = scrub_jsonb_unsafe(value)
    if not isinstance(value, str) or scrubbed == value:
        return f"{column_sql} = ?", [value]
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return f"{column_sql} = ?", [scrubbed]
    return f"{column_sql} IN (?, ?)", [value, scrubbed]


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS events (
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
)"""

#: ``occurred_at`` is TEXT in whatever offset (or none) the writer's ``datetime``
#: carried, so the queries compare and order :data:`_OCCURRED_AT_UTC_SQL`, its
#: instant as fixed-width UTC (see :mod:`trellis.stores.sqlite.instant`), not
#: the text. ``idx_events_occurred_at`` stays: an earlier build expects it.
_OCCURRED_AT_UTC_SQL = instant_key_sql("occurred_at")

_OCCURRED_AT_UTC_INDEX = "idx_events_occurred_at_utc"

#: Led by ``event_type``, so a read that names a type and then compares or
#: orders the instant (the session dedup and feedback lookups, ``count`` for a
#: type) is a range scan, not an evaluation of the expression per matching row.
_TYPE_OCCURRED_AT_UTC_INDEX = (
    "idx_events_type_occurred_at_utc",
    "event_type, occurred_at_utc",
)

_MIGRATION_EVENTS = MigrationEvents(
    migrating="migrating_event_log_schema_add_occurred_at_utc",
    skipped="event_log_schema_migration_skipped",
    index_skipped="event_log_index_creation_skipped",
)

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_source ON events(source)",
]


class SQLiteEventLog(SQLiteStoreBase, EventLog):
    """SQLite-backed append-only event log.

    Note: Uses ``check_same_thread=False`` for compatibility with async
    frameworks but provides no internal locking. Callers must synchronise
    access when sharing a single instance across threads.
    """

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            cur.execute(idx_sql)
        self._conn.commit()
        self._has_occurred_at_utc = self._migrate_add_occurred_at_utc()

    def _event_columns(self) -> set[str]:
        return {row["name"] for row in self._conn.execute("PRAGMA table_xinfo(events)")}

    def _migrate_add_occurred_at_utc(self) -> bool:
        """Additive migration: index ``occurred_at`` by instant, not by text.

        See :func:`~trellis.stores.sqlite.instant.migrate_instant_column`; no
        stored row is rewritten. Returns whether the column exists afterwards;
        when it does not, :meth:`_instant` evaluates the same expression inline,
        which returns the same rows and is merely unindexed.
        """
        return migrate_instant_column(
            self._conn,
            db_path=self._db_path,
            table="events",
            source_column="occurred_at",
            instant_column="occurred_at_utc",
            index=_OCCURRED_AT_UTC_INDEX,
            columns=self._event_columns,
            events=_MIGRATION_EVENTS,
            also_indexes=(_TYPE_OCCURRED_AT_UTC_INDEX,),
        )

    def _instant(self) -> str:
        """The SQL the time bounds and ``ORDER BY`` read: the indexed column
        when the migration ran, else the expression it holds."""
        if self._has_occurred_at_utc:
            return "occurred_at_utc"
        return f"({_OCCURRED_AT_UTC_SQL})"

    # -- mutations -----------------------------------------------------------

    def _append(self, event: Event) -> None:
        """Append event (immutable, no updates). Called by ``EventLog.append``
        only, after it has scrubbed ``event`` -- see the base class.

        An INSERT that fails (a duplicate ``event_id``, say) has already
        taken the database's write lock, so it is rolled back rather than
        left open, holding that lock against every other connection.
        """
        conn = self._conn
        with conn:  # commits on success, rolls back on an exception
            conn.execute(
                "INSERT INTO events "
                "(event_id, event_type, source, entity_id, entity_type, "
                "occurred_at, recorded_at, payload_json, metadata_json, "
                "schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    str(event.event_type),
                    event.source,
                    event.entity_id,
                    event.entity_type,
                    event.occurred_at.isoformat(),
                    event.recorded_at.isoformat(),
                    json.dumps(event.payload, allow_nan=False),
                    json.dumps(event.metadata, allow_nan=False),
                    event.schema_version,
                ),
            )
        logger.debug(
            "event_log.appended",
            event_id=event.event_id,
            event_type=str(event.event_type),
        )

    # -- idempotency ---------------------------------------------------------

    def has_idempotency_key(self, key: str) -> bool:
        """Efficient single-row check for an idempotency key.

        ``key`` is matched scrub-aware (``_scrub_match_clause``) so it
        agrees with what ``append`` would have stored for it, and also
        finds a legacy row stored before that scrub existed; see the
        base class's ``has_idempotency_key`` docstring for why this
        never bites in practice (Stage 1 validation refuses a
        hazardous key outright).
        """
        clause, value_params = _scrub_match_clause(
            "json_extract(payload_json, '$.idempotency_key')", key
        )
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT 1 FROM events WHERE event_type = ? AND {clause} LIMIT 1",
            [str(EventType.MUTATION_EXECUTED), *value_params],
        )
        return cur.fetchone() is not None

    # -- queries -------------------------------------------------------------

    def get_event(self, event_id: str) -> Event | None:
        """Direct primary-key lookup -- reaches the whole log, not a window.

        A row whose type this build does not know reads as absent, as it does
        from :meth:`get_events`, and is logged the same way.
        """
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
        row = cur.fetchone()
        if row is None:
            return None
        known, skipped = partition_known_event_rows([row], lambda r: r["event_type"])
        if skipped:
            warn_skipped_event_types(skipped, operation="get_event")
            return None
        return self._row_to_event(known[0])

    def get_events(
        self,
        *,
        event_type: EventType | None = None,
        entity_id: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        order: EventOrder = "asc",
        payload_filters: dict[str, str] | None = None,
    ) -> list[Event]:
        """Query events with filters.

        ``entity_id``, ``source`` and each ``payload_filters`` value are
        matched scrub-aware (``_scrub_match_clause``): scrubbed the same
        way ``append`` scrubs the corresponding stored field, so a
        caller filtering by the raw string it originally handed
        ``emit()`` matches a post-fix (scrubbed-on-write) row, *and*
        also matches the raw value directly, so a legacy row written
        before this module scrubbed on write stays findable too.
        ``payload_filters`` keys are left alone: they are JSON field
        names chosen by code, not caller free text, and no shipped
        caller names one containing either hazard.

        A row whose ``event_type`` this build does not know is left out and
        logged (see :meth:`EventLog.get_events`). When such a row sat in a full
        page, the page is read again restricted to the types this build knows,
        so ``limit`` still returns up to ``limit`` readable rows.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(str(event_type))
        if entity_id is not None:
            clause, value_params = _scrub_match_clause("entity_id", entity_id)
            clauses.append(clause)
            params.extend(value_params)
        if source is not None:
            clause, value_params = _scrub_match_clause("source", source)
            clauses.append(clause)
            params.extend(value_params)
        instant = self._instant()
        if since is not None:
            clauses.append(f"{instant} >= ?")
            params.append(instant_key(since))
        if until is not None:
            clauses.append(f"{instant} <= ?")
            params.append(instant_key(until))
        if payload_filters:
            for key, value in payload_filters.items():
                # Build the JSON path with concatenation so the key is a
                # bound parameter, not interpolated SQL — this keeps the
                # caller-supplied key safe from injection. Stored as TEXT
                # under ``payload_json``; ``json_extract`` returns the
                # underlying scalar so plain string comparison matches
                # JSON string values.
                clause, value_params = _scrub_match_clause(
                    "json_extract(payload_json, '$.' || ?)", value
                )
                clauses.append(clause)
                params.append(key)
                params.extend(value_params)

        where = " AND ".join(clauses) if clauses else "1=1"
        direction = "DESC" if order == "desc" else "ASC"

        def read(where_sql: str, bound: list[Any]) -> list[sqlite3.Row]:
            # ``rowid`` in the same direction breaks ties among events at one
            # instant, so ``limit`` cuts the same rows whichever plan runs (the
            # indexes, or the inline expression on a file that could not
            # migrate). Every index on the instant carries ``rowid`` last, so
            # it adds no sort.
            sql = (
                f"SELECT * FROM events WHERE {where_sql} "
                f"ORDER BY {instant} {direction}, rowid {direction} LIMIT ?"
            )
            cur = self._conn.cursor()
            cur.execute(sql, [*bound, limit])
            return cur.fetchall()

        rows = read(where, params)
        known, skipped = partition_known_event_rows(rows, lambda r: r["event_type"])
        if skipped:
            warn_skipped_event_types(skipped, operation="get_events")
            if limit > 0 and len(rows) >= limit:
                # The page was full, so the skipped rows used up some of it and
                # a caller reading ``len(events) >= limit`` as "there is more"
                # would read it as the end. Ask for readable rows only. The unary
                # ``+`` keeps the planner off the type index, which matches nearly
                # every row and would sort them all to return one page.
                marks = ", ".join("?" * len(KNOWN_EVENT_TYPE_VALUES))
                known = read(
                    f"{where} AND +event_type IN ({marks})",
                    [*params, *KNOWN_EVENT_TYPE_VALUES],
                )
        return [self._row_to_event(row) for row in known]

    def count(
        self,
        *,
        event_type: EventType | None = None,
        since: datetime | None = None,
    ) -> int:
        """Count events with optional filters.

        Counts every stored row, including rows of a type this build cannot
        read, so it can exceed what :meth:`get_events` returns.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(str(event_type))
        if since is not None:
            clauses.append(f"{self._instant()} >= ?")
            params.append(instant_key(since))

        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"SELECT COUNT(*) FROM events WHERE {where}"

        cur = self._conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row else 0

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            event_type=EventType(row["event_type"]),
            source=row["source"],
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            payload=json.loads(row["payload_json"]),
            metadata=json.loads(row["metadata_json"]),
            schema_version=row["schema_version"],
        )
