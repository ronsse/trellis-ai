"""BlobStore — abstract interface for binary/file storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BlobStore(ABC):
    """Abstract interface for blob/file storage.

    Stores raw files and returns URIs for reference in the graph.
    """

    @abstractmethod
    def put(self, key: str, data: bytes, metadata: dict[str, Any] | None = None) -> str:
        """Store a blob. Returns a URI (e.g., file:///... or s3://...)."""

    @abstractmethod
    def get(self, key: str) -> bytes | None:
        """Retrieve blob data by key. Returns None if not found."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a blob. Returns True if it existed."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if a blob exists."""

    @abstractmethod
    def list_keys(self, prefix: str = "") -> list[str]:
        """List blob keys matching prefix."""

    @abstractmethod
    def get_uri(self, key: str) -> str:
        """Get the URI for a stored blob."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
