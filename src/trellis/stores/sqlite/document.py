"""SQLiteDocumentStore — SQLite-backed document store with FTS5."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.core.hashing import content_hash as _content_hash
from trellis.core.ids import generate_ulid
from trellis.stores.base.document import DocumentStore
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


def _build_tag_conditions(
    tag_filters: dict[str, Any],
) -> tuple[list[str], list[Any]]:
    """Build SQL conditions for content_tags filtering.

    Handles two facet shapes:
    - **list facet** (e.g. ``domain``): the item's stored value is a JSON array;
      we check if *any* of the filter values appears in that array.
    - **scalar facet** (e.g. ``content_type``, ``signal_quality``): the item's
      stored value is a string; we check ``IN (?, ?, …)``.
    """
    conditions: list[str] = []
    params: list[Any] = []
    list_facets = {"domain"}

    for facet, values in tag_filters.items():
        if not isinstance(values, list) or not values:
            continue
        json_path = f"$.content_tags.{facet}"
        if facet in list_facets:
            # Array facets: check if any filter value is in the JSON array.
            sub_parts = " OR ".join("je.value = ?" for _ in values)
            conditions.append(
                f"EXISTS (SELECT 1 FROM json_each(d.metadata_json, '{json_path}') je"
                f" WHERE {sub_parts})"
            )
            params.extend(values)
        else:
            # For scalar facets: simple IN check.
            placeholders = ", ".join("?" for _ in values)
            conditions.append(
                f"json_extract(d.metadata_json, '{json_path}') IN ({placeholders})"
            )
            params.extend(values)

    return conditions, params


class SQLiteDocumentStore(SQLiteStoreBase, DocumentStore):
    """SQLite-backed document store with FTS5 full-text search.

    Note: Uses ``check_same_thread=False`` for compatibility with async
    frameworks but provides no internal locking. Callers must synchronise
    access when sharing a single instance across threads.
    """

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                content_hash TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                doc_id,
                content
            );

            CREATE INDEX IF NOT EXISTS idx_documents_created
                ON documents(created_at);

            CREATE INDEX IF NOT EXISTS idx_documents_hash
                ON documents(content_hash);
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def put(
        self,
        doc_id: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if doc_id is None:
            doc_id = generate_ulid()

        now = utc_now().isoformat()
        metadata = metadata or {}
        metadata_json = json.dumps(metadata)
        chash = _content_hash(content)

        self._conn.execute(
            """
            INSERT INTO documents
                (doc_id, content, content_hash,
                 metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                content = excluded.content,
                content_hash = excluded.content_hash,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (doc_id, content, chash, metadata_json, now, now),
        )
        # FTS5 doesn't support ON CONFLICT — delete+insert
        self._conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
        self._conn.execute(
            "INSERT INTO documents_fts (doc_id, content) VALUES (?, ?)",
            (doc_id, content),
        )

        self._conn.commit()
        logger.debug("document_stored", doc_id=doc_id)
        return doc_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete(self, doc_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        self._conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.debug("document_deleted", doc_id=doc_id)
        return deleted

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        sanitized = self._sanitize_fts_query(query)
        if not sanitized:
            return []

        # Split filters into SQL-pushable vs complex
        filter_conditions: list[str] = []
        filter_params: list[Any] = []
        complex_filters: dict[str, Any] = {}

        if filters:
            for key, value in filters.items():
                if key == "content_tags" and isinstance(value, dict):
                    tag_conds, tag_params = _build_tag_conditions(value)
                    filter_conditions.extend(tag_conds)
                    filter_params.extend(tag_params)
                elif isinstance(value, bool):
                    filter_conditions.append(
                        f"json_extract(d.metadata_json, '$.{key}') = ?"
                    )
                    filter_params.append(1 if value else 0)
                elif isinstance(value, str | int | float):
                    filter_conditions.append(
                        f"json_extract(d.metadata_json, '$.{key}') = ?"
                    )
                    filter_params.append(value)
                else:
                    complex_filters[key] = value

        where_parts = ["documents_fts MATCH ?"]
        sql_params: list[Any] = [sanitized]

        if filter_conditions:
            where_parts.extend(filter_conditions)
            sql_params.extend(filter_params)

        sql_params.append(limit)
        where_clause = " AND ".join(where_parts)

        sql = (
            "SELECT d.*, bm25(documents_fts) as rank"
            " FROM documents d"
            " JOIN documents_fts fts ON d.doc_id = fts.doc_id"
            f" WHERE {where_clause}"
            " ORDER BY rank"
            " LIMIT ?"
        )
        cursor = self._conn.execute(sql, sql_params)

        results: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            doc = self._row_to_dict(row, include_rank=True)
            if complex_filters:
                metadata = doc["metadata"]
                if not all(metadata.get(k) == v for k, v in complex_filters.items()):
                    continue
            results.append(doc)
        return results

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Sanitize a query string for FTS5 MATCH."""
        if not query or not query.strip():
            return ""

        sanitized = query.replace("\n", " ").replace("\t", " ")
        words = re.findall(r"[a-zA-Z0-9]+", sanitized)
        if not words:
            return ""

        return " OR ".join(f'"{w}"' for w in words[:20])

    # ------------------------------------------------------------------
    # Listing / counting
    # ------------------------------------------------------------------

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        cursor = self._conn.execute(
            """
            SELECT * FROM documents
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def count(self) -> int:
        cursor = self._conn.execute("SELECT COUNT(*) as cnt FROM documents")
        row = cursor.fetchone()
        assert row is not None
        return int(row["cnt"])

    # ------------------------------------------------------------------
    # Hash lookup
    # ------------------------------------------------------------------

    def get_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "SELECT * FROM documents WHERE content_hash = ?", (content_hash,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, *, include_rank: bool = False) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "doc_id": row["doc_id"],
            "content": row["content"],
            "content_hash": row["content_hash"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_rank:
            doc["rank"] = row["rank"]
        return doc
