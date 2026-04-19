"""ParameterStore — abstract interface for versioned parameter snapshots."""

from __future__ import annotations

from abc import ABC, abstractmethod

from trellis.schemas.parameters import ParameterScope, ParameterSet


class ParameterStore(ABC):
    """Versioned, scoped parameter snapshots for tuneable components.

    Each ``put`` creates a new immutable snapshot at a monotonic
    ``params_version``.  Resolution walks the precedence chain
    (narrowest scope first, then backoff) until a snapshot is found.
    ``get_active`` caches the latest snapshot per scope key.
    """

    @abstractmethod
    def put(self, params: ParameterSet) -> ParameterSet:
        """Persist a new parameter snapshot.  Returns the stored copy."""

    @abstractmethod
    def get(self, params_version: str) -> ParameterSet | None:
        """Fetch a specific snapshot by version id."""

    @abstractmethod
    def get_active(self, scope: ParameterScope) -> ParameterSet | None:
        """Fetch the latest snapshot for an exact scope, or ``None``.

        Does not walk the precedence chain — use :meth:`resolve` for that.
        """

    @abstractmethod
    def resolve(self, scope: ParameterScope) -> ParameterSet | None:
        """Walk the precedence chain to find the best matching snapshot.

        Order:

        1. ``(component_id, domain, intent_family, tool_name)``
        2. ``(component_id, domain, intent_family)``
        3. ``(component_id, domain)``
        4. ``(component_id, intent_family)``
        5. ``(component_id)``

        Returns the first snapshot found, or ``None`` if no snapshot
        exists at any level.
        """

    @abstractmethod
    def list_versions(
        self,
        scope: ParameterScope | None = None,
        *,
        limit: int = 100,
    ) -> list[ParameterSet]:
        """List historical snapshots, optionally filtered by scope."""

    @abstractmethod
    def close(self) -> None:
        """Cleanup."""
