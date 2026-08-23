"""Re-emission cooldown shared by the surface-only promotion loops.

Both promotion analyzers — :mod:`trellis.learning.schema_evolution` (open-string
node/edge types) and :mod:`trellis.learning.tag_evolution` (tag keywords) — are
*surface-only*: they emit a candidate event for a human to act on and mutate
nothing themselves. That makes idempotency across runs the load-bearing
property. Without it a nightly analyzer re-emits the same candidate every night
and the signal drowns in its own repetition; with a naive "emit once ever" rule
a candidate that has since doubled its evidence stays silent.

The rule both use (``adr-well-known-promotion-loop.md`` §2.3):

* no prior emission → emit;
* evidence grew by ≥ :data:`COOLDOWN_GROWTH_RATIO` since the last emission →
  emit, and count the recurrence;
* inside the cooldown window with no material growth → suppress;
* past the cooldown window → emit (a persistent candidate is a persistent
  signal, §4.2).

Extracted here so the two analyzers cannot drift apart on it — the alternative
was one importing the other's privates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger(__name__)

#: Growth in the evidence count that re-surfaces a candidate regardless of its
#: cooldown. Per ADR §2.3.
COOLDOWN_GROWTH_RATIO: float = 0.20

#: Default cap on the candidate-event history scanned to rebuild prior state.
DEFAULT_PRIOR_SCAN_LIMIT: int = 5_000


@dataclass(frozen=True, slots=True)
class PriorCandidate:
    """Snapshot of the most recent emission for one ``candidate_id``."""

    emitted_at: datetime
    count: int
    recurrence_count: int


def load_prior_candidates(
    event_log: EventLog,
    *,
    event_type: EventType,
    count_key: str,
    scan_limit: int = DEFAULT_PRIOR_SCAN_LIMIT,
) -> dict[str, PriorCandidate]:
    """Index the latest candidate event per ``candidate_id``.

    Payload predicate push-down is awkward across backends, so this reads
    ``order="desc"`` and filters Python-side; the unique candidate space is
    bounded by the sample the analyzer would produce anyway.

    Args:
        event_log: Where candidate events live.
        event_type: The candidate event type to scan.
        count_key: Payload key holding the evidence count that growth is
            measured against (``"count"`` for schema evolution, ``"support"``
            for tag evolution).
        scan_limit: How much history to read.

    Returns:
        ``candidate_id -> PriorCandidate`` for the most recent emission of each.
    """
    events = event_log.get_events(
        event_type=event_type,
        limit=scan_limit,
        order="desc",
    )
    out: dict[str, PriorCandidate] = {}
    for event in events:
        cid = event.payload.get("candidate_id")
        # ``order="desc"`` means the first sighting per id is the freshest;
        # later ones are history and must not overwrite it.
        if not isinstance(cid, str) or cid in out:
            continue
        raw_count = event.payload.get(count_key)
        raw_recurrence = event.payload.get("recurrence_count")
        out[cid] = PriorCandidate(
            emitted_at=event.occurred_at,
            count=int(raw_count) if isinstance(raw_count, int | float) else 0,
            recurrence_count=int(raw_recurrence)
            if isinstance(raw_recurrence, int | float)
            else 0,
        )
    return out


def cooldown_blocks_emission(
    *,
    candidate_id: str,
    current_count: int,
    prior: PriorCandidate | None,
    cooldown_days: int,
    now: datetime,
    log_event: str = "promotion.candidate_suppressed_cooldown",
) -> tuple[bool, datetime | None, int]:
    """Return ``(blocked, cooldown_until, recurrence_count)``.

    See the module docstring for the rule. ``log_event`` names the structlog
    event so a suppressed candidate is attributable to the analyzer that
    suppressed it.
    """
    if prior is None:
        return False, None, 0

    growth_ratio = (
        (current_count - prior.count) / prior.count if prior.count > 0 else 1.0
    )
    if growth_ratio >= COOLDOWN_GROWTH_RATIO:
        return False, None, prior.recurrence_count + 1

    cooldown_until = prior.emitted_at + timedelta(days=cooldown_days)
    if now < cooldown_until:
        logger.info(
            log_event,
            candidate_id=candidate_id,
            cooldown_until=cooldown_until.isoformat(),
            current_count=current_count,
            prior_count=prior.count,
        )
        return True, cooldown_until, prior.recurrence_count

    return False, None, prior.recurrence_count + 1


__all__ = [
    "COOLDOWN_GROWTH_RATIO",
    "DEFAULT_PRIOR_SCAN_LIMIT",
    "PriorCandidate",
    "cooldown_blocks_emission",
    "load_prior_candidates",
]
