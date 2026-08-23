"""DocumentStore — abstract interface for document storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DocumentStore(ABC):
    """Abstract interface for document storage.

    Documents are raw content items (notes, files, transcripts, etc.)
    with metadata and optional full-text search.
    """

    @abstractmethod
    def put(
        self,
        doc_id: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        preserve_updated_at: bool = False,
    ) -> str:
        """Store or update a document.

        Auto-generates an ID if *doc_id* is ``None``.

        Args:
            doc_id: Document id, or ``None`` to auto-generate one.
            content: Document content.
            metadata: Free-form metadata mapping.
            preserve_updated_at: When ``True`` and the row already exists,
                its ``updated_at`` is left untouched instead of being set to
                now. For writers that attach *derived* metadata without
                changing what the document says — the row is not modified in
                any sense a reader cares about, and bumping the stamp would
                silently re-rank it, because ``updated_at`` drives the recency
                decay in
                :class:`~trellis.retrieve.strategies.KeywordSearch`. Ignored on
                insert (a new row's ``updated_at`` is its creation time).

        Returns:
            The document ID.
        """

    @abstractmethod
    def get(self, doc_id: str) -> dict[str, Any] | None:
        """Retrieve a document by ID.

        Returns:
            Document dict ``{doc_id, content, content_hash, metadata,
            created_at, updated_at}`` or ``None``.
        """

    @abstractmethod
    def delete(self, doc_id: str) -> bool:
        """Delete a document.  Returns ``True`` if it existed."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search with optional metadata filters.

        Returns a list of matching documents with a ``rank`` key.
        """

    @abstractmethod
    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Paginated listing of documents."""

    @abstractmethod
    def count(self) -> int:
        """Total number of stored documents."""

    @abstractmethod
    def get_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        """Get a document by its content hash (for deduplication)."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
