"""PostgresEventLog — Postgres-backed append-only event log."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import structlog

from trellis.stores.base.event_log import Event, EventLog, EventOrder, EventType
from trellis.stores.postgres.base import PostgresStoreBase

logger = structlog.get_logger(__name__)

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    entity_id TEXT,
    entity_type TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}',
    schema_version TEXT
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_source ON events(source)",
    # Composite index for ``get_events(event_type=X, order="desc", limit=N)``.
    # Lets Postgres serve a recency-ordered slice of a single event type as an
    # index range scan with no sort step.
    "CREATE INDEX IF NOT EXISTS idx_events_type_occurred_desc "
    "ON events(event_type, occurred_at DESC)",
    # Composite index for entity-history lookups
    # (``get_events(entity_id=X)`` ordered by recency).
    "CREATE INDEX IF NOT EXISTS idx_events_entity_occurred_desc "
    "ON events(entity_id, occurred_at DESC)",
    # Partial expression index for ``_feedback_id_in_event_log`` — turns the
    # 10K-row scan it currently does into an O(log N) JSON-key probe.
    "CREATE INDEX IF NOT EXISTS idx_events_feedback_id "
    "ON events ((payload->>'feedback_id')) "
    "WHERE event_type = 'feedback.recorded'",
    # Partial expression index for ``has_idempotency_key``. Targets the
    # ``WHERE event_type = 'mutation.executed' AND payload->>'idempotency_key' = %s``
    # query the mutation pipeline runs on every command.
    "CREATE INDEX IF NOT EXISTS idx_events_idempotency_key "
    "ON events ((payload->>'idempotency_key')) "
    "WHERE event_type = 'mutation.executed'",
]


class PostgresEventLog(PostgresStoreBase, EventLog):
    """Postgres-backed append-only event log using psycopg v3 (sync)."""

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, event: Event) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events
                    (event_id, event_type, source, entity_id, entity_type,
                     occurred_at, recorded_at, payload, metadata, schema_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event.event_id,
                    str(event.event_type),
                    event.source,
                    event.entity_id,
                    event.entity_type,
                    event.occurred_at,
                    event.recorded_at,
                    json.dumps(event.payload),
                    json.dumps(event.metadata),
                    event.schema_version,
                ),
            )
        logger.debug(
            "event_log.appended",
            event_id=event.event_id,
            event_type=str(event.event_type),
        )

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def has_idempotency_key(self, key: str) -> bool:
        """Efficient single-row check for an idempotency key."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM events WHERE event_type = %s "
                "AND payload->>'idempotency_key' = %s LIMIT 1",
                (str(EventType.MUTATION_EXECUTED), key),
            )
            return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

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
        clauses: list[str] = []
        params: list[Any] = []

        if event_type is not None:
            clauses.append("event_type = %s")
            params.append(str(event_type))
        if entity_id is not None:
            clauses.append("entity_id = %s")
            params.append(entity_id)
        if source is not None:
            clauses.append("source = %s")
            params.append(source)
        if since is not None:
            clauses.append("occurred_at >= %s")
            params.append(since)
        if until is not None:
            clauses.append("occurred_at <= %s")
            params.append(until)
        if payload_filters:
            for key, value in payload_filters.items():
                # ``payload->>'k' = 'v'`` returns TEXT; callers comparing
                # ints / bools must coerce to str. JSONB makes this
                # GIN-indexable but no index is required for correctness.
                clauses.append("payload->>%s = %s")
                params.extend([key, value])

        where = " AND ".join(clauses) if clauses else "1=1"
        direction = "DESC" if order == "desc" else "ASC"
        sql = (
            f"SELECT * FROM events WHERE {where} "
            f"ORDER BY occurred_at {direction} LIMIT %s"
        )
        params.append(limit)

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_event(row) for row in rows]

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    def count(
        self,
        *,
        event_type: EventType | None = None,
        since: datetime | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []

        if event_type is not None:
            clauses.append("event_type = %s")
            params.append(str(event_type))
        if since is not None:
            clauses.append("occurred_at >= %s")
            params.append(since)

        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"SELECT COUNT(*) FROM events WHERE {where}"

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_event(row: tuple[Any, ...]) -> Event:
        payload_raw = row[7]
        if isinstance(payload_raw, str):
            payload = json.loads(payload_raw)
        elif isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            payload = {}

        metadata_raw = row[8]
        if isinstance(metadata_raw, str):
            metadata = json.loads(metadata_raw)
        elif isinstance(metadata_raw, dict):
            metadata = metadata_raw
        else:
            metadata = {}

        return Event(
            event_id=row[0],
            event_type=EventType(row[1]),
            source=row[2],
            entity_id=row[3],
            entity_type=row[4],
            occurred_at=(
                row[5]
                if isinstance(row[5], datetime)
                else datetime.fromisoformat(row[5])
            ),
            recorded_at=(
                row[6]
                if isinstance(row[6], datetime)
                else datetime.fromisoformat(row[6])
            ),
            payload=payload,
            metadata=metadata,
            schema_version=row[9],
        )
