"""SQLiteOutcomeStore — SQLite-backed OutcomeEvent store."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import structlog

from trellis.schemas.outcome import ComponentOutcome, OutcomeEvent
from trellis.stores.base.outcome import OutcomeStore
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS outcomes (
    event_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL,
    params_version TEXT,
    domain TEXT,
    intent_family TEXT,
    tool_name TEXT,
    phase TEXT,
    agent_role TEXT,
    agent_id TEXT,
    run_id TEXT,
    session_id TEXT,
    pack_id TEXT,
    trace_id TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    items_served INTEGER,
    items_referenced INTEGER,
    error TEXT,
    cohort TEXT,
    segment TEXT,
    outcome_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    schema_version TEXT
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_outcomes_component ON outcomes(component_id)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_occurred ON outcomes(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_domain ON outcomes(domain)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_intent ON outcomes(intent_family)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_tool ON outcomes(tool_name)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_phase ON outcomes(phase)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_params ON outcomes(params_version)",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_run ON outcomes(run_id)",
    (
        "CREATE INDEX IF NOT EXISTS idx_outcomes_scope ON outcomes("
        "component_id, domain, intent_family, tool_name)"
    ),
]


class SQLiteOutcomeStore(SQLiteStoreBase, OutcomeStore):
    """SQLite-backed :class:`OutcomeEvent` store.

    Note: Uses ``check_same_thread=False`` for async compatibility but
    provides no internal locking.  Callers must synchronise access when
    sharing a single instance across threads.
    """

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            cur.execute(idx_sql)
        self._conn.commit()

    # -- mutations -----------------------------------------------------------

    def append(self, outcome: OutcomeEvent) -> None:
        cur = self._conn.cursor()
        cur.execute(*self._insert(outcome))
        self._conn.commit()
        logger.debug(
            "outcome.appended",
            event_id=outcome.event_id,
            component_id=outcome.component_id,
        )

    def append_many(self, outcomes: list[OutcomeEvent]) -> int:
        if not outcomes:
            return 0
        cur = self._conn.cursor()
        for o in outcomes:
            cur.execute(*self._insert(o))
        self._conn.commit()
        logger.debug("outcomes.appended_many", count=len(outcomes))
        return len(outcomes)

    @staticmethod
    def _insert(o: OutcomeEvent) -> tuple[str, tuple[Any, ...]]:
        sql = (
            "INSERT INTO outcomes ("
            "event_id, component_id, params_version, domain, intent_family, "
            "tool_name, phase, agent_role, agent_id, run_id, session_id, "
            "pack_id, trace_id, occurred_at, recorded_at, success, latency_ms, "
            "items_served, items_referenced, error, cohort, segment, "
            "outcome_json, metadata_json, schema_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)"
        )
        params: tuple[Any, ...] = (
            o.event_id,
            o.component_id,
            o.params_version,
            o.domain,
            o.intent_family,
            o.tool_name,
            o.phase,
            o.agent_role,
            o.agent_id,
            o.run_id,
            o.session_id,
            o.pack_id,
            o.trace_id,
            o.occurred_at.isoformat(),
            o.recorded_at.isoformat(),
            1 if o.outcome.success else 0,
            o.outcome.latency_ms,
            o.outcome.items_served,
            o.outcome.items_referenced,
            o.outcome.error,
            o.cohort,
            o.segment,
            json.dumps(o.outcome.model_dump(mode="json")),
            json.dumps(o.metadata),
            o.schema_version,
        )
        return sql, params

    # -- queries -------------------------------------------------------------

    def query(
        self,
        *,
        component_id: str | None = None,
        domain: str | None = None,
        intent_family: str | None = None,
        tool_name: str | None = None,
        phase: str | None = None,
        agent_role: str | None = None,
        params_version: str | None = None,
        run_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[OutcomeEvent]:
        clauses, params = self._build_filters(
            component_id=component_id,
            domain=domain,
            intent_family=intent_family,
            tool_name=tool_name,
            phase=phase,
            agent_role=agent_role,
            params_version=params_version,
            run_id=run_id,
            since=since,
            until=until,
        )
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = (
            f"SELECT * FROM outcomes WHERE {where} ORDER BY occurred_at ASC LIMIT ?"
        )
        params.append(limit)

        cur = self._conn.cursor()
        cur.execute(sql, params)
        return [self._row_to_outcome(row) for row in cur.fetchall()]

    def count(
        self,
        *,
        component_id: str | None = None,
        domain: str | None = None,
        intent_family: str | None = None,
        tool_name: str | None = None,
        phase: str | None = None,
        params_version: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        clauses, params = self._build_filters(
            component_id=component_id,
            domain=domain,
            intent_family=intent_family,
            tool_name=tool_name,
            phase=phase,
            params_version=params_version,
            since=since,
            until=until,
        )
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"SELECT COUNT(*) FROM outcomes WHERE {where}"

        cur = self._conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _build_filters(
        *,
        component_id: str | None = None,
        domain: str | None = None,
        intent_family: str | None = None,
        tool_name: str | None = None,
        phase: str | None = None,
        agent_role: str | None = None,
        params_version: str | None = None,
        run_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if component_id is not None:
            clauses.append("component_id = ?")
            params.append(component_id)
        if domain is not None:
            clauses.append("domain = ?")
            params.append(domain)
        if intent_family is not None:
            clauses.append("intent_family = ?")
            params.append(intent_family)
        if tool_name is not None:
            clauses.append("tool_name = ?")
            params.append(tool_name)
        if phase is not None:
            clauses.append("phase = ?")
            params.append(phase)
        if agent_role is not None:
            clauses.append("agent_role = ?")
            params.append(agent_role)
        if params_version is not None:
            clauses.append("params_version = ?")
            params.append(params_version)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("occurred_at <= ?")
            params.append(until.isoformat())
        return clauses, params

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_outcome(row: sqlite3.Row) -> OutcomeEvent:
        outcome_payload = json.loads(row["outcome_json"])
        return OutcomeEvent(
            event_id=row["event_id"],
            component_id=row["component_id"],
            params_version=row["params_version"],
            domain=row["domain"],
            intent_family=row["intent_family"],
            tool_name=row["tool_name"],
            phase=row["phase"],
            agent_role=row["agent_role"],
            agent_id=row["agent_id"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            pack_id=row["pack_id"],
            trace_id=row["trace_id"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            outcome=ComponentOutcome.model_validate(outcome_payload),
            cohort=row["cohort"],
            segment=row["segment"],
            metadata=json.loads(row["metadata_json"]),
            schema_version=row["schema_version"],
        )
