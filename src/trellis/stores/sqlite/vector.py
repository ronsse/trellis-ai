"""SQLiteVectorStore — SQLite-backed vector store with cosine similarity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

from trellis.stores.base.vector import VectorStore
from trellis.stores.sqlite.base import SQLiteStoreBase

logger = structlog.get_logger(__name__)


class SQLiteVectorStore(SQLiteStoreBase, VectorStore):
    """SQLite-backed vector store with brute-force cosine similarity.

    Note: Uses ``check_same_thread=False`` for compatibility with async
    frameworks but provides no internal locking. Callers must synchronise
    access when sharing a single instance across threads.
    """

    def __init__(self, db_path: str | Path) -> None:
        if not HAS_NUMPY:
            msg = (
                "numpy is required for SQLiteVectorStore. "
                "Install it with: pip install numpy"
            )
            raise ImportError(msg)
        super().__init__(db_path)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vectors (
                item_id TEXT PRIMARY KEY,
                vector_blob BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_vectors_created
                ON vectors(created_at);
            """
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        blob = np.array(vector, dtype=np.float32).tobytes()
        dimensions = len(vector)
        meta_json = json.dumps(metadata or {})

        self._conn.execute(
            "INSERT OR REPLACE INTO vectors "
            "(item_id, vector_blob, dimensions, metadata_json) "
            "VALUES (?, ?, ?, ?)",
            (item_id, blob, dimensions, meta_json),
        )
        self._conn.commit()

    def upsert_bulk(self, items: list[dict[str, Any]]) -> None:
        # In-process backend: a simple loop is the correct
        # implementation. The bulk method exists for API symmetry;
        # Neo4j is the backend that benefits from the UNWIND override.
        # Pre-validate keys so missing-key errors surface as
        # ValueError (with the offending index) instead of KeyError
        # mid-loop. Then reject within-batch duplicate item_ids so the
        # contract matches the network-bound backends.
        self._validate_bulk_required_keys(items, ("item_id", "vector"), "upsert_bulk")
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
        query_vec = np.array(vector, dtype=np.float32)
        query_norm = float(np.linalg.norm(query_vec))
        if query_norm == 0.0:
            return []

        # Push metadata filters to SQL where possible
        where_parts: list[str] = []
        params: list[Any] = []
        complex_filters: dict[str, Any] = {}

        if filters:
            for key, value in filters.items():
                if isinstance(value, str | int | float):
                    where_parts.append(f"json_extract(metadata_json, '$.{key}') = ?")
                    params.append(value)
                elif isinstance(value, bool):
                    where_parts.append(f"json_extract(metadata_json, '$.{key}') = ?")
                    params.append(1 if value else 0)
                else:
                    complex_filters[key] = value

        sql = "SELECT item_id, vector_blob, metadata_json FROM vectors"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)

        rows = self._conn.execute(sql, params).fetchall()

        scored: list[dict[str, Any]] = []
        for row in rows:
            stored_vec = np.frombuffer(row["vector_blob"], dtype=np.float32)
            stored_norm = float(np.linalg.norm(stored_vec))
            if stored_norm == 0.0:
                continue

            score = float(np.dot(query_vec, stored_vec) / (query_norm * stored_norm))
            meta = json.loads(row["metadata_json"])

            # Apply remaining complex filters Python-side
            if complex_filters and not self._matches_filters(meta, complex_filters):
                continue

            scored.append(
                {
                    "item_id": row["item_id"],
                    "score": score,
                    "metadata": meta,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def get(self, item_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT item_id, vector_blob, dimensions, metadata_json "
            "FROM vectors WHERE item_id = ?",
            (item_id,),
        ).fetchone()

        if row is None:
            return None

        vec = np.frombuffer(row["vector_blob"], dtype=np.float32)
        return {
            "item_id": row["item_id"],
            "vector": vec.tolist(),
            "dimensions": row["dimensions"],
            "metadata": json.loads(row["metadata_json"]),
        }

    def delete(self, item_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM vectors WHERE item_id = ?",
            (item_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _matches_filters(meta: dict[str, Any], filters: dict[str, Any]) -> bool:
        """Check if metadata matches all filter conditions."""
        for key, value in filters.items():
            if key not in meta or meta[key] != value:
                return False
        return True
