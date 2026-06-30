"""No-op :class:`~trellis.stores.base.event_log.EventLog` implementation.

``NullEventLog`` satisfies the EventLog ABC without persisting anything. It
exists so a knowledge-plane-only deployment — one running governed graph /
vector mutations against a remote substrate with no Operational-Plane store —
can wire a real, type-correct event log whose emission is an intentional
no-op, rather than passing ``event_log=None`` and monkey-patching the emit
paths the curate handlers reach through ``registry.operational.event_log``.

Behaviour:

* :meth:`append` drops the event on the floor.
* :meth:`emit` (inherited) still constructs and returns a valid
  :class:`~trellis.stores.base.event_log.Event` so callers that read the
  returned ``event_id`` keep working — only the *persistence* is skipped.
* Read paths (:meth:`get_events`, :meth:`count`) return empty/zero.
* :meth:`has_idempotency_key` is always ``False``: a no-op log holds no
  history, so cross-restart idempotency falls back to the executor's
  in-memory FIFO cache. Knowledge-plane-only mutations are expected to carry
  their own deterministic identity (deterministic ``entity_id`` for nodes;
  ``(source, kind, target)`` SCD-2 dedup for edges).

Registered as the ``null`` ``event_log`` backend in
``StoreRegistry._BUILTIN_BACKENDS``. See issue #196 and
``docs/design/adr-planes-and-substrates.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.stores.base.event_log import EventLog, EventOrder, EventType

if TYPE_CHECKING:
    from datetime import datetime

    from trellis.stores.base.event_log import Event


class NullEventLog(EventLog):
    """An :class:`EventLog` that persists nothing. See module docstring."""

    def append(self, event: Event) -> None:
        """Drop the event — no persistence in knowledge-plane-only mode."""

    def get_events(
        self,
        *,
        event_type: EventType | None = None,
        entity_id: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        order: EventOrder = "asc",
        payload_filters: dict[str, str] | None = None,
    ) -> list[Event]:
        """No events are ever stored, so the query window is always empty."""
        return []

    def count(
        self,
        *,
        event_type: EventType | None = None,
        since: datetime | None = None,
    ) -> int:
        """Nothing is stored, so the count is always zero."""
        return 0

    def has_idempotency_key(self, key: str) -> bool:
        """A no-op log holds no history — every key is unseen."""
        return False

    def close(self) -> None:
        """No resources to release."""


__all__ = ["NullEventLog"]
