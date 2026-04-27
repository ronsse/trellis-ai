"""PgVectorStore — PostgreSQL + pgvector backed vector store."""

from __future__ import annotations

import json
from typing import Any

import structlog

from trellis.stores.base._bulk_validation import validate_bulk_required_keys
from trellis.stores.base.vector import VectorStore

logger = structlog.get_logger(__name__)

try:
    import psycopg

    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False

try:
    from pgvector.psycopg import register_vector

    HAS_PGVECTOR = True
except ImportError:
    HAS_PGVECTOR = False


def _format_vector(vec: list[float]) -> str:
    # pgvector's psycopg dumper only adapts numpy.ndarray; a plain Python list
    # gets sent as a Postgres array (e.g. smallint[]) and the ::vector cast
    # then fails with "cannot cast smallint[] to vector". Formatting as the
    # vector text literal lets the cast succeed without pulling in numpy.
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorStore(VectorStore):
    """PostgreSQL + pgvector backed vector store with HNSW indexing.

    Parameters
    ----------
    dsn:
        PostgreSQL connection string (e.g. ``"postgresql://user:pass@host/db"``).
    dimensions:
        Dimensionality of stored vectors. Defaults to 1536 (OpenAI ada-002).
    """

    def __init__(self, dsn: str, dimensions: int = 1536) -> None:
        if not HAS_PSYCOPG:
            msg = (
                "psycopg is required for PgVectorStore. "
                "Install it with: pip install psycopg[binary]"
            )
            raise ImportError(msg)
        if not HAS_PGVECTOR:
            msg = (
                "pgvector is required for PgVectorStore. "
                "Install it with: pip install pgvector"
            )
            raise ImportError(msg)

        self._dsn = dsn
        self._dimensions = dimensions
        self._conn = psycopg.connect(dsn, autocommit=False)
        register_vector(self._conn)
        self._init_schema()
        logger.info(
            "pgvector_store_initialized",
            dimensions=dimensions,
        )

    @property
    def conn(self) -> psycopg.Connection:
        """Return the connection, reconnecting if it was closed."""
        if self._conn.closed:
            logger.warning("pgvector_reconnecting_closed_connection")
            self._conn = psycopg.connect(self._dsn, autocommit=False)
            register_vector(self._conn)
        return self._conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS vectors (
                    item_id    TEXT PRIMARY KEY,
                    embedding  vector({self._dimensions}),
                    metadata   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vectors_embedding
                    ON vectors USING hnsw (embedding vector_cosine_ops)
                """
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        meta_json = json.dumps(metadata or {})
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vectors (item_id, embedding, metadata)
                VALUES (%s, %s::vector, %s::jsonb)
                ON CONFLICT (item_id)
                DO UPDATE SET embedding  = EXCLUDED.embedding,
                              metadata   = EXCLUDED.metadata
                """,
                (item_id, _format_vector(vector), meta_json),
            )
        self.conn.commit()

    def upsert_bulk(self, items: list[dict[str, Any]]) -> None:
        # Network-bound but no UNWIND-style batching here — simple
        # loop. Bulk method exists for API symmetry with Neo4j (which
        # has its own UNWIND override). Pre-validates keys so missing
        # fields surface as ValueError-with-index instead of KeyError
        # mid-loop, then rejects within-batch duplicate item_ids so
        # the contract matches the network-bound backends.
        validate_bulk_required_keys(items, ("item_id", "vector"), "upsert_bulk")
        self._pre_validate_bulk_item_ids(items)
        for spec in items:
            self.upsert(
                item_id=spec["item_id"],
                vector=spec["vector"],
                metadata=spec.get("metadata"),
            )

    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        where_clauses: list[str] = []
        filter_params: list[Any] = []
        if filters:
            for key, value in filters.items():
                where_clauses.append("metadata @> %s::jsonb")
                filter_params.append(json.dumps({key: value}))

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        sql = f"""
            SELECT item_id,
                   1 - (embedding <=> %s::vector) AS score,
                   metadata
            FROM vectors
            {where_sql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """

        formatted = _format_vector(vector)
        # Param order must match placeholder order: SELECT vector, then filter
        # JSONs (in WHERE), then ORDER BY vector, then LIMIT.
        params: list[Any] = [formatted, *filter_params, formatted, top_k]

        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        return [
            {
                "item_id": row[0],
                "score": float(row[1]),
                "metadata": row[2] if isinstance(row[2], dict) else json.loads(row[2]),
            }
            for row in rows
        ]

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT item_id, embedding, metadata
                FROM vectors WHERE item_id = %s
                """,
                (item_id,),
            )
            row = cur.fetchone()

        if row is None:
            return None

        vec = list(row[1]) if not isinstance(row[1], list) else row[1]
        meta = row[2] if isinstance(row[2], dict) else json.loads(row[2])
        return {
            "item_id": row[0],
            "vector": vec,
            "dimensions": len(vec),
            "metadata": meta,
        }

    def delete(self, item_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM vectors WHERE item_id = %s",
                (item_id,),
            )
            deleted = bool(cur.rowcount > 0)
        self.conn.commit()
        return deleted

    def count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM vectors")
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._conn.close()
        logger.info("pgvector_store_closed")
