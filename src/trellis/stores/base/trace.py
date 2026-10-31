"""TraceStore — abstract interface for immutable trace storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from trellis.schemas.trace import Trace


def as_utc(moment: datetime) -> datetime:
    """Return *moment* as an aware UTC datetime; a naive one is read as UTC.

    ``Trace.created_at`` accepts a naive datetime, and Postgres reads a naive
    value as UTC in its default session time zone. Every ``TraceStore`` and its
    callers therefore settle the question the same way, so naive and aware
    values compare by instant instead of raising or sorting as text.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


class TraceStore(ABC):
    """Abstract interface for immutable trace storage."""

    @abstractmethod
    def append(self, trace: Trace) -> str:
        """Store a trace (immutable).

        Returns trace_id. Raises if trace_id already exists.
        """

    @abstractmethod
    def get(self, trace_id: str) -> Trace | None:
        """Get trace by ID."""

    @abstractmethod
    def query(
        self,
        *,
        source: str | None = None,
        domain: str | None = None,
        agent_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[Trace]:
        """Query traces with filters, newest first.

        ``since`` and ``until`` are inclusive and compare by instant: a trace
        stored with a ``-11:00`` or ``+14:00`` offset is found by the moment it
        happened, not by the wall-clock digits it was written with. A naive
        bound or a naive stored ``created_at`` is read as UTC (see
        :func:`as_utc`). The returned trace keeps the ``created_at`` it was
        appended with, so a caller that compares or sorts them must normalise
        with :func:`as_utc` first.
        """

    @abstractmethod
    def count(self, *, source: str | None = None, domain: str | None = None) -> int:
        """Count traces with optional filters."""

    @abstractmethod
    def count_by_domain(self) -> dict[str, int]:
        """Count traces grouped by ``domain``, keyed by the stored value.

        A trace with no domain is omitted, not keyed under ``None`` — a
        caller building a domain filter wants only values it could pass
        back into ``query(domain=...)``. Backs a server-side-count filter
        populated from the whole store rather than one fetched page.
        """

    @abstractmethod
    def close(self) -> None:
        """Cleanup."""
