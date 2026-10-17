"""SQLiteEventLog — SQLite-backed append-only event log."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import structlog

from trellis.core.text_safety import scrub_jsonb_unsafe
from trellis.stores.base.event_log import Event, EventLog, EventOrder, EventType
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


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

        ``key`` is scrubbed before the lookup so it agrees with what
        ``append`` would have stored for it; see the base class's
        ``has_idempotency_key`` docstring for why this never bites in
        practice (Stage 1 validation refuses a hazardous key outright).
        """
        cur = self._conn.cursor()
        cur.execute(
            "SELECT 1 FROM events WHERE event_type = ? "
            "AND json_extract(payload_json, '$.idempotency_key') = ? LIMIT 1",
            (str(EventType.MUTATION_EXECUTED), scrub_jsonb_unsafe(key)),
        )
        return cur.fetchone() is not None

    # -- queries -------------------------------------------------------------

    def get_event(self, event_id: str) -> Event | None:
        """Direct primary-key lookup -- reaches the whole log, not a window."""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
        row = cur.fetchone()
        return self._row_to_event(row) if row is not None else None

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
        scrubbed the same way ``append`` scrubs the corresponding stored
        field, so a caller filtering by the raw string it originally
        handed ``emit()`` still matches the (possibly scrubbed) row
        instead of silently returning nothing. ``payload_filters`` keys
        are left alone: they are JSON field names chosen by code, not
        caller free text, and no shipped caller names one containing
        either hazard.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(str(event_type))
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(scrub_jsonb_unsafe(entity_id))
        if source is not None:
            clauses.append("source = ?")
            params.append(scrub_jsonb_unsafe(source))
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("occurred_at <= ?")
            params.append(until.isoformat())
        if payload_filters:
            for key, value in payload_filters.items():
                # Build the JSON path with concatenation so the key is a
                # bound parameter, not interpolated SQL — this keeps the
                # caller-supplied key safe from injection. Stored as TEXT
                # under ``payload_json``; ``json_extract`` returns the
                # underlying scalar so plain string comparison matches
                # JSON string values.
                clauses.append("json_extract(payload_json, '$.' || ?) = ?")
                params.extend([key, scrub_jsonb_unsafe(value)])

        where = " AND ".join(clauses) if clauses else "1=1"
        direction = "DESC" if order == "desc" else "ASC"
        sql = (
            f"SELECT * FROM events WHERE {where} "
            f"ORDER BY occurred_at {direction} LIMIT ?"
        )
        params.append(limit)

        cur = self._conn.cursor()
        cur.execute(sql, params)
        return [self._row_to_event(row) for row in cur.fetchall()]

    def count(
        self,
        *,
        event_type: EventType | None = None,
        since: datetime | None = None,
    ) -> int:
        """Count events with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []

        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(str(event_type))
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since.isoformat())

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
