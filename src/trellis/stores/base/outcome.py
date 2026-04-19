"""OutcomeStore — abstract interface for per-call OutcomeEvent persistence.

High-volume ops-tier storage.  Distinct from the audit EventLog: raw
call-level outcomes do NOT go through ``EventLog.emit``.  The EventLog
receives curated, low-volume governance events only (``PARAMS_UPDATED``,
``TUNER_PROPOSAL_CREATED``, ``CANARY_COMPLETED``, ``PRECEDENT_PROMOTED``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from trellis.schemas.outcome import OutcomeEvent


class OutcomeStore(ABC):
    """Append-only store for :class:`OutcomeEvent` records.

    Implementations may roll up older outcomes (>30d by default) into
    aggregate counters, but callers query through the same interface
    and the store chooses whether to hit raw rows or rollups.
    """

    @abstractmethod
    def append(self, outcome: OutcomeEvent) -> None:
        """Append a single outcome (immutable)."""

    @abstractmethod
    def append_many(self, outcomes: list[OutcomeEvent]) -> int:
        """Append multiple outcomes in one transaction.  Returns count."""

    @abstractmethod
    def query(
        self,
        *,
        component_id: str | None = None,
        domain: str | None = None,
        intent_family: str | None = None,
        tool_name: str | None = None,
        phase: str | None = None,
        agent_role: str | None = None,
        params_version: str | None = None,
        run_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[OutcomeEvent]:
        """Query outcomes filtered by any combination of axes."""

    @abstractmethod
    def count(
        self,
        *,
        component_id: str | None = None,
        domain: str | None = None,
        intent_family: str | None = None,
        tool_name: str | None = None,
        phase: str | None = None,
        params_version: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """Count outcomes matching the filters."""

    @abstractmethod
    def close(self) -> None:
        """Cleanup."""
