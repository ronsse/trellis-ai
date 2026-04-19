"""TunerStateStore — mutable working state for the self-learning loop.

Holds tuner proposals, canary assignments, and cursors.  Distinct from
:class:`ParameterStore` which holds immutable versioned snapshots, and
from :class:`OutcomeStore` which holds immutable call-level signals.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from trellis.schemas.parameters import ParameterProposal


class TunerStateStore(ABC):
    """Working-state store for tuners — mutable, low-volume.

    Entries here are not audit-worthy individually; their effects
    (promotion, rejection) get recorded in the EventLog.  Keeping them
    in a separate store avoids growing the audit log with transient
    state and lets tuners iterate cursors without touching governed
    data.
    """

    @abstractmethod
    def put_proposal(self, proposal: ParameterProposal) -> ParameterProposal:
        """Persist (or replace) a proposal by ``proposal_id``."""

    @abstractmethod
    def get_proposal(self, proposal_id: str) -> ParameterProposal | None:
        """Fetch a proposal by id."""

    @abstractmethod
    def list_proposals(
        self,
        *,
        tuner: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ParameterProposal]:
        """List proposals with optional filters."""

    @abstractmethod
    def update_status(
        self,
        proposal_id: str,
        status: str,
        *,
        notes: str | None = None,
    ) -> ParameterProposal | None:
        """Update a proposal's status.  Returns the updated record or ``None``."""

    @abstractmethod
    def get_cursor(self, tuner: str) -> str | None:
        """Return the last processed outcome cursor for a tuner, or ``None``."""

    @abstractmethod
    def set_cursor(self, tuner: str, cursor: str) -> None:
        """Record the last processed outcome cursor for a tuner."""

    @abstractmethod
    def close(self) -> None:
        """Cleanup."""
