"""TraceStore — abstract interface for immutable trace storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from trellis.schemas.trace import Trace


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
        """Query traces with filters."""

    @abstractmethod
    def count(self, *, source: str | None = None, domain: str | None = None) -> int:
        """Count traces with optional filters."""

    @abstractmethod
    def close(self) -> None:
        """Cleanup."""
