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
