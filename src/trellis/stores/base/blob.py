"""BlobStore — abstract interface for binary/file storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trellis.schemas.blob import BlobGCReport
    from trellis.stores.base.event_log import EventLog

#: Reserved metadata key that holds the blob's TTL (ISO 8601 UTC). Kept
#: as a named constant so both backends agree and tests can reach it
#: without magic strings.
BLOB_EXPIRES_AT_KEY = "trellis_expires_at"


class BlobStore(ABC):
    """Abstract interface for blob/file storage.

    Stores raw files and returns URIs for reference in the graph.

    Each backend exposes optional per-blob TTLs via the ``expires_at``
    argument on :meth:`put` and a :meth:`sweep_expired` GC sweep
    (Gap 4.4). TTLs are reserved metadata; callers can still pass
    application metadata alongside.
    """

    @abstractmethod
    def put(
        self,
        key: str,
        data: bytes,
        metadata: dict[str, Any] | None = None,
        *,
        expires_at: datetime | None = None,
    ) -> str:
        """Store a blob. Returns a URI (e.g., file:///... or s3://...).

        When ``expires_at`` is supplied the blob becomes eligible for
        :meth:`sweep_expired`. The timestamp is persisted under the
        reserved metadata key :data:`BLOB_EXPIRES_AT_KEY`; callers may
        still supply their own ``metadata`` dict and the two merge
        with the TTL taking precedence on key collision.
        """

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

    def sweep_expired(
        self,
        before: datetime | None = None,
        *,
        prefix: str = "",
        dry_run: bool = False,
        event_log: EventLog | None = None,
    ) -> BlobGCReport:
        """Drop blobs whose ``expires_at`` is strictly earlier than ``before``.

        Closes Gap 4.4 — manual GC for blob stores that would otherwise
        leak extraction artifacts indefinitely. Scope is deliberately
        narrow: time-based TTL only, triggered by the caller. Operators
        who prefer bucket-level lifecycle rules (S3) can continue to
        use those in parallel; this sweep exists so deployments with
        shorter TTLs (or no infrastructure-level policy) aren't stuck
        with manual cleanup.

        Args:
            before: Cutoff timestamp. Blobs with ``expires_at < before``
                are swept. Defaults to :func:`utc_now` when ``None``.
            prefix: Optional key prefix to restrict the sweep scope.
                Defaults to ``""`` (whole store).
            dry_run: When ``True``, return counts of blobs that *would*
                be deleted without modifying the store.
            event_log: Optional audit destination. When provided, a
                :attr:`~trellis.stores.base.event_log.EventType.BLOB_GC_SWEPT`
                event is emitted with the report payload. Dry runs emit
                the event too, with ``dry_run=True``, so previews are
                observable.

        Returns:
            :class:`~trellis.schemas.blob.BlobGCReport` with counts of
            swept / skipped-no-ttl / skipped-not-yet-expired / errored
            blobs and run metadata.

        Raises:
            NotImplementedError: For backends that have not opted into
                sweep support.
        """
        msg = f"{type(self).__name__} does not implement sweep_expired"
        raise NotImplementedError(msg)
