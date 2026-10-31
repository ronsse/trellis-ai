"""SQLiteTraceStore — SQLite-backed immutable trace store."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import structlog

from trellis.errors import StoreError
from trellis.schemas.trace import Trace
from trellis.stores.base.trace import TraceStore, as_utc
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)

#: The instant of a row's ``created_at`` text as fixed-width UTC,
#: ``YYYY-MM-DDTHH:MM:SS.ffffff``, so plain string order is chronological order
#: whatever offset (or none) the text was written with. A naive text is read as
#: UTC, as :func:`~trellis.stores.base.trace.as_utc` does.
#:
#: Python writes ``created_at`` with ``isoformat()``, so the fraction is absent
#: or exactly six digits, which fixes where the offset starts. The offset is
#: handed to ``strftime`` with the fraction cut off because ``strftime`` rounds
#: to the millisecond, and a ``.9996`` would otherwise carry into the next
#: second. The fraction is re-attached verbatim, so the key keeps the
#: microsecond. Text ``strftime`` cannot read (an offset beyond +/-14:00, or one
#: with seconds) falls back to the raw text, so such a row stays visible
#: ordered by its wall clock, as it was before, instead of dropping out of every
#: bounded query on a NULL. ``append`` never writes one.
_CREATED_AT_UTC_SQL = """COALESCE(
    strftime(
        '%Y-%m-%dT%H:%M:%S',
        substr(created_at, 1, 19)
        || substr(
            created_at,
            CASE WHEN substr(created_at, 20, 1) = '.' THEN 27 ELSE 20 END
        )
    )
    || '.'
    || substr(
        CASE WHEN substr(created_at, 20, 1) = '.'
            THEN substr(created_at, 21, 6) ELSE '' END
        || '000000',
        1, 6
    ),
    created_at
)"""


def _instant_key(moment: datetime) -> str:
    """*moment* in the form ``_CREATED_AT_UTC_SQL`` yields, for a bound."""
    return as_utc(moment).isoformat(timespec="microseconds")[: -len("+00:00")]


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
        self._migrate_add_created_at_utc()

    def _migrate_add_created_at_utc(self) -> None:
        """Additive migration: index ``created_at`` by instant, not by text.

        ``created_at`` is TEXT, and rows written by earlier builds carry
        whatever offset (or none) the trace came with, so comparing and
        ordering the text is wrong across offsets. A virtual generated column
        derives the instant from that text on read, which covers the old rows
        without touching one: no stored value is rewritten, and an older build
        that opens the file afterwards still reads and writes it (its
        ``INSERT`` names its columns). Idempotent.
        """
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_xinfo(traces)")
        }
        if "created_at_utc" not in columns:
            logger.info("migrating_trace_schema_add_created_at_utc")
            try:
                self._conn.execute(
                    "ALTER TABLE traces ADD COLUMN created_at_utc TEXT "
                    f"GENERATED ALWAYS AS ({_CREATED_AT_UTC_SQL}) VIRTUAL"
                )
            except sqlite3.OperationalError:
                # A concurrent opener may have added it first.
                now = {
                    row["name"]
                    for row in self._conn.execute("PRAGMA table_xinfo(traces)")
                }
                if "created_at_utc" not in now:
                    raise
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_traces_created_at_utc "
            "ON traces(created_at_utc)"
        )
        self._conn.commit()

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
                        # The column is the instant in UTC; ``trace_json``
                        # keeps the trace exactly as it was given.
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
        if since is not None:
            conditions.append("created_at_utc >= ?")
            params.append(_instant_key(since))
        if until is not None:
            conditions.append("created_at_utc <= ?")
            params.append(_instant_key(until))

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        params.append(limit)

        sql = (
            f"SELECT trace_json FROM traces {where} "
            "ORDER BY created_at_utc DESC LIMIT ?"
        )
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
