"""VectorStore — abstract interface for vector storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class VectorStore(ABC):
    """Abstract interface for vector storage.

    Stores embedding vectors with metadata and supports
    similarity search via cosine distance.
    """

    @abstractmethod
    def upsert(
        self,
        item_id: str,
        vector: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Store or update a vector with optional metadata."""

    @abstractmethod
    def upsert_bulk(
        self,
        items: list[dict[str, Any]],
    ) -> None:
        """Bulk variant of :meth:`upsert`.

        Each entry in ``items`` is a dict with the same fields the
        single-row method accepts:

        - ``item_id`` (``str``, required).
        - ``vector`` (``list[float]``, required).
        - ``metadata`` (``dict | None``, optional; defaults to ``{}``).

        Semantics match :meth:`upsert` per row: existing vectors with
        the same ``item_id`` are replaced; metadata is overwritten.
        Within-batch duplicate ``item_id`` values are **rejected** — see
        :meth:`_pre_validate_bulk_item_ids`. Sequential per-row calls
        with the same ``item_id`` would have collapsed (last-write-wins
        deterministically); bulk paths can't make the same guarantee
        across all backends (Neo4j's UNWIND fires SET twice
        non-deterministically; LanceDB's ``merge_insert`` with
        duplicate sources is implementation-defined). Reject up-front
        rather than ship divergent semantics.

        On backends with network round-trip cost (Neo4j),
        implementations SHOULD consolidate the work into a small
        constant number of round trips per batch — typically one
        UNWIND-style write. On in-process backends a simple loop over
        :meth:`upsert` is acceptable.

        Raises:
            ValueError: with the offending list index when a row's
                vector dimensions don't match the store's configured
                dimensions, when a required field is missing, or when
                two rows in the batch share an ``item_id``.
        """

    def _pre_validate_bulk_item_ids(self, items: list[dict[str, Any]]) -> None:
        """Reject duplicate ``item_id`` values in a bulk batch.

        Implementations call this from :meth:`upsert_bulk` before any
        write so divergent within-batch-duplicate behavior across
        backends doesn't surface as a silent correctness gap. Errors
        are tagged with the offending row index (the second occurrence)
        so callers can map them back to input.
        """
        seen: set[str] = set()
        for i, spec in enumerate(items):
            item_id = spec.get("item_id")
            if item_id is None:
                continue
            if item_id in seen:
                msg = (
                    f"upsert_bulk[{i}]: duplicate item_id {item_id!r} in batch; "
                    "deduplicate before calling — last-write-wins is "
                    "non-deterministic across backends"
                )
                raise ValueError(msg)
            seen.add(item_id)

    @abstractmethod
    def query(
        self,
        vector: list[float],
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Find similar vectors.

        Returns:
            List of ``{item_id, score, metadata}`` sorted by score descending.
        """

    @abstractmethod
    def get(self, item_id: str) -> dict[str, Any] | None:
        """Get a vector by ID.

        Returns:
            ``{item_id, vector, dimensions, metadata}`` or ``None``.
        """

    @abstractmethod
    def delete(self, item_id: str) -> bool:
        """Delete a vector. Returns ``True`` if it existed."""

    @abstractmethod
    def count(self) -> int:
        """Return the total number of stored vectors."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
