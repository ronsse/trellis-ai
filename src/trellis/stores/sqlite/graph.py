"""SQLiteGraphStore — SQLite-backed graph store with SCD Type 2 versioning."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


class SQLiteGraphStore(SQLiteStoreBase, GraphStore):
    """SQLite-backed graph store with recursive CTE subgraph traversal.

    Supports SCD Type 2 temporal versioning: each mutation creates a new
    version row and closes the previous one by setting ``valid_to``.

    Note: Uses ``check_same_thread=False`` for compatibility with async
    frameworks but provides no internal locking. Callers must synchronise
    access when sharing a single instance across threads.
    """

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        # Check whether the schema already has temporal columns by
        # inspecting the nodes table (if it exists).
        needs_migration = False
        cursor = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
        )
        col_names: set[str] = set()
        if cursor.fetchone() is not None:
            col_cursor = self._conn.execute("PRAGMA table_info(nodes)")
            col_names = {row["name"] for row in col_cursor.fetchall()}
            if "version_id" not in col_names:
                needs_migration = True

        if needs_migration:
            self._migrate_to_v2()
        else:
            self._create_v2_schema()

        # v2 → v3 additive migration: node_role + generation_spec_json.
        # Always re-check because _create_v2_schema uses IF NOT EXISTS and
        # may have been a no-op against a v2 database that predates v3.
        self._migrate_add_node_role()

        # v3 → v4 additive migration: document_ids_json (Phase 4 of
        # ADR planes-and-substrates). Same idempotent pattern —
        # pre-existing rows leave the column NULL which reads back as
        # an empty list.
        self._migrate_add_document_ids()

    def _create_v2_schema(self) -> None:
        """Create temporal (v2/v3) schema from scratch."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                version_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                node_type TEXT NOT NULL,
                node_role TEXT NOT NULL DEFAULT 'semantic',
                generation_spec_json TEXT DEFAULT NULL,
                document_ids_json TEXT DEFAULT NULL,
                properties_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS edges (
                version_id TEXT PRIMARY KEY,
                edge_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                properties_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_node_id ON nodes(node_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
            CREATE INDEX IF NOT EXISTS idx_nodes_role ON nodes(node_role);
            CREATE INDEX IF NOT EXISTS idx_nodes_valid ON nodes(valid_from, valid_to);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_current
                ON nodes(node_id) WHERE valid_to IS NULL;

            CREATE INDEX IF NOT EXISTS idx_edges_edge_id ON edges(edge_id);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
            CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
            CREATE INDEX IF NOT EXISTS idx_edges_valid ON edges(valid_from, valid_to);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_current
                ON edges(edge_id) WHERE valid_to IS NULL;

            CREATE INDEX IF NOT EXISTS idx_edges_upsert
                ON edges(source_id, target_id, edge_type) WHERE valid_to IS NULL;
        """)
        self._create_alias_schema()
        self._conn.commit()

    def _migrate_to_v2(self) -> None:
        """Migrate v1 (node_id PK) tables to v2/v3 (version_id PK, temporal,
        node_role)."""
        logger.info("migrating_graph_schema_to_v2")
        self._conn.executescript("""
            -- Nodes migration
            CREATE TABLE nodes_v2 (
                version_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                node_type TEXT NOT NULL,
                node_role TEXT NOT NULL DEFAULT 'semantic',
                generation_spec_json TEXT DEFAULT NULL,
                document_ids_json TEXT DEFAULT NULL,
                properties_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT DEFAULT NULL
            );

            INSERT INTO nodes_v2 (version_id, node_id, node_type, node_role,
                                  generation_spec_json, properties_json,
                                  created_at, updated_at, valid_from, valid_to)
            SELECT node_id, node_id, node_type, 'semantic',
                   NULL, properties_json,
                   created_at, updated_at, created_at, NULL
            FROM nodes;

            DROP TABLE nodes;
            ALTER TABLE nodes_v2 RENAME TO nodes;

            -- Edges migration
            CREATE TABLE edges_v2 (
                version_id TEXT PRIMARY KEY,
                edge_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                properties_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT DEFAULT NULL
            );

            INSERT INTO edges_v2 (version_id, edge_id, source_id, target_id,
                                  edge_type, properties_json, created_at,
                                  valid_from, valid_to)
            SELECT edge_id, edge_id, source_id, target_id,
                   edge_type, properties_json, created_at, created_at, NULL
            FROM edges;

            DROP TABLE edges;
            ALTER TABLE edges_v2 RENAME TO edges;

            -- Recreate indices
            CREATE INDEX idx_nodes_node_id ON nodes(node_id);
            CREATE INDEX idx_nodes_type ON nodes(node_type);
            CREATE INDEX idx_nodes_role ON nodes(node_role);
            CREATE INDEX idx_nodes_valid ON nodes(valid_from, valid_to);
            CREATE UNIQUE INDEX idx_nodes_current
                ON nodes(node_id) WHERE valid_to IS NULL;

            CREATE INDEX idx_edges_edge_id ON edges(edge_id);
            CREATE INDEX idx_edges_source ON edges(source_id);
            CREATE INDEX idx_edges_target ON edges(target_id);
            CREATE INDEX idx_edges_type ON edges(edge_type);
            CREATE INDEX idx_edges_valid ON edges(valid_from, valid_to);
            CREATE UNIQUE INDEX idx_edges_current
                ON edges(edge_id) WHERE valid_to IS NULL;
        """)
        self._create_alias_schema()
        self._conn.commit()
        logger.info("graph_schema_migration_complete")

    def _migrate_add_node_role(self) -> None:
        """Additive migration: ensure nodes table has node_role /
        generation_spec_json columns (v3 schema).

        Idempotent — skips columns that already exist. Existing rows are
        backfilled to ``node_role='semantic'`` (the default for historical
        content).
        """
        col_cursor = self._conn.execute("PRAGMA table_info(nodes)")
        col_names = {row["name"] for row in col_cursor.fetchall()}

        altered = False
        if "node_role" not in col_names:
            logger.info("migrating_graph_schema_add_node_role")
            self._conn.execute(
                "ALTER TABLE nodes ADD COLUMN node_role TEXT NOT NULL "
                "DEFAULT 'semantic'"
            )
            altered = True
        if "generation_spec_json" not in col_names:
            logger.info("migrating_graph_schema_add_generation_spec_json")
            self._conn.execute(
                "ALTER TABLE nodes ADD COLUMN generation_spec_json TEXT DEFAULT NULL"
            )
            altered = True

        if altered:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_nodes_role ON nodes(node_role)"
            )
            self._conn.commit()

    def _migrate_add_document_ids(self) -> None:
        """Additive migration: ensure nodes table has document_ids_json.

        Introduced in v4 for ADR planes-and-substrates §2.4 (first-class
        graph↔document link). Idempotent — skips if the column already
        exists. Pre-existing rows keep the column NULL which the read
        path surfaces as an empty ``list[str]``.
        """
        col_cursor = self._conn.execute("PRAGMA table_info(nodes)")
        col_names = {row["name"] for row in col_cursor.fetchall()}
        if "document_ids_json" in col_names:
            return
        logger.info("migrating_graph_schema_add_document_ids_json")
        self._conn.execute(
            "ALTER TABLE nodes ADD COLUMN document_ids_json TEXT DEFAULT NULL"
        )
        self._conn.commit()

    def _create_alias_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entity_aliases (
                version_id TEXT PRIMARY KEY,
                alias_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                source_system TEXT NOT NULL,
                raw_id TEXT NOT NULL,
                raw_name TEXT DEFAULT NULL,
                match_confidence REAL NOT NULL DEFAULT 1.0,
                is_primary INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_aliases_alias_id
                ON entity_aliases(alias_id);
            CREATE INDEX IF NOT EXISTS idx_aliases_entity_id
                ON entity_aliases(entity_id);
            CREATE INDEX IF NOT EXISTS idx_aliases_source_raw
                ON entity_aliases(source_system, raw_id);
            CREATE INDEX IF NOT EXISTS idx_aliases_valid
                ON entity_aliases(valid_from, valid_to);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_current
                ON entity_aliases(source_system, raw_id) WHERE valid_to IS NULL;
        """)

    # ------------------------------------------------------------------
    # Transaction support
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Context manager for batching multiple operations in one transaction."""
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Temporal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _temporal_filter(as_of: datetime | None, table_alias: str = "") -> str:
        """Return a SQL WHERE fragment for temporal filtering.

        When *as_of* is ``None`` only current rows (``valid_to IS NULL``)
        are matched.  When set, returns the version valid at that instant.
        """
        prefix = f"{table_alias}." if table_alias else ""
        if as_of is None:
            return f"{prefix}valid_to IS NULL"
        return (
            f"{prefix}valid_from <= ? AND "
            f"({prefix}valid_to IS NULL OR {prefix}valid_to > ?)"
        )

    @staticmethod
    def _temporal_params(as_of: datetime | None) -> list[str]:
        """Return bind parameters for :func:`_temporal_filter`."""
        if as_of is None:
            return []
        iso = as_of.isoformat()
        return [iso, iso]

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
        commit: bool = True,
    ) -> str:
        validate_node_role_args(node_role, generation_spec)
        validate_document_ids(document_ids)

        if node_id is None:
            node_id = generate_ulid()

        now = utc_now()
        now_iso = now.isoformat()
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
            # Close the current version
            self._conn.execute(
                """
                UPDATE nodes SET valid_to = ?
                WHERE node_id = ? AND valid_to IS NULL
                """,
                (now_iso, node_id),
            )
            # Insert new version
            version_id = generate_ulid()
            self._conn.execute(
                """
                INSERT INTO nodes
                    (version_id, node_id, node_type, node_role,
                     generation_spec_json, document_ids_json,
                     properties_json, created_at, updated_at,
                     valid_from, valid_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
                    now_iso,
                    now_iso,
                ),
            )
        else:
            version_id = generate_ulid()
            self._conn.execute(
                """
                INSERT INTO nodes
                    (version_id, node_id, node_type, node_role,
                     generation_spec_json, document_ids_json,
                     properties_json, created_at, updated_at,
                     valid_from, valid_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    version_id,
                    node_id,
                    node_type,
                    node_role,
                    generation_spec_json,
                    document_ids_json,
                    properties_json,
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )

        if commit:
            self._conn.commit()
        return node_id

    def upsert_nodes_bulk(self, nodes: list[dict[str, Any]]) -> list[str]:
        # In-process backend: no network round-trip cost, so a simple
        # loop over ``upsert_node`` is the correct implementation. The
        # bulk method exists for API symmetry; Neo4j is the backend
        # that benefits from its own UNWIND-batched override.
        #
        # Run every per-row validator up-front so the ABC's
        # validation-atomicity contract holds: invalid input is rejected
        # before any row is written. Mid-batch IO failures during the
        # subsequent loop can still leave a partial commit (per-row
        # ``commit=True``); see the ABC docstring.
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
        cursor = self._conn.execute(
            f"SELECT * FROM nodes WHERE node_id = ? AND {temporal}",
            params,
        )
        row = cursor.fetchone()
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
        placeholders = ",".join("?" for _ in node_ids)
        temporal = self._temporal_filter(as_of)
        params: list[Any] = list(node_ids) + self._temporal_params(as_of)
        cursor = self._conn.execute(
            f"SELECT * FROM nodes WHERE node_id IN ({placeholders}) AND {temporal}",
            params,
        )
        return [self._node_row_to_dict(row) for row in cursor.fetchall()]

    def get_node_history(self, node_id: str) -> list[dict[str, Any]]:
        """Return all versions of *node_id*, newest first."""
        cursor = self._conn.execute(
            "SELECT * FROM nodes WHERE node_id = ? ORDER BY valid_from DESC",
            (node_id,),
        )
        return [self._node_row_to_dict(row) for row in cursor.fetchall()]

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
        now_iso = now.isoformat()

        existing = self.resolve_alias(source_system, raw_id)
        if existing:
            self._conn.execute(
                """
                UPDATE entity_aliases SET valid_to = ?
                WHERE alias_id = ? AND valid_to IS NULL
                """,
                (now_iso, existing["alias_id"]),
            )
            alias_id = existing["alias_id"]
            created_at = existing["created_at"]
        else:
            alias_id = generate_ulid()
            created_at = now_iso

        version_id = generate_ulid()
        self._conn.execute(
            """
            INSERT INTO entity_aliases
                (version_id, alias_id, entity_id, source_system,
                 raw_id, raw_name, match_confidence, is_primary,
                 created_at, updated_at, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                version_id,
                alias_id,
                entity_id,
                source_system,
                raw_id,
                raw_name,
                match_confidence,
                1 if is_primary else 0,
                created_at,
                now_iso,
                now_iso,
            ),
        )
        self._conn.commit()
        return str(alias_id)

    def resolve_alias(
        self,
        source_system: str,
        raw_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any] | None:
        temporal = self._temporal_filter(as_of)
        params: list[Any] = [source_system, raw_id, *self._temporal_params(as_of)]
        cursor = self._conn.execute(
            f"""
            SELECT * FROM entity_aliases
            WHERE source_system = ? AND raw_id = ? AND {temporal}
            """,
            params,
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._alias_row_to_dict(row)

    def get_aliases(
        self,
        entity_id: str,
        source_system: str | None = None,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        temporal = self._temporal_filter(as_of)
        conditions = ["entity_id = ?", temporal]
        params: list[Any] = [entity_id, *self._temporal_params(as_of)]
        if source_system:
            conditions.insert(1, "source_system = ?")
            params.insert(1, source_system)
        where_clause = " AND ".join(conditions)
        cursor = self._conn.execute(
            f"""
            SELECT * FROM entity_aliases
            WHERE {where_clause}
            ORDER BY source_system, raw_id
            """,
            params,
        )
        return [self._alias_row_to_dict(row) for row in cursor.fetchall()]

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
        commit: bool = True,
    ) -> str:
        # Check if a current edge already exists by (source, target, type)
        cursor = self._conn.execute(
            """
            SELECT edge_id FROM edges
            WHERE source_id = ? AND target_id = ? AND edge_type = ?
              AND valid_to IS NULL
            """,
            (source_id, target_id, edge_type),
        )
        row = cursor.fetchone()

        now = utc_now()
        now_iso = now.isoformat()
        properties_json = json.dumps(properties or {})

        if row:
            edge_id: str = row["edge_id"]
            # Close current version
            self._conn.execute(
                """
                UPDATE edges SET valid_to = ?
                WHERE edge_id = ? AND valid_to IS NULL
                """,
                (now_iso, edge_id),
            )
            # Insert new version
            version_id = generate_ulid()
            self._conn.execute(
                """
                INSERT INTO edges
                    (version_id, edge_id, source_id, target_id, edge_type,
                     properties_json, created_at, valid_from, valid_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    version_id,
                    edge_id,
                    source_id,
                    target_id,
                    edge_type,
                    properties_json,
                    now_iso,
                    now_iso,
                ),
            )
        else:
            edge_id = generate_ulid()
            version_id = generate_ulid()
            self._conn.execute(
                """
                INSERT INTO edges
                    (version_id, edge_id, source_id, target_id, edge_type,
                     properties_json, created_at, valid_from, valid_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    version_id,
                    edge_id,
                    source_id,
                    target_id,
                    edge_type,
                    properties_json,
                    now_iso,
                    now_iso,
                ),
            )

        if commit:
            self._conn.commit()
        return edge_id

    def upsert_edges_bulk(self, edges: list[dict[str, Any]]) -> list[str]:
        # Pre-validate required keys + endpoint existence. SQLite's
        # single-row ``upsert_edge`` doesn't validate endpoints (it
        # happily inserts dangling edges); the bulk method tightens
        # that contract so callers can rely on it across backends.
        for i, spec in enumerate(edges):
            for key in ("source_id", "target_id", "edge_type"):
                if key not in spec or spec[key] is None:
                    msg = f"upsert_edges_bulk[{i}]: missing required key {key!r}"
                    raise ValueError(msg)
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
            conditions.append("source_id = ?")
            params.append(node_id)
        if direction in ("incoming", "both"):
            conditions.append("target_id = ?")
            params.append(node_id)

        where_clause = " OR ".join(conditions)

        if edge_type:
            where_clause = f"({where_clause}) AND edge_type = ?"
            params.append(edge_type)

        temporal = self._temporal_filter(as_of)
        where_clause = f"({where_clause}) AND {temporal}"
        params.extend(self._temporal_params(as_of))

        cursor = self._conn.execute(
            f"SELECT * FROM edges WHERE {where_clause}",
            params,
        )
        return [self._edge_row_to_dict(row) for row in cursor.fetchall()]

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

        # Temporal filter fragments
        node_temporal = self._temporal_filter(as_of, "n")
        node_temporal_params = self._temporal_params(as_of)
        edge_temporal = self._temporal_filter(as_of, "e")
        edge_temporal_params = self._temporal_params(as_of)

        # Build edge type filter
        edge_filter = ""
        edge_params: list[Any] = []
        if edge_types:
            placeholders = ",".join("?" for _ in edge_types)
            edge_filter = f"AND e.edge_type IN ({placeholders})"
            edge_params = list(edge_types)

        seed_placeholders = ",".join("?" for _ in seed_ids)

        # Recursive CTE to collect reachable node IDs within depth
        query = f"""
        WITH RECURSIVE traversal(node_id, depth) AS (
            -- Base case: seed nodes at depth 0
            SELECT n.node_id, 0 FROM nodes n
            WHERE n.node_id IN ({seed_placeholders})
              AND {node_temporal}

            UNION

            -- Recursive case: follow edges up to max depth
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
            WHERE t.depth < ?
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
            n.generation_spec_json,
            n.document_ids_json,
            n.properties_json,
            n.created_at,
            n.updated_at,
            n.valid_from,
            n.valid_to,
            un.min_depth
        FROM unique_nodes un
        JOIN nodes n ON n.node_id = un.node_id AND {node_temporal}
        ORDER BY un.min_depth, n.node_id
        """

        params: list[Any] = (
            list(seed_ids)
            + node_temporal_params  # base case temporal
            + edge_temporal_params  # recursive case temporal
            + edge_params
            + [depth]
            + node_temporal_params  # final JOIN temporal
        )
        cursor = self._conn.execute(query, params)

        collected_nodes: list[dict[str, Any]] = []
        node_id_set: set[str] = set()
        for row in cursor.fetchall():
            node_id_set.add(row["node_id"])
            collected_nodes.append(self._node_row_to_dict(row))

        # Fetch all edges between collected nodes
        collected_edges: list[dict[str, Any]] = []
        if node_id_set:
            node_list = list(node_id_set)
            np_ = ",".join("?" for _ in node_list)
            edge_temporal_frag = self._temporal_filter(as_of)

            edge_query = f"""
            SELECT * FROM edges
            WHERE source_id IN ({np_})
              AND target_id IN ({np_})
              AND {edge_temporal_frag}
            """
            eq_params: list[Any] = node_list + node_list + self._temporal_params(as_of)
            if edge_types:
                etp = ",".join("?" for _ in edge_types)
                edge_query += f" AND edge_type IN ({etp})"
                eq_params += list(edge_types)

            cursor = self._conn.execute(edge_query, eq_params)
            collected_edges = [self._edge_row_to_dict(row) for row in cursor.fetchall()]

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
            conditions.append("node_type = ?")
            params.append(node_type)

        # Push simple property filters to SQL via json_extract
        complex_filters: dict[str, Any] = {}
        if properties:
            for key, value in properties.items():
                if isinstance(value, bool):
                    conditions.append(f"json_extract(properties_json, '$.{key}') = ?")
                    params.append(1 if value else 0)
                elif isinstance(value, str | int | float):
                    conditions.append(f"json_extract(properties_json, '$.{key}') = ?")
                    params.append(value)
                elif value is None:
                    conditions.append(
                        f"json_extract(properties_json, '$.{key}') IS NULL"
                    )
                else:
                    complex_filters[key] = value

        where_clause = " AND ".join(conditions)
        params.append(limit)

        cursor = self._conn.execute(
            f"""
            SELECT * FROM nodes
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        )

        results: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            node = self._node_row_to_dict(row)
            # Apply complex filters Python-side
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
        # Cascade: delete all edge versions referencing this node
        self._conn.execute(
            "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        )
        self._conn.execute(
            "DELETE FROM entity_aliases WHERE entity_id = ?",
            (node_id,),
        )
        cursor = self._conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("node_deleted", node_id=node_id)
        return deleted

    def delete_edge(self, edge_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM edges WHERE edge_id = ?", (edge_id,))
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("edge_deleted", edge_id=edge_id)
        return deleted

    # ------------------------------------------------------------------
    # Counts
    # ------------------------------------------------------------------

    def count_nodes(self) -> int:
        cursor = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM nodes WHERE valid_to IS NULL"
        )
        row = cursor.fetchone()
        assert row is not None
        return int(row["cnt"])

    def count_edges(self) -> int:
        cursor = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM edges WHERE valid_to IS NULL"
        )
        row = cursor.fetchone()
        assert row is not None
        return int(row["cnt"])

    # ------------------------------------------------------------------
    # Canonical DSL — Phase 2 compiler
    # ------------------------------------------------------------------

    def execute_node_query(self, query: Any) -> list[dict[str, Any]]:
        """Compile :class:`NodeQuery` to a SQLite SELECT.

        Supports the full Phase 1 operator surface (``eq`` / ``in`` /
        ``exists``) on:

        * ``node_type`` (column comparison)
        * ``node_role`` (column comparison)
        * ``properties.<key>`` (via ``json_extract``)
        """
        sql, params = self._compile_node_query(query)
        cursor = self._conn.execute(sql, params)
        return [self._node_row_to_dict(row) for row in cursor.fetchall()]

    def _compile_node_query(self, query: Any) -> tuple[str, list[Any]]:
        """Pure compile step — returns (sql, params). No I/O.

        Exposed so it's unit-testable without a live store.
        """
        where_parts: list[str] = [self._temporal_filter(query.as_of)]
        params: list[Any] = list(self._temporal_params(query.as_of))
        for clause in query.filters:
            frag, p = self._compile_clause_sqlite(clause)
            where_parts.append(frag)
            params.extend(p)
        sql = (
            "SELECT * FROM nodes WHERE "
            + " AND ".join(where_parts)
            + " ORDER BY created_at DESC LIMIT ?"
        )
        params.append(query.limit)
        return sql, params

    @staticmethod
    def _compile_clause_sqlite(clause: Any) -> tuple[str, list[Any]]:
        """Translate one :class:`FilterClause` to a SQLite WHERE fragment."""
        column = SQLiteGraphStore._field_to_sql_expr(clause.field)
        if clause.op == "eq":
            return f"{column} = ?", [clause.value]
        if clause.op == "in":
            placeholders = ", ".join("?" for _ in clause.value)
            return f"{column} IN ({placeholders})", list(clause.value)
        if clause.op == "exists":
            return f"{column} IS NOT NULL", []
        msg = f"Unknown filter op {clause.op!r}"
        raise ValueError(msg)

    @staticmethod
    def _field_to_sql_expr(field: str) -> str:
        """Map a DSL field path to a SQLite column / ``json_extract`` expression."""
        if field in {"node_type", "node_role", "node_id"}:
            return field
        if field.startswith("properties."):
            key = field.split(".", 1)[1]
            # SQLite identifier-quoting on JSON path keys is unnecessary
            # because keys come from agents and are constrained to JSON;
            # parameter binding doesn't apply to json paths in
            # json_extract — but the path string itself is constructed
            # from the DSL so it's not user input.
            return f"json_extract(properties_json, '$.{key}')"
        msg = f"Unsupported DSL field path: {field!r}"
        raise ValueError(msg)

    # ------------------------------------------------------------------
    # Compaction (Gap 4.2 — SCD2 retention)
    # ------------------------------------------------------------------

    #: Tables whose closed SCD2 rows are subject to compaction. Order
    #: controls report aggregation, not correctness — closed rows are
    #: independent across tables.
    _COMPACTABLE_TABLES = ("nodes", "edges", "entity_aliases")

    def compact_versions(
        self,
        before: datetime,
        *,
        dry_run: bool = False,
        event_log: EventLog | None = None,
    ) -> CompactionReport:
        before_iso = before.isoformat()
        start_ns = time.monotonic_ns()

        counts: dict[str, int] = {}
        #: Parse each row's ``MIN/MAX(valid_to)`` to ``datetime`` on the
        #: way in so the outer ``min()/max()`` compares datetimes, not
        #: strings. Lexicographic ISO comparison breaks silently if
        #: formats ever mix (``Z`` vs ``+00:00``, varying offsets).
        range_valid_to: list[datetime] = []
        with self.transaction():
            for table in self._COMPACTABLE_TABLES:
                row = self._conn.execute(
                    f"SELECT COUNT(*) AS cnt, "
                    f"MIN(valid_to) AS oldest, MAX(valid_to) AS newest "
                    f"FROM {table} "
                    f"WHERE valid_to IS NOT NULL AND valid_to < ?",
                    (before_iso,),
                ).fetchone()
                count = int(row["cnt"]) if row is not None else 0
                counts[table] = count
                if count > 0 and row is not None:
                    if row["oldest"] is not None:
                        range_valid_to.append(datetime.fromisoformat(row["oldest"]))
                    if row["newest"] is not None:
                        range_valid_to.append(datetime.fromisoformat(row["newest"]))
                if count > 0 and not dry_run:
                    self._conn.execute(
                        f"DELETE FROM {table} "
                        f"WHERE valid_to IS NOT NULL AND valid_to < ?",
                        (before_iso,),
                    )

        oldest_dt = min(range_valid_to) if range_valid_to else None
        newest_dt = max(range_valid_to) if range_valid_to else None
        report = CompactionReport(
            before=before,
            nodes_compacted=counts.get("nodes", 0),
            edges_compacted=counts.get("edges", 0),
            aliases_compacted=counts.get("entity_aliases", 0),
            oldest_compacted_valid_to=oldest_dt,
            newest_compacted_valid_to=newest_dt,
            dry_run=dry_run,
            duration_ms=max((time.monotonic_ns() - start_ns) // 1_000_000, 0),
        )
        logger.info(
            "graph_versions_compacted",
            before=before_iso,
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
    def _node_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        # node_role / generation_spec_json are v3 additive columns;
        # document_ids_json is a v4 additive column (Phase 4 of ADR
        # planes-and-substrates). Older in-memory row objects (from
        # tests constructing rows manually) may not carry them, so
        # tolerate their absence.
        row_keys = set(row.keys())
        node_role = row["node_role"] if "node_role" in row_keys else "semantic"
        gen_spec_raw = (
            row["generation_spec_json"] if "generation_spec_json" in row_keys else None
        )
        generation_spec = json.loads(gen_spec_raw) if gen_spec_raw else None
        doc_ids_raw = (
            row["document_ids_json"] if "document_ids_json" in row_keys else None
        )
        document_ids = json.loads(doc_ids_raw) if doc_ids_raw else []
        return {
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "node_role": node_role,
            "generation_spec": generation_spec,
            "document_ids": document_ids,
            "properties": json.loads(row["properties_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
        }

    @staticmethod
    def _edge_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "edge_id": row["edge_id"],
            "source_id": row["source_id"],
            "target_id": row["target_id"],
            "edge_type": row["edge_type"],
            "properties": json.loads(row["properties_json"]),
            "created_at": row["created_at"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
        }

    @staticmethod
    def _alias_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "alias_id": row["alias_id"],
            "entity_id": row["entity_id"],
            "source_system": row["source_system"],
            "raw_id": row["raw_id"],
            "raw_name": row["raw_name"],
            "match_confidence": row["match_confidence"],
            "is_primary": bool(row["is_primary"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
        }
