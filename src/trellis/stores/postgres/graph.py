"""PostgresGraphStore — Postgres-backed graph store with SCD Type 2 versioning."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.core.ids import generate_ulid
from trellis.schemas.graph import CompactionReport
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.graph import (
    GraphStore,
    check_node_role_immutable,
    validate_document_ids,
    validate_node_role_args,
)
from trellis.stores.postgres.base import PostgresStoreBase

logger = structlog.get_logger(__name__)

_CREATE_NODES = """\
CREATE TABLE IF NOT EXISTS nodes (
    version_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    node_role TEXT NOT NULL DEFAULT 'semantic',
    generation_spec JSONB DEFAULT NULL,
    document_ids JSONB DEFAULT NULL,
    properties JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ DEFAULT NULL
)"""

# Additive migrations run after CREATE TABLE so upgrades from older
# schema versions pick up new columns without a rebuild.
_MIGRATE_ADD_NODE_ROLE = [
    # v2 → v3
    (
        "ALTER TABLE nodes "
        "ADD COLUMN IF NOT EXISTS node_role TEXT NOT NULL DEFAULT 'semantic'"
    ),
    "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS generation_spec JSONB DEFAULT NULL",
    # v3 → v4: document_ids (Phase 4 of ADR planes-and-substrates).
    # Pre-existing rows stay NULL, which the read path surfaces as [].
    "ALTER TABLE nodes ADD COLUMN IF NOT EXISTS document_ids JSONB DEFAULT NULL",
]

_CREATE_EDGES = """\
CREATE TABLE IF NOT EXISTS edges (
    version_id TEXT PRIMARY KEY,
    edge_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    properties JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ DEFAULT NULL
)"""

_CREATE_ENTITY_ALIASES = """\
CREATE TABLE IF NOT EXISTS entity_aliases (
    version_id TEXT PRIMARY KEY,
    alias_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    raw_id TEXT NOT NULL,
    raw_name TEXT DEFAULT NULL,
    match_confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ DEFAULT NULL
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_nodes_node_id ON nodes(node_id)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_role ON nodes(node_role)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_valid ON nodes(valid_from, valid_to)",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_current"
        " ON nodes(node_id) WHERE valid_to IS NULL"
    ),
    "CREATE INDEX IF NOT EXISTS idx_edges_edge_id ON edges(edge_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type)",
    ("CREATE INDEX IF NOT EXISTS idx_edges_valid ON edges(valid_from, valid_to)"),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_current"
        " ON edges(edge_id) WHERE valid_to IS NULL"
    ),
    "CREATE INDEX IF NOT EXISTS idx_aliases_alias_id ON entity_aliases(alias_id)",
    "CREATE INDEX IF NOT EXISTS idx_aliases_entity_id ON entity_aliases(entity_id)",
    (
        "CREATE INDEX IF NOT EXISTS idx_aliases_source_raw"
        " ON entity_aliases(source_system, raw_id)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_aliases_valid"
        " ON entity_aliases(valid_from, valid_to)"
    ),
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_current"
        " ON entity_aliases(source_system, raw_id) WHERE valid_to IS NULL"
    ),
]


