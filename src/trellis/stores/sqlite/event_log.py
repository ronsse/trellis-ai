"""SQLiteEventLog — SQLite-backed append-only event log."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import structlog

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

    def append(self, event: Event) -> None:
        """Append event (immutable, no updates)."""
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO events "
            "(event_id, event_type, source, entity_id, entity_type, "
            "occurred_at, recorded_at, payload_json, metadata_json, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                str(event.event_type),
                event.source,
                event.entity_id,
                event.entity_type,
                event.occurred_at.isoformat(),
                event.recorded_at.isoformat(),
                json.dumps(event.payload),
                json.dumps(event.metadata),
                event.schema_version,
            ),
        )
        self._conn.commit()
        logger.debug(
            "event_log.appended",
            event_id=event.event_id,
            event_type=str(event.event_type),
        )

    # -- idempotency ---------------------------------------------------------

    def has_idempotency_key(self, key: str) -> bool:
        """Efficient single-row check for an idempotency key."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT 1 FROM events WHERE event_type = ? "
            "AND json_extract(payload_json, '$.idempotency_key') = ? LIMIT 1",
            (str(EventType.MUTATION_EXECUTED), key),
        )
        return cur.fetchone() is not None

    # -- queries -------------------------------------------------------------

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
        """Query events with filters."""
        clauses: list[str] = []
        params: list[Any] = []

        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(str(event_type))
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
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
                params.extend([key, value])

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
