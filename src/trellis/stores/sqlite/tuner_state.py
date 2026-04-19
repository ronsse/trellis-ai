"""SQLiteTunerStateStore — working state for tuners."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.schemas.parameters import ParameterProposal, ParameterScope
from trellis.stores.base.tuner_state import TunerStateStore
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


_CREATE_PROPOSALS = """\
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    tuner TEXT NOT NULL,
    status TEXT NOT NULL,
    component_id TEXT NOT NULL,
    domain TEXT,
    intent_family TEXT,
    tool_name TEXT,
    proposed_values_json TEXT NOT NULL DEFAULT '{}',
    baseline_version TEXT,
    sample_size INTEGER NOT NULL DEFAULT 0,
    effect_size REAL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    schema_version TEXT
)"""

_CREATE_CURSORS = """\
CREATE TABLE IF NOT EXISTS tuner_cursors (
    tuner TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_proposals_tuner ON proposals(tuner)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals(created_at)",
]


class SQLiteTunerStateStore(SQLiteStoreBase, TunerStateStore):
    """SQLite-backed working-state store for tuners."""

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(_CREATE_PROPOSALS)
        cur.execute(_CREATE_CURSORS)
        for idx_sql in _CREATE_INDEXES:
            cur.execute(idx_sql)
        self._conn.commit()

    # -- proposals -----------------------------------------------------------

    def put_proposal(self, proposal: ParameterProposal) -> ParameterProposal:
        now = utc_now().isoformat()
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO proposals ("
            "proposal_id, tuner, status, component_id, domain, intent_family, "
            "tool_name, proposed_values_json, baseline_version, sample_size, "
            "effect_size, notes, created_at, updated_at, metadata_json, schema_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal.proposal_id,
                proposal.tuner,
                proposal.status,
                proposal.scope.component_id,
                proposal.scope.domain,
                proposal.scope.intent_family,
                proposal.scope.tool_name,
                json.dumps(proposal.proposed_values),
                proposal.baseline_version,
                proposal.sample_size,
                proposal.effect_size,
                proposal.notes,
                proposal.created_at.isoformat(),
                now,
                json.dumps(proposal.metadata),
                proposal.schema_version,
            ),
        )
        self._conn.commit()
        logger.info(
            "tuner_proposal.stored",
            proposal_id=proposal.proposal_id,
            tuner=proposal.tuner,
            status=proposal.status,
        )
        return proposal

    def get_proposal(self, proposal_id: str) -> ParameterProposal | None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,))
        row = cur.fetchone()
        return self._row_to_proposal(row) if row else None

    def list_proposals(
        self,
        *,
        tuner: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ParameterProposal]:
        clauses: list[str] = []
        params: list[Any] = []
        if tuner is not None:
            clauses.append("tuner = ?")
            params.append(tuner)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = (
            f"SELECT * FROM proposals WHERE {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(limit)
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return [self._row_to_proposal(row) for row in cur.fetchall()]

    def update_status(
        self,
        proposal_id: str,
        status: str,
        *,
        notes: str | None = None,
    ) -> ParameterProposal | None:
        existing = self.get_proposal(proposal_id)
        if existing is None:
            return None
        now = utc_now().isoformat()
        cur = self._conn.cursor()
        if notes is not None:
            cur.execute(
                "UPDATE proposals SET status = ?, notes = ?, updated_at = ? "
                "WHERE proposal_id = ?",
                (status, notes, now, proposal_id),
            )
        else:
            cur.execute(
                "UPDATE proposals SET status = ?, updated_at = ? "
                "WHERE proposal_id = ?",
                (status, now, proposal_id),
            )
        self._conn.commit()
        return self.get_proposal(proposal_id)

    # -- cursors -------------------------------------------------------------

    def get_cursor(self, tuner: str) -> str | None:
        cur = self._conn.cursor()
        cur.execute("SELECT cursor FROM tuner_cursors WHERE tuner = ?", (tuner,))
        row = cur.fetchone()
        return str(row["cursor"]) if row else None

    def set_cursor(self, tuner: str, cursor: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO tuner_cursors (tuner, cursor, updated_at) "
            "VALUES (?, ?, ?)",
            (tuner, cursor, utc_now().isoformat()),
        )
        self._conn.commit()

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> ParameterProposal:
        scope = ParameterScope(
            component_id=row["component_id"],
            domain=row["domain"],
            intent_family=row["intent_family"],
            tool_name=row["tool_name"],
        )
        return ParameterProposal(
            proposal_id=row["proposal_id"],
            scope=scope,
            proposed_values=json.loads(row["proposed_values_json"]),
            baseline_version=row["baseline_version"],
            tuner=row["tuner"],
            created_at=datetime.fromisoformat(row["created_at"]),
            sample_size=row["sample_size"],
            effect_size=row["effect_size"],
            status=row["status"],
            notes=row["notes"],
            metadata=json.loads(row["metadata_json"]),
            schema_version=row["schema_version"],
        )
