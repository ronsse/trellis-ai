"""SQLiteTraceStore — SQLite-backed immutable trace store."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import structlog

from trellis.errors import StoreError
from trellis.schemas.trace import Trace
from trellis.stores.base.trace import TraceStore, as_utc
from trellis.stores.sqlite.base import SQLiteStoreBase
from trellis.stores.sqlite.instant import (
    MigrationEvents,
    instant_key,
    instant_key_sql,
    migrate_instant_column,
)

logger = structlog.get_logger(__name__)

#: The instant of a row's ``created_at`` text as fixed-width UTC; the expression,
#: its limits and the migration that indexes it are shared with the event log
#: (:mod:`trellis.stores.sqlite.instant`).
_CREATED_AT_UTC_SQL = instant_key_sql("created_at")

_CREATED_AT_UTC_INDEX = "idx_traces_created_at_utc"

_instant_key = instant_key

_MIGRATION_EVENTS = MigrationEvents(
    migrating="migrating_trace_schema_add_created_at_utc",
    skipped="trace_schema_migration_skipped",
    index_skipped="trace_index_creation_skipped",
)


class SQLiteTraceStore(SQLiteStoreBase, TraceStore):
    """SQLite-backed immutable trace store.

    Note: Uses ``check_same_thread=False`` for compatibility with async
    frameworks but provides no internal locking. Callers must synchronise
    access when sharing a single instance across threads.
    """

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                intent TEXT NOT NULL,
                domain TEXT,
                agent_id TEXT,
                outcome_status TEXT,
                trace_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_traces_source ON traces(source);
            CREATE INDEX IF NOT EXISTS idx_traces_domain ON traces(domain);
            CREATE INDEX IF NOT EXISTS idx_traces_agent ON traces(agent_id);
            CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);
            CREATE INDEX IF NOT EXISTS idx_traces_outcome ON traces(outcome_status);
        """)
        self._conn.commit()
        self._has_created_at_utc = self._migrate_add_created_at_utc()

    def _trace_columns(self) -> set[str]:
        return {row["name"] for row in self._conn.execute("PRAGMA table_xinfo(traces)")}

    def _migrate_add_created_at_utc(self) -> bool:
        """Additive migration: index ``created_at`` by instant, not by text.

        See :func:`~trellis.stores.sqlite.instant.migrate_instant_column`.
        Returns whether the column exists afterwards; when it does not,
        :meth:`query` evaluates the same expression inline, which returns the
        same rows and is merely unindexed.
        """
        return migrate_instant_column(
            self._conn,
            db_path=self._db_path,
            table="traces",
            source_column="created_at",
            instant_column="created_at_utc",
            index=_CREATED_AT_UTC_INDEX,
            columns=self._trace_columns,
            events=_MIGRATION_EVENTS,
        )

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, trace: Trace) -> str:
        domain = trace.context.domain if trace.context else None
        agent_id = trace.context.agent_id if trace.context else None
        outcome_status = trace.outcome.status.value if trace.outcome else None

        conn = self._conn
        try:
            with conn:  # commits on success, rolls back on an exception
                conn.execute(
                    """
                    INSERT INTO traces
                        (trace_id, source, intent, domain, agent_id,
                         outcome_status, trace_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace.trace_id,
                        trace.source.value,
                        trace.intent,
                        domain,
                        agent_id,
                        outcome_status,
                        trace.model_dump_json(),
                        # The column is the instant in UTC; ``trace_json`` is
                        # the trace's own JSON, unchanged.
                        as_utc(trace.created_at).isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            msg = f"Trace {trace.trace_id} already exists"
            raise StoreError(msg) from exc

        logger.debug("trace_appended", trace_id=trace.trace_id)
        return trace.trace_id

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    def get(self, trace_id: str) -> Trace | None:
        cursor = self._conn.execute(
            "SELECT trace_json FROM traces WHERE trace_id = ?", (trace_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return Trace.model_validate_json(row["trace_json"])  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        source: str | None = None,
        domain: str | None = None,
        agent_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[Trace]:
        conditions: list[str] = []
        params: list[str | int] = []

        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if domain is not None:
            conditions.append("domain = ?")
            params.append(domain)
        if agent_id is not None:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        # The indexed column when the migration ran, else the expression it
        # holds: the same rows, found by a scan.
        instant = (
            "created_at_utc"
            if self._has_created_at_utc
            else (f"({_CREATED_AT_UTC_SQL})")
        )
        if since is not None:
            conditions.append(f"{instant} >= ?")
            params.append(_instant_key(since))
        if until is not None:
            conditions.append(f"{instant} <= ?")
            params.append(_instant_key(until))

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        params.append(limit)

        sql = f"SELECT trace_json FROM traces {where} ORDER BY {instant} DESC LIMIT ?"
        cursor = self._conn.execute(sql, params)
        return [
            Trace.model_validate_json(row["trace_json"]) for row in cursor.fetchall()
        ]

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    def count(self, *, source: str | None = None, domain: str | None = None) -> int:
        conditions: list[str] = []
        params: list[str] = []

        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if domain is not None:
            conditions.append("domain = ?")
            params.append(domain)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        cursor = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM traces {where}", params
        )
        row = cursor.fetchone()
        assert row is not None
        return int(row["cnt"])

    def count_by_domain(self) -> dict[str, int]:
        cursor = self._conn.execute(
            "SELECT domain, COUNT(*) as cnt FROM traces"
            " WHERE domain IS NOT NULL GROUP BY domain"
        )
        return {row["domain"]: int(row["cnt"]) for row in cursor.fetchall()}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