class PostgresGraphStore(PostgresStoreBase, GraphStore):
    """Postgres-backed graph store with SCD Type 2 temporal versioning."""

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_CREATE_NODES)
            # Additive v3 migration: pick up node_role / generation_spec on
            # databases that were created against an older schema.
            for alter_sql in _MIGRATE_ADD_NODE_ROLE:
                cur.execute(alter_sql)
            cur.execute(_CREATE_EDGES)
            cur.execute(_CREATE_ENTITY_ALIASES)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Temporal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _temporal_filter(as_of: datetime | None, table_alias: str = "") -> str:
        prefix = f"{table_alias}." if table_alias else ""
        if as_of is None:
            return f"{prefix}valid_to IS NULL"
        return (
            f"{prefix}valid_from <= %s AND "
            f"({prefix}valid_to IS NULL OR {prefix}valid_to > %s)"
        )

    @staticmethod
    def _temporal_params(as_of: datetime | None) -> list[datetime]:
        if as_of is None:
            return []
        return [as_of, as_of]

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def upsert_node(
        self,
        node_id: str | None,
        node_type: str,
        properties: dict[str, Any],
        *,
        node_role: str = "semantic",
        generation_spec: dict[str, Any] | None = None,
        document_ids: list[str] | None = None,
        commit: bool = True,  # noqa: ARG002
    ) -> str:
        validate_node_role_args(node_role, generation_spec)
        validate_document_ids(document_ids)

        if node_id is None:
            node_id = generate_ulid()

        now = utc_now()
        properties_json = json.dumps(properties)
        generation_spec_json = (
            json.dumps(generation_spec) if generation_spec is not None else None
        )
        document_ids_json = (
            json.dumps(document_ids) if document_ids is not None else None
        )

        existing = self.get_node(node_id)
        if existing:
            check_node_role_immutable(node_id, existing, node_role)
            with self.conn.cursor() as cur:
                # Close current version
                cur.execute(
                    "UPDATE nodes SET valid_to = %s"
                    " WHERE node_id = %s AND valid_to IS NULL",
                    (now, node_id),
                )
                # Insert new version
                version_id = generate_ulid()
                cur.execute(
                    """
                    INSERT INTO nodes
                        (version_id, node_id, node_type, node_role,
                         generation_spec, document_ids, properties,
                         created_at, updated_at, valid_from, valid_to)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        version_id,
                        node_id,
                        node_type,
                        node_role,
                        generation_spec_json,
                        document_ids_json,
                        properties_json,
                        existing["created_at"],
                        now,
                        now,
                    ),
                )
        else:
            version_id = generate_ulid()
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO nodes
                        (version_id, node_id, node_type, node_role,
                         generation_spec, document_ids, properties,
                         created_at, updated_at, valid_from, valid_to)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        version_id,
                        node_id,
                        node_type,
                        node_role,
                        generation_spec_json,
                        document_ids_json,
                        properties_json,
                        now,
                        now,
                        now,
                    ),
                )

        self.conn.commit()
        return node_id

    def upsert_nodes_bulk(self, nodes: list[dict[str, Any]]) -> list[str]:
        # Network round trips per call but no UNWIND-style batching —
        # a simple loop over ``upsert_node`` is the pass-through impl.
        # Neo4j gets its own UNWIND override.
        #
        # Run every per-row validator up-front so the ABC's
        # validation-atomicity contract holds: invalid input is rejected
        # before any row is written. Mid-batch IO failures during the
        # subsequent loop can still leave a partial commit (per-row
        # auto-commit on the underlying psycopg connection).
        self._pre_validate_nodes_bulk(nodes)
        return [
            self.upsert_node(
                node_id=spec.get("node_id"),
                node_type=spec["node_type"],
                properties=spec.get("properties") or {},
                node_role=spec.get("node_role", "semantic"),
                generation_spec=spec.get("generation_spec"),
                document_ids=spec.get("document_ids"),
            )
            for spec in nodes
        ]

    def get_node(
        self,
        node_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        temporal = self._temporal_filter(as_of)
        params: list[Any] = [node_id, *self._temporal_params(as_of)]
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT version_id, node_id, node_type, node_role,
                       generation_spec, document_ids, properties,
                       created_at, updated_at, valid_from, valid_to
                FROM nodes
                WHERE node_id = %s AND {temporal}
                """,
                params,
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._node_row_to_dict(row)

    def get_nodes_bulk(
        self,
        node_ids: list[str],
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        # Use ANY(%s) for list-based IN queries
        temporal = self._temporal_filter(as_of)
        params: list[Any] = [node_ids, *self._temporal_params(as_of)]
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT version_id, node_id, node_type, node_role,
                       generation_spec, document_ids, properties,
                       created_at, updated_at, valid_from, valid_to
                FROM nodes
                WHERE node_id = ANY(%s) AND {temporal}
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._node_row_to_dict(row) for row in rows]

    def get_node_history(self, node_id: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT version_id, node_id, node_type, node_role,
                       generation_spec, document_ids, properties,
                       created_at, updated_at, valid_from, valid_to
                FROM nodes
                WHERE node_id = %s
                ORDER BY valid_from DESC
                """,
                (node_id,),
            )
            rows = cur.fetchall()
        return [self._node_row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Aliases
    # ------------------------------------------------------------------

    def upsert_alias(
        self,
        entity_id: str,
        source_system: str,
        raw_id: str,
        *,
        raw_name: str | None = None,
        match_confidence: float = 1.0,
        is_primary: bool = False,
    ) -> str:
        now = utc_now()

        existing = self.resolve_alias(source_system, raw_id)
        if existing:
            alias_id = existing["alias_id"]
            created_at = existing["created_at"]
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE entity_aliases SET valid_to = %s"
                    " WHERE alias_id = %s AND valid_to IS NULL",
                    (now, alias_id),
                )
        else:
            alias_id = generate_ulid()
            created_at = now

        version_id = generate_ulid()
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entity_aliases
                    (version_id, alias_id, entity_id, source_system,
                     raw_id, raw_name, match_confidence, is_primary,
                     created_at, updated_at, valid_from, valid_to)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                """,
                (
                    version_id,
                    alias_id,
                    entity_id,
                    source_system,
                    raw_id,
                    raw_name,
                    match_confidence,
                    is_primary,
                    created_at,
                    now,
                    now,
                ),
            )
        self.conn.commit()
        return str(alias_id)

    def resolve_alias(
        self,
        source_system: str,
        raw_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        temporal = self._temporal_filter(as_of)
        params: list[Any] = [source_system, raw_id, *self._temporal_params(as_of)]
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT version_id, alias_id, entity_id,
                       source_system, raw_id, raw_name,
                       match_confidence, is_primary,
                       created_at, updated_at, valid_from, valid_to
                FROM entity_aliases
                WHERE source_system = %s AND raw_id = %s AND {temporal}
                """,
                params,
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._alias_row_to_dict(row)

    def get_aliases(
        self,
        entity_id: str,
        source_system: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["entity_id = %s"]
        params: list[Any] = [entity_id]
        if source_system:
            conditions.append("source_system = %s")
            params.append(source_system)
        conditions.append(self._temporal_filter(as_of))
        params.extend(self._temporal_params(as_of))
        where_clause = " AND ".join(conditions)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT version_id, alias_id, entity_id,
                       source_system, raw_id, raw_name,
                       match_confidence, is_primary,
                       created_at, updated_at, valid_from, valid_to
                FROM entity_aliases
                WHERE {where_clause}
                ORDER BY source_system, raw_id
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._alias_row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------

    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        properties: dict[str, Any] | None = None,
        *,
        commit: bool = True,  # noqa: ARG002
    ) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT edge_id FROM edges
                WHERE source_id = %s AND target_id = %s AND edge_type = %s
                  AND valid_to IS NULL
                """,
                (source_id, target_id, edge_type),
            )
            row = cur.fetchone()

        now = utc_now()
        properties_json = json.dumps(properties or {})

        if row:
            edge_id: str = row[0]
            with self.conn.cursor() as cur:
                # Close current version
                cur.execute(
                    "UPDATE edges SET valid_to = %s"
                    " WHERE edge_id = %s AND valid_to IS NULL",
                    (now, edge_id),
                )
                # Insert new version
                version_id = generate_ulid()
                cur.execute(
                    """
                    INSERT INTO edges
                        (version_id, edge_id, source_id, target_id, edge_type,
                         properties, created_at, valid_from, valid_to)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        version_id,
                        edge_id,
                        source_id,
                        target_id,
                        edge_type,
                        properties_json,
                        now,
                        now,
                    ),
                )
        else:
            edge_id = generate_ulid()
            version_id = generate_ulid()
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO edges
                        (version_id, edge_id, source_id, target_id, edge_type,
                         properties, created_at, valid_from, valid_to)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        version_id,
                        edge_id,
                        source_id,
                        target_id,
                        edge_type,
                        properties_json,
                        now,
                        now,
                    ),
                )

        self.conn.commit()
        return edge_id

    def upsert_edges_bulk(self, edges: list[dict[str, Any]]) -> list[str]:
        # Pre-validate keys + endpoint existence so the bulk contract is
        # consistent with Neo4j's, even though the single-row Postgres
        # ``upsert_edge`` is permissive about dangling endpoints.
        self._validate_bulk_required_keys(
            edges, ("source_id", "target_id", "edge_type"), "upsert_edges_bulk"
        )
        self._pre_validate_edges_bulk(edges)
        unique_endpoints = {spec["source_id"] for spec in edges} | {
            spec["target_id"] for spec in edges
        }
        existing = {
            row["node_id"] for row in self.get_nodes_bulk(list(unique_endpoints))
        }
        for i, spec in enumerate(edges):
            if spec["source_id"] not in existing:
                msg = (
                    f"upsert_edges_bulk[{i}]: source "
                    f"{spec['source_id']!r} has no current version"
                )
                raise ValueError(msg)
            if spec["target_id"] not in existing:
                msg = (
                    f"upsert_edges_bulk[{i}]: target "
                    f"{spec['target_id']!r} has no current version"
                )
                raise ValueError(msg)
        return [
            self.upsert_edge(
                source_id=spec["source_id"],
                target_id=spec["target_id"],
                edge_type=spec["edge_type"],
                properties=spec.get("properties"),
            )
            for spec in edges
        ]

    def get_edges(
        self,
        node_id: str,
        direction: str = "both",
        edge_type: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []

        if direction in ("outgoing", "both"):
            conditions.append("source_id = %s")
            params.append(node_id)
        if direction in ("incoming", "both"):
            conditions.append("target_id = %s")
            params.append(node_id)

        where_clause = " OR ".join(conditions)

        if edge_type:
            where_clause = f"({where_clause}) AND edge_type = %s"
            params.append(edge_type)

        temporal = self._temporal_filter(as_of)
        where_clause = f"({where_clause}) AND {temporal}"
        params.extend(self._temporal_params(as_of))

        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT version_id, edge_id, source_id, target_id, edge_type,
                       properties, created_at, valid_from, valid_to
                FROM edges WHERE {where_clause}
                """,
                params,
            )
            rows = cur.fetchall()
        return [self._edge_row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Subgraph (recursive CTE)
    # ------------------------------------------------------------------

    def get_subgraph(
        self,
        seed_ids: list[str],
        depth: int = 2,
        edge_types: list[str] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        if not seed_ids:
            return {"nodes": [], "edges": []}

        node_temporal = self._temporal_filter(as_of, "n")
        node_temporal_params = self._temporal_params(as_of)
        edge_temporal = self._temporal_filter(as_of, "e")
        edge_temporal_params = self._temporal_params(as_of)

        # Edge type filter
        edge_filter = ""
        edge_params: list[Any] = []
        if edge_types:
            edge_filter = "AND e.edge_type = ANY(%s)"
            edge_params = [edge_types]

        query = f"""
        WITH RECURSIVE traversal(node_id, depth) AS (
            SELECT n.node_id, 0 FROM nodes n
            WHERE n.node_id = ANY(%s)
              AND {node_temporal}

            UNION

            SELECT
                CASE
                    WHEN e.source_id = t.node_id THEN e.target_id
                    ELSE e.source_id
                END AS node_id,
                t.depth + 1
            FROM traversal t
            JOIN edges e ON (e.source_id = t.node_id OR e.target_id = t.node_id)
                AND {edge_temporal}
                {edge_filter}
            WHERE t.depth < %s
        ),
        unique_nodes AS (
            SELECT node_id, MIN(depth) AS min_depth
            FROM traversal
            GROUP BY node_id
        )
        SELECT
            n.version_id,
            n.node_id,
            n.node_type,
            n.node_role,
            n.generation_spec,
            n.properties,
            n.created_at,
            n.updated_at,
            n.valid_from,
            n.valid_to,
            un.min_depth
        FROM unique_nodes un
        JOIN nodes n ON n.node_id = un.node_id AND {node_temporal}
        ORDER BY un.min_depth, n.node_id
        """

        params: list[Any] = [
            seed_ids,
            *node_temporal_params,  # base case temporal
            *edge_temporal_params,  # recursive case temporal
            *edge_params,
            depth,
            *node_temporal_params,  # final JOIN temporal
        ]
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            node_rows = cur.fetchall()

        collected_nodes: list[dict[str, Any]] = []
        node_id_set: set[str] = set()
        for row in node_rows:
            node_id_set.add(row[1])  # node_id is index 1
            collected_nodes.append(self._node_row_to_dict(row))

        # Fetch edges between collected nodes
        collected_edges: list[dict[str, Any]] = []
        if node_id_set:
            node_list = list(node_id_set)
            edge_temporal_frag = self._temporal_filter(as_of)

            edge_query = f"""
            SELECT version_id, edge_id, source_id, target_id, edge_type,
                   properties, created_at, valid_from, valid_to
            FROM edges
            WHERE source_id = ANY(%s)
              AND target_id = ANY(%s)
              AND {edge_temporal_frag}
            """
            eq_params: list[Any] = [node_list, node_list, *self._temporal_params(as_of)]
            if edge_types:
                edge_query += " AND edge_type = ANY(%s)"
                eq_params.append(edge_types)

            with self.conn.cursor() as cur:
                cur.execute(edge_query, eq_params)
                edge_rows = cur.fetchall()
            collected_edges = [self._edge_row_to_dict(row) for row in edge_rows]

        logger.debug(
            "subgraph_fetched",
            seed_count=len(seed_ids),
            depth=depth,
            nodes_found=len(collected_nodes),
            edges_found=len(collected_edges),
        )

        return {"nodes": collected_nodes, "edges": collected_edges}

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        node_type: str | None = None,
        properties: dict[str, Any] | None = None,
        limit: int = 50,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        temporal = self._temporal_filter(as_of)
        conditions = [temporal]
        params: list[Any] = list(self._temporal_params(as_of))

        if node_type:
            conditions.append("node_type = %s")
            params.append(node_type)

        complex_filters: dict[str, Any] = {}
        if properties:
            for key, value in properties.items():
                if isinstance(value, str | int | float | bool):
                    conditions.append("properties->>%s = %s")
                    params.extend([key, str(value)])
                elif value is None:
                    conditions.append("properties->>%s IS NULL")
                    params.append(key)
                else:
                    complex_filters[key] = value

        where_clause = " AND ".join(conditions)
        params.append(limit)

        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT version_id, node_id, node_type, node_role,
                       generation_spec, document_ids, properties,
                       created_at, updated_at, valid_from, valid_to
                FROM nodes
                WHERE {where_clause}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            node = self._node_row_to_dict(row)
            if complex_filters:
                props = node["properties"]
                if not all(props.get(k) == v for k, v in complex_filters.items()):
                    continue
            results.append(node)
        return results

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_node(self, node_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edges WHERE source_id = %s OR target_id = %s",
                (node_id, node_id),
            )
            cur.execute("DELETE FROM entity_aliases WHERE entity_id = %s", (node_id,))
            cur.execute("DELETE FROM nodes WHERE node_id = %s", (node_id,))
            deleted = bool(cur.rowcount > 0)
        self.conn.commit()
        if deleted:
            logger.debug("node_deleted", node_id=node_id)
        return deleted

    def delete_edge(self, edge_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM edges WHERE edge_id = %s", (edge_id,))
            deleted = bool(cur.rowcount > 0)
        self.conn.commit()
        if deleted:
            logger.debug("edge_deleted", edge_id=edge_id)
        return deleted

    # ------------------------------------------------------------------
    # Counts
    # ------------------------------------------------------------------

    def count_nodes(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM nodes WHERE valid_to IS NULL")
            row = cur.fetchone()
        assert row is not None
        return int(row[0])

    def count_edges(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM edges WHERE valid_to IS NULL")
            row = cur.fetchone()
        assert row is not None
        return int(row[0])

    # ------------------------------------------------------------------
    # Canonical DSL — Phase 2 compiler
    # ------------------------------------------------------------------

    def execute_node_query(self, query: Any) -> list[dict[str, Any]]:
        """Compile :class:`NodeQuery` to a Postgres SELECT.

        Supports the full Phase 1 operator surface (``eq`` / ``in`` /
        ``exists``) on:

        * ``node_type`` / ``node_role`` (column comparison)
        * ``properties.<key>`` (via JSONB ``->>`` path extractor)
        """
        sql, params = self._compile_node_query(query)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [self._node_row_to_dict(row) for row in rows]

    def _compile_node_query(self, query: Any) -> tuple[str, list[Any]]:
        """Pure compile step — returns (sql, params). No I/O."""
        where_parts: list[str] = [self._temporal_filter(query.as_of)]
        params: list[Any] = list(self._temporal_params(query.as_of))
        for clause in query.filters:
            frag, p = self._compile_clause_postgres(clause)
            where_parts.append(frag)
            params.extend(p)
        sql = (
            "SELECT * FROM nodes WHERE "
            + " AND ".join(where_parts)
            + " ORDER BY created_at DESC LIMIT %s"
        )
        params.append(query.limit)
        return sql, params

    @staticmethod
    def _compile_clause_postgres(clause: Any) -> tuple[str, list[Any]]:
        """Translate one :class:`FilterClause` to a Postgres WHERE fragment.

        Top-level columns (``node_type`` / ``node_role`` / ``node_id``)
        compile to direct ``=`` / ``IN`` / ``IS NOT NULL`` predicates.

        ``properties.<key>`` paths use the JSONB containment operator
        (``@>``) for ``eq`` / ``in`` so the comparison is *type-aware*
        — ``properties->>'tier' = '1'`` would compare the TEXT
        rendering, which silently reinterprets ints, floats and bools.
        Containment matches the full JSON value at the path.
        ``exists`` uses the JSONB key-existence operator (``?``).
        """
        if clause.field.startswith("properties."):
            return PostgresGraphStore._compile_properties_clause(clause)
        column = PostgresGraphStore._field_to_column(clause.field)
        if clause.op == "eq":
            return f"{column} = %s", [clause.value]
        if clause.op == "in":
            placeholders = ", ".join("%s" for _ in clause.value)
            return f"{column} IN ({placeholders})", list(clause.value)
        if clause.op == "exists":
            return f"{column} IS NOT NULL", []
        msg = f"Unknown filter op {clause.op!r}"
        raise ValueError(msg)

    @staticmethod
    def _compile_properties_clause(clause: Any) -> tuple[str, list[Any]]:
        key = clause.field.split(".", 1)[1]
        if clause.op == "eq":
            return "properties @> %s::jsonb", [json.dumps({key: clause.value})]
        if clause.op == "in":
            ors = " OR ".join("properties @> %s::jsonb" for _ in clause.value)
            return (
                f"({ors})",
                [json.dumps({key: v}) for v in clause.value],
            )
        if clause.op == "exists":
            return "properties ? %s", [key]
        msg = f"Unknown filter op {clause.op!r}"
        raise ValueError(msg)

    @staticmethod
    def _field_to_column(field: str) -> str:
        if field in {"node_type", "node_role", "node_id"}:
            return field
        msg = f"Unsupported DSL field path: {field!r}"
        raise ValueError(msg)

    # ------------------------------------------------------------------
    # Compaction (Gap 4.2 — SCD2 retention)
    # ------------------------------------------------------------------

    _COMPACTABLE_TABLES = ("nodes", "edges", "entity_aliases")

    def compact_versions(
        self,
        before: datetime,
        *,
        dry_run: bool = False,
        event_log: EventLog | None = None,
    ) -> CompactionReport:
        start_ns = time.monotonic_ns()
        counts: dict[str, int] = {}
        range_valid_to: list[datetime] = []

        try:
            with self.conn.cursor() as cur:
                for table in self._COMPACTABLE_TABLES:
                    cur.execute(
                        f"SELECT COUNT(*) AS cnt, "
                        f"MIN(valid_to) AS oldest, MAX(valid_to) AS newest "
                        f"FROM {table} "
                        f"WHERE valid_to IS NOT NULL AND valid_to < %s",
                        (before,),
                    )
                    row = cur.fetchone()
                    count = int(row[0]) if row is not None else 0
                    counts[table] = count
                    if count > 0 and row is not None:
                        if row[1] is not None:
                            range_valid_to.append(row[1])
                        if row[2] is not None:
                            range_valid_to.append(row[2])
                    if count > 0 and not dry_run:
                        cur.execute(
                            f"DELETE FROM {table} "
                            f"WHERE valid_to IS NOT NULL AND valid_to < %s",
                            (before,),
                        )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        report = CompactionReport(
            before=before,
            nodes_compacted=counts.get("nodes", 0),
            edges_compacted=counts.get("edges", 0),
            aliases_compacted=counts.get("entity_aliases", 0),
            oldest_compacted_valid_to=min(range_valid_to) if range_valid_to else None,
            newest_compacted_valid_to=max(range_valid_to) if range_valid_to else None,
            dry_run=dry_run,
            duration_ms=max((time.monotonic_ns() - start_ns) // 1_000_000, 0),
        )
        logger.info(
            "graph_versions_compacted",
            before=before.isoformat(),
            dry_run=dry_run,
            nodes=report.nodes_compacted,
            edges=report.edges_compacted,
            aliases=report.aliases_compacted,
            duration_ms=report.duration_ms,
        )
        if event_log is not None:
            event_log.emit(
                EventType.GRAPH_VERSIONS_COMPACTED,
                source="graph_store",
                payload=report.model_dump(mode="json"),
            )
        return report

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_iso(val: Any) -> str | None:
        """Convert a datetime-like value to ISO string."""
        if val is None:
            return None
        if hasattr(val, "isoformat"):
            return str(val.isoformat())
        return str(val)

    @classmethod
    def _node_row_to_dict(cls, row: tuple[Any, ...]) -> dict[str, Any]:
        # Row layout (v4 — Phase 4 of ADR planes-and-substrates added
        # document_ids between generation_spec and properties):
        # 0 version_id, 1 node_id, 2 node_type, 3 node_role,
        # 4 generation_spec, 5 document_ids, 6 properties,
        # 7 created_at, 8 updated_at, 9 valid_from, 10 valid_to
        gen_spec_raw = row[4]
        if gen_spec_raw is None:
            generation_spec: dict[str, Any] | None = None
        elif isinstance(gen_spec_raw, str):
            generation_spec = json.loads(gen_spec_raw)
        elif isinstance(gen_spec_raw, dict):
            generation_spec = gen_spec_raw
        else:
            generation_spec = None

        doc_ids_raw = row[5]
        if doc_ids_raw is None:
            document_ids: list[str] = []
        elif isinstance(doc_ids_raw, str):
            document_ids = json.loads(doc_ids_raw) or []
        elif isinstance(doc_ids_raw, list):
            document_ids = doc_ids_raw
        else:
            document_ids = []

        props_raw = row[6]
        if isinstance(props_raw, str):
            props = json.loads(props_raw)
        elif isinstance(props_raw, dict):
            props = props_raw
        else:
            props = {}

        return {
            "node_id": row[1],
            "node_type": row[2],
            "node_role": row[3] or "semantic",
            "generation_spec": generation_spec,
            "document_ids": document_ids,
            "properties": props,
            "created_at": cls._to_iso(row[7]),
            "updated_at": cls._to_iso(row[8]),
            "valid_from": cls._to_iso(row[9]),
            "valid_to": cls._to_iso(row[10]),
        }

    @classmethod
    def _edge_row_to_dict(cls, row: tuple[Any, ...]) -> dict[str, Any]:
        props_raw = row[5]
        if isinstance(props_raw, str):
            props = json.loads(props_raw)
        elif isinstance(props_raw, dict):
            props = props_raw
        else:
            props = {}

        return {
            "edge_id": row[1],
            "source_id": row[2],
            "target_id": row[3],
            "edge_type": row[4],
            "properties": props,
            "created_at": cls._to_iso(row[6]),
            "valid_from": cls._to_iso(row[7]),
            "valid_to": cls._to_iso(row[8]),
        }

    @classmethod
    def _alias_row_to_dict(cls, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "alias_id": row[1],
            "entity_id": row[2],
            "source_system": row[3],
            "raw_id": row[4],
            "raw_name": row[5],
            "match_confidence": row[6],
            "is_primary": row[7],
            "created_at": cls._to_iso(row[8]),
            "updated_at": cls._to_iso(row[9]),
            "valid_from": cls._to_iso(row[10]),
            "valid_to": cls._to_iso(row[11]),
        }
