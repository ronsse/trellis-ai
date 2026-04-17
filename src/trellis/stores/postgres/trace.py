"""PostgresTraceStore — Postgres-backed immutable trace store."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog

from trellis.errors import StoreError
from trellis.schemas.trace import Trace
from trellis.stores.base.trace import TraceStore
from trellis.stores.postgres.base import PostgresStoreBase

logger = structlog.get_logger(__name__)

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    intent TEXT NOT NULL,
    domain TEXT,
    agent_id TEXT,
    outcome_status TEXT,
    trace_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_traces_source ON traces(source)",
    "CREATE INDEX IF NOT EXISTS idx_traces_domain ON traces(domain)",
    "CREATE INDEX IF NOT EXISTS idx_traces_agent ON traces(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_traces_outcome ON traces(outcome_status)",
]


class PostgresTraceStore(PostgresStoreBase, TraceStore):
    """Postgres-backed immutable trace store using psycopg v3 (sync)."""

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(self, trace: Trace) -> str:
        import psycopg.errors  # noqa: PLC0415

        domain = trace.context.domain if trace.context else None
        agent_id = trace.context.agent_id if trace.context else None
        outcome_status = trace.outcome.status.value if trace.outcome else None

        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO traces
                        (trace_id, source, intent, domain, agent_id,
                         outcome_status, trace_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        trace.trace_id,
                        trace.source.value,
                        trace.intent,
                        domain,
                        agent_id,
                        outcome_status,
                        trace.model_dump_json(),
                        trace.created_at,
                    ),
                )
            self.conn.commit()
        except psycopg.errors.UniqueViolation as exc:
            self.conn.rollback()
            msg = f"Trace {trace.trace_id} already exists"
            raise StoreError(msg) from exc

        logger.debug("trace_appended", trace_id=trace.trace_id)
        return trace.trace_id

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    def get(self, trace_id: str) -> Trace | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT trace_json FROM traces WHERE trace_id = %s",
                (trace_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        raw = row[0]
        # JSONB columns may come back as dict or str depending on driver
        if isinstance(raw, dict):
            return Trace.model_validate(raw)  # type: ignore[no-any-return]
        return Trace.model_validate_json(raw)  # type: ignore[no-any-return]

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
        params: list[Any] = []

        if source is not None:
            conditions.append("source = %s")
            params.append(source)
        if domain is not None:
            conditions.append("domain = %s")
            params.append(domain)
        if agent_id is not None:
            conditions.append("agent_id = %s")
            params.append(agent_id)
        if since is not None:
            conditions.append("created_at >= %s")
            params.append(since)
        if until is not None:
            conditions.append("created_at <= %s")
            params.append(until)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        params.append(limit)

        sql = f"SELECT trace_json FROM traces {where} ORDER BY created_at DESC LIMIT %s"
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        results: list[Trace] = []
        for row in rows:
            raw = row[0]
            if isinstance(raw, dict):
                results.append(Trace.model_validate(raw))
            else:
                results.append(Trace.model_validate_json(raw))
        return results

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    def count(self, *, source: str | None = None, domain: str | None = None) -> int:
        conditions: list[str] = []
        params: list[Any] = []

        if source is not None:
            conditions.append("source = %s")
            params.append(source)
        if domain is not None:
            conditions.append("domain = %s")
            params.append(domain)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        with self.conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM traces {where}", params)
            row = cur.fetchone()
        assert row is not None
        return int(row[0])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
