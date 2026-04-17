"""PgVectorStore — PostgreSQL + pgvector backed vector store."""

from __future__ import annotations

import json
from typing import Any

import structlog

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
                (item_id, vector, meta_json),
            )
        self.conn.commit()
        logger.debug("vector_upserted", item_id=item_id, dimensions=len(vector))

    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        where_clauses: list[str] = []
        params: list[Any] = [vector, vector, top_k]

        if filters:
            for key, value in filters.items():
                where_clauses.append("metadata @> %s::jsonb")
                params.insert(-1, json.dumps({key: value}))

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
