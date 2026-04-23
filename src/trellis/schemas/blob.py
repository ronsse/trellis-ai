"""Blob storage schemas — TTL + GC reporting."""

from __future__ import annotations

from datetime import datetime

from trellis.core.base import TrellisModel


class BlobGCReport(TrellisModel):
    """Result of a :meth:`BlobStore.sweep_expired` call.

    Closes Gap 4.4. Blob backends now accept an optional ``expires_at``
    on :meth:`put`; the sweeper drops every object past its TTL and
    reports what it touched. ``dry_run=True`` fills the same shape
    without deleting anything, so operators can preview the impact
    before committing.
    """

    before: datetime
    """Cutoff — blobs with ``expires_at < before`` are eligible."""

    swept: int = 0
    """Number of blobs deleted (or that would be deleted in dry-run)."""
    skipped_no_ttl: int = 0
    """Number of blobs that lack an ``expires_at`` marker and were skipped."""
    skipped_not_yet_expired: int = 0
    """Number of blobs with a future ``expires_at`` that were skipped."""
    errors: int = 0
    """Number of blobs whose TTL parse or delete raised; sweep is fail-soft."""
    dry_run: bool = False
    duration_ms: int = 0
