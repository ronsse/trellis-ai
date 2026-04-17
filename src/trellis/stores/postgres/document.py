"""PostgresDocumentStore — Postgres-backed document store with tsvector FTS."""

from __future__ import annotations

import json
from typing import Any

import structlog

from trellis.core.base import utc_now
from trellis.core.hashing import content_hash as _content_hash
from trellis.core.ids import generate_ulid
from trellis.stores.base.document import DocumentStore
from trellis.stores.postgres.base import PostgresStoreBase

logger = structlog.get_logger(__name__)


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    content_hash TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(doc_id, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(metadata->>'title', '')), 'A') ||
        setweight(to_tsvector('english', coalesce(metadata->>'domain', '')), 'B') ||
        setweight(to_tsvector('english', content), 'C')
    ) STORED
)"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_documents_tsv ON documents USING GIN(tsv)",
]


class PostgresDocumentStore(PostgresStoreBase, DocumentStore):
    """Postgres-backed document store with tsvector full-text search."""

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
            # Migrate existing tables: upgrade tsv column to include metadata
            self._migrate_tsv_weights(cur)
        self.conn.commit()

    def _migrate_tsv_weights(self, cur: Any) -> None:
        """Upgrade tsv column to weighted search if it only indexes content.

        Safe to call repeatedly — checks the column definition before altering.
        """
        cur.execute(
            """
            SELECT pg_get_expr(adbin, adrelid)
            FROM pg_attrdef
            JOIN pg_attribute ON attrelid = adrelid AND attnum = adnum
            WHERE attrelid = 'documents'::regclass AND attname = 'tsv'
            """
        )
        row = cur.fetchone()
        if row and "setweight" not in str(row[0]):
            logger.info(
                "Upgrading documents.tsv to weighted search"
                " (doc_id + title + domain + content)"
            )
            cur.execute("ALTER TABLE documents DROP COLUMN tsv")
            cur.execute("""
                ALTER TABLE documents ADD COLUMN tsv tsvector
                GENERATED ALWAYS AS (
                    setweight(to_tsvector('english',
                        coalesce(doc_id, '')), 'A') ||
                    setweight(to_tsvector('english',
                        coalesce(metadata->>'title', '')), 'A') ||
                    setweight(to_tsvector('english',
                        coalesce(metadata->>'domain', '')), 'B') ||
                    setweight(to_tsvector('english', content), 'C')
                ) STORED
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_tsv"
                " ON documents USING GIN(tsv)"
            )

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

        now = utc_now()
        metadata = metadata or {}
        metadata_json = json.dumps(metadata)
        chash = _content_hash(content)

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                    (doc_id, content, content_hash, metadata, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id) DO UPDATE SET
                    content = EXCLUDED.content,
                    content_hash = EXCLUDED.content_hash,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                (doc_id, content, chash, metadata_json, now, now),
            )
        self.conn.commit()
        logger.debug("document_stored", doc_id=doc_id)
        return doc_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, content, content_hash, metadata,
                       created_at, updated_at
                FROM documents WHERE doc_id = %s
                """,
                (doc_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete(self, doc_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE doc_id = %s", (doc_id,))
            deleted = bool(cur.rowcount > 0)
        self.conn.commit()
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
        if not query or not query.strip():
            return []

        conditions = ["tsv @@ plainto_tsquery('english', %s)"]
        params: list[Any] = [query]

        if filters:
            for key, value in filters.items():
                if isinstance(value, (str, int, float, bool)):
                    conditions.append("metadata->>%s = %s")
                    params.extend([key, str(value)])

        where_clause = " AND ".join(conditions)
        params.append(limit)

        sql = f"""
            SELECT doc_id, content, content_hash, metadata,
                   created_at, updated_at,
                   ts_rank(tsv, plainto_tsquery('english', %s)) AS rank
            FROM documents
            WHERE {where_clause}
            ORDER BY rank DESC
            LIMIT %s
        """
        # The first %s in the SELECT is the ranking query param
        all_params: list[Any] = [query, *params]

        with self.conn.cursor() as cur:
            cur.execute(sql, all_params)
            rows = cur.fetchall()

        return [self._row_to_dict(row, include_rank=True) for row in rows]

    # ------------------------------------------------------------------
    # Listing / counting
    # ------------------------------------------------------------------

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, content, content_hash, metadata,
                       created_at, updated_at
                FROM documents
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM documents")
            row = cur.fetchone()
        assert row is not None
        return int(row[0])

    # ------------------------------------------------------------------
    # Hash lookup
    # ------------------------------------------------------------------

    def get_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, content, content_hash, metadata,
                       created_at, updated_at
                FROM documents WHERE content_hash = %s
                """,
                (content_hash,),
            )
            row = cur.fetchone()
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
    def _row_to_dict(
        row: tuple[Any, ...],
        *,
        include_rank: bool = False,
    ) -> dict[str, Any]:
        metadata_raw = row[3]
        if isinstance(metadata_raw, str):
            metadata = json.loads(metadata_raw)
        elif isinstance(metadata_raw, dict):
            metadata = metadata_raw
        else:
            metadata = {}

        created = row[4]
        updated = row[5]
        doc: dict[str, Any] = {
            "doc_id": row[0],
            "content": row[1],
            "content_hash": row[2],
            "metadata": metadata,
            "created_at": (
                created.isoformat() if hasattr(created, "isoformat") else created
            ),
            "updated_at": (
                updated.isoformat() if hasattr(updated, "isoformat") else updated
            ),
        }
        if include_rank:
            doc["rank"] = row[6]
        return doc
