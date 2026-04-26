"""LanceVectorStore — LanceDB-backed vector store with native ANN indexing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from trellis.stores.base.vector import VectorStore

logger = structlog.get_logger(__name__)

try:
    import lancedb  # type: ignore[import-untyped]

    HAS_LANCEDB = True
except ImportError:
    HAS_LANCEDB = False

try:
    import pyarrow as pa  # type: ignore[import-untyped]

    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False


class LanceVectorStore(VectorStore):
    """LanceDB-backed vector store with native ANN indexing.

    LanceDB is serverless — no external database needed. Data is stored
    as Lance-format files in a local directory (or S3/GCS path).

    Parameters
    ----------
    uri:
        Directory path for the LanceDB database.
    table_name:
        Name of the table to store vectors in.
    metric:
        Distance metric for search (``"cosine"``, ``"L2"``, or ``"dot"``).
    """

    def __init__(
        self,
        uri: str | Path,
        table_name: str = "vectors",
        metric: str = "cosine",
    ) -> None:
        if not HAS_LANCEDB:
            msg = (
                "lancedb is required for LanceVectorStore. "
                "Install it with: pip install lancedb"
            )
            raise ImportError(msg)
        if not HAS_PYARROW:
            msg = (
                "pyarrow is required for LanceVectorStore. "
                "Install it with: pip install pyarrow"
            )
            raise ImportError(msg)

        self._uri = str(uri)
        self._table_name = table_name
        self._metric = metric
        self._db = lancedb.connect(self._uri)
        self._table: Any = None

        # Try to open an existing table
        table_list = (
            self._db.list_tables()
            if hasattr(self._db, "list_tables")
            else self._db.table_names()
        )
        if self._table_name in table_list:
            self._table = self._db.open_table(self._table_name)

        logger.info(
            "lancedb_vector_store_initialized",
            uri=self._uri,
            table_name=table_name,
            metric=metric,
        )

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------

    def _make_schema(self, dimensions: int) -> pa.Schema:
        """Build a PyArrow schema for the vectors table."""
        return pa.schema(
            [
                pa.field("item_id", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), list_size=dimensions)),
                pa.field("metadata_json", pa.string()),
            ]
        )

    def _ensure_table(self, dimensions: int) -> Any:
        """Create the table if it does not exist yet."""
        if self._table is None:
            schema = self._make_schema(dimensions)
            self._table = self._db.create_table(
                self._table_name,
                schema=schema,
            )
            logger.debug(
                "lancedb_table_created",
                table_name=self._table_name,
                dimensions=dimensions,
            )
        return self._table

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        dimensions = len(vector)
        table = self._ensure_table(dimensions)
        meta_json = json.dumps(metadata or {})

        data = [
            {
                "item_id": item_id,
                "vector": vector,
                "metadata_json": meta_json,
            }
        ]

        (
            table.merge_insert("item_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(data)
        )

    def upsert_bulk(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return

        # LanceDB's merge_insert accepts a list — we can write the
        # whole batch in one call rather than looping. Validate
        # everything Python-side first so a bad row in the middle
        # doesn't leave a half-written batch on disk.
        for i, spec in enumerate(items):
            for key in ("item_id", "vector"):
                if key not in spec or spec[key] is None:
                    msg = f"upsert_bulk[{i}]: missing required key {key!r}"
                    raise ValueError(msg)

        dimensions = len(items[0]["vector"])
        for i, spec in enumerate(items):
            if len(spec["vector"]) != dimensions:
                msg = (
                    f"upsert_bulk[{i}]: inconsistent dimensions — first row "
                    f"has {dimensions}, this row has {len(spec['vector'])}"
                )
                raise ValueError(msg)
        table = self._ensure_table(dimensions)

        data = [
            {
                "item_id": spec["item_id"],
                "vector": spec["vector"],
                "metadata_json": json.dumps(spec.get("metadata") or {}),
            }
            for spec in items
        ]
        (
            table.merge_insert("item_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(data)
        )
        logger.debug(
            "vectors_upserted_bulk", count=len(items), dimensions=dimensions
        )

    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self._table is None:
            return []

        results = (
            self._table.search(vector)
            .metric(self._metric)
            .limit(top_k if not filters else top_k * 10)
            .to_list()
        )

        scored: list[dict[str, Any]] = []
        for row in results:
            meta = json.loads(row["metadata_json"])

            if filters and not self._matches_filters(meta, filters):
                continue

            # LanceDB _distance for cosine metric is 1 - cosine_similarity
            distance = float(row.get("_distance", 0.0))
            score = 1.0 - distance

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
        if self._table is None:
            return None

        # Use a SQL filter to find the exact item
        safe_id = item_id.replace("'", "''")
        results = (
            self._table.search().where(f"item_id = '{safe_id}'").limit(1).to_list()
        )

        if not results:
            return None

        row = results[0]
        vec = list(row["vector"])
        return {
            "item_id": row["item_id"],
            "vector": [float(v) for v in vec],
            "dimensions": len(vec),
            "metadata": json.loads(row["metadata_json"]),
        }

    def delete(self, item_id: str) -> bool:
        if self._table is None:
            return False

        count_before = self._table.count_rows()
        safe_id = item_id.replace("'", "''")
        self._table.delete(f"item_id = '{safe_id}'")
        count_after = self._table.count_rows()
        return bool(count_after < count_before)

    def count(self) -> int:
        if self._table is None:
            return 0
        return int(self._table.count_rows())

    def close(self) -> None:
        self._table = None
        self._db = None  # type: ignore[assignment]
        logger.info("lancedb_vector_store_closed", uri=self._uri)

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
