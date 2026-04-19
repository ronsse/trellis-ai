"""SQLiteParameterStore — versioned parameter snapshots."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import structlog

from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.base.parameter import ParameterStore
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS parameter_snapshots (
    params_version TEXT PRIMARY KEY,
    component_id TEXT NOT NULL,
    domain TEXT,
    intent_family TEXT,
    tool_name TEXT,
    values_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    notes TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    schema_version TEXT
)"""

_CREATE_INDEXES = [
    (
        "CREATE INDEX IF NOT EXISTS idx_params_scope ON parameter_snapshots("
        "component_id, domain, intent_family, tool_name, created_at)"
    ),
    "CREATE INDEX IF NOT EXISTS idx_params_created ON parameter_snapshots(created_at)",
]


class SQLiteParameterStore(SQLiteStoreBase, ParameterStore):
    """SQLite-backed versioned :class:`ParameterSet` store.

    Snapshots are immutable; ``put`` always creates a new row.  Active
    snapshot = the most recently created row for a given scope key.
    """

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            cur.execute(idx_sql)
        self._conn.commit()

    # -- mutations -----------------------------------------------------------

    def put(self, params: ParameterSet) -> ParameterSet:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO parameter_snapshots ("
            "params_version, component_id, domain, intent_family, tool_name, "
            "values_json, source, created_at, notes, metadata_json, schema_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                params.params_version,
                params.scope.component_id,
                params.scope.domain,
                params.scope.intent_family,
                params.scope.tool_name,
                json.dumps(params.values),
                params.source,
                params.created_at.isoformat(),
                params.notes,
                json.dumps(params.metadata),
                params.schema_version,
            ),
        )
        self._conn.commit()
        logger.info(
            "parameters.stored",
            params_version=params.params_version,
            component_id=params.scope.component_id,
        )
        return params

    # -- queries -------------------------------------------------------------

    def get(self, params_version: str) -> ParameterSet | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM parameter_snapshots WHERE params_version = ?",
            (params_version,),
        )
        row = cur.fetchone()
        return self._row_to_params(row) if row else None

    def get_active(self, scope: ParameterScope) -> ParameterSet | None:
        clauses, params = self._exact_scope_clauses(scope)
        where = " AND ".join(clauses)
        sql = (
            f"SELECT * FROM parameter_snapshots WHERE {where} "
            "ORDER BY created_at DESC LIMIT 1"
        )
        cur = self._conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return self._row_to_params(row) if row else None

    def resolve(self, scope: ParameterScope) -> ParameterSet | None:
        # Precedence chain: narrowest first.
        candidates: list[ParameterScope] = [
            scope,
            ParameterScope(
                component_id=scope.component_id,
                domain=scope.domain,
                intent_family=scope.intent_family,
            ),
            ParameterScope(
                component_id=scope.component_id,
                domain=scope.domain,
            ),
            ParameterScope(
                component_id=scope.component_id,
                intent_family=scope.intent_family,
            ),
            ParameterScope(component_id=scope.component_id),
        ]
        seen: set[tuple[str, str | None, str | None, str | None]] = set()
        for cand in candidates:
            key = cand.key()
            if key in seen:
                continue
            seen.add(key)
            active = self.get_active(cand)
            if active is not None:
                return active
        return None

    def list_versions(
        self,
        scope: ParameterScope | None = None,
        *,
        limit: int = 100,
    ) -> list[ParameterSet]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses, params = self._exact_scope_clauses(scope)
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = (
            f"SELECT * FROM parameter_snapshots WHERE {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(limit)
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return [self._row_to_params(row) for row in cur.fetchall()]

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _exact_scope_clauses(
        scope: ParameterScope,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = ["component_id = ?"]
        params: list[Any] = [scope.component_id]
        for col, val in (
            ("domain", scope.domain),
            ("intent_family", scope.intent_family),
            ("tool_name", scope.tool_name),
        ):
            if val is None:
                clauses.append(f"{col} IS NULL")
            else:
                clauses.append(f"{col} = ?")
                params.append(val)
        return clauses, params

    @staticmethod
    def _row_to_params(row: sqlite3.Row) -> ParameterSet:
        from datetime import datetime  # noqa: PLC0415

        scope = ParameterScope(
            component_id=row["component_id"],
            domain=row["domain"],
            intent_family=row["intent_family"],
            tool_name=row["tool_name"],
        )
        return ParameterSet(
            params_version=row["params_version"],
            scope=scope,
            values=json.loads(row["values_json"]),
            source=row["source"],
            created_at=datetime.fromisoformat(row["created_at"]),
            notes=row["notes"],
            metadata=json.loads(row["metadata_json"]),
            schema_version=row["schema_version"],
        )
