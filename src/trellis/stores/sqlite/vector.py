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
from trellis.stores.sqlite.base import SQLiteStoreBase, json_key_path

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

    def reset_storage(self) -> None:
        """Drop the ``vectors`` table and recreate it empty.

        Dropping the table takes ``idx_vectors_created`` with it and
        ``_init_schema`` recreates both. This is the body that used to sit
        inline in ``trellis_api.routes.admin.reset_vectors``, driving
        ``self._conn`` from outside the package (#512).
        """
        self._conn.execute("DROP TABLE IF EXISTS vectors")
        self._conn.commit()
        self._init_schema()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def dimensions(self) -> int | None:
        """``None`` — this backend pins no width.

        Not "unknown" and not a fallback. The ``vectors`` table carries a
        ``dimensions`` column *per row*, filled from ``len(vector)`` on
        every :meth:`upsert`, so a 3-wide and a 1536-wide vector coexist
        happily; nothing in the schema constrains the width. ``None`` is
        therefore the true answer rather than the absence of one.
        """
        return None

    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        blob = np.array(vector, dtype=np.float32).tobytes()
        dimensions = len(vector)
        meta_json = json.dumps(metadata or {}, allow_nan=False)

        conn = self._conn
        with conn:  # commits on success, rolls back on an exception
            conn.execute(
                "INSERT OR REPLACE INTO vectors "
                "(item_id, vector_blob, dimensions, metadata_json) "
                "VALUES (?, ?, ?, ?)",
                (item_id, blob, dimensions, meta_json),
            )

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
        """Return the ``top_k`` stored rows most similar to ``vector``.

        Only rows of the query's own width are scored. A cosine between two
        widths is undefined, and a row of another width reaching ``np.dot``
        raised ``ValueError`` and failed the whole query (and with it every
        semantic pack). The width filter runs in SQL, so a row of another
        width is not read at all.

        The skip is silent while the query returns rows: counting what was
        skipped is a second full scan of ``vectors`` (there is no index on
        ``dimensions``), a cost not worth paying on every query. The one case
        that has to be heard is the degenerate one, a width that matches no
        stored row, which is what an embedder change that re-embedded nothing
        produces and would otherwise read as an empty semantic axis. Only when
        the width-filtered fetch is empty does the query pay for that scan, and
        if any row of another width passes the same SQL-pushed filters it logs
        one ``vector_query.width_mismatch`` warning (``query_width`` and
        ``other_width_rows``, counts only) and returns ``[]``. A list- or
        dict-valued filter runs Python-side, so it is not applied to that count.
        """
        query_vec = np.array(vector, dtype=np.float32)
        query_norm = float(np.linalg.norm(query_vec))
        if query_norm == 0.0:
            return []

        # Push metadata filters to SQL where possible. The width test comes
        # first and is not a caller filter: it is what makes the dot product
        # below well defined. No index backs it, and none is needed — the
        # brute-force scoring that follows reads every surviving row anyway.
        where_parts: list[str] = ["dimensions = ?"]
        params: list[Any] = [int(query_vec.shape[0])]
        complex_filters: dict[str, Any] = {}

        if filters:
            for key, value in filters.items():
                # A bool is an int, so it takes this branch too, and sqlite3
                # binds it as 1 or 0: what json_extract returns for true/false.
                if isinstance(value, str | int | float):
                    where_parts.append("json_extract(metadata_json, ?) = ?")
                    params.extend([json_key_path(key), value])
                else:
                    complex_filters[key] = value

        sql = (
            "SELECT item_id, vector_blob, metadata_json FROM vectors WHERE "
            + " AND ".join(where_parts)
        )

        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            self._warn_if_width_mismatch(
                int(query_vec.shape[0]), where_parts[1:], params[1:]
            )
            return []

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
        conn = self._conn
        with conn:  # commits on success, rolls back on an exception
            cursor = conn.execute(
                "DELETE FROM vectors WHERE item_id = ?",
                (item_id,),
            )
        return cursor.rowcount > 0

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _warn_if_width_mismatch(
        self, query_width: int, filter_parts: list[str], filter_params: list[Any]
    ) -> None:
        """Warn once when a query found nothing of its width but rows of another.

        Called only after the width-filtered fetch came back empty, so the
        second scan this costs is paid on the degenerate path alone. The count
        takes the same SQL-pushed filters as the fetch: a filter that matches
        no row at all is an empty answer, not a width mismatch, and must not
        read as one. Counts only, never an id or a value.
        """
        sql = "SELECT COUNT(*) FROM vectors WHERE dimensions != ?"
        for part in filter_parts:
            sql += " AND " + part
        other = int(
            self._conn.execute(sql, [query_width, *filter_params]).fetchone()[0]
        )
        if other:
            logger.warning(
                "vector_query.width_mismatch",
                query_width=query_width,
                other_width_rows=other,
            )

    @staticmethod
    def _matches_filters(meta: dict[str, Any], filters: dict[str, Any]) -> bool:
        """Check if metadata matches all filter conditions."""
        for key, value in filters.items():
            if key not in meta or meta[key] != value:
                return False
        return True
