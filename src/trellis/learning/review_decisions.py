"""Human Review-queue decisions on learning candidates, read back from events.

The Review queue's learning section records two decisions as
``REVIEW_DECISION_RECORDED`` events under ``surface="learning_promotion"``:
``action="promote"`` (``POST /learning/promotions``; ``candidate_ids`` names
the approved rows the run accepted) and ``action="decline"`` (``POST
/learning/candidates/{id}/decline``; ``candidate_ids`` names the one row).

Three display readers share :func:`read_learning_decisions`: the agreement
metric on the loop-health row, the "declined on" stamp in the candidate
listing, and the learning row of the Review backlog. **No gate reads it.**
Auto-promotion, the promote route and every other decision path ignore
declines; ``tests/unit/learning/test_review_decisions.py`` pins that the
auto-promote decision is identical with and without decline events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trellis.stores.base.event_log import EventLog, EventType

#: The ``surface`` the learning-candidate review decisions carry.
LEARNING_REVIEW_SURFACE = "learning_promotion"
#: A reviewer approved the candidate and the promotion run accepted it.
PROMOTE_ACTION = "promote"
#: A reviewer declined the candidate. It stays listed.
DECLINE_ACTION = "decline"

_DECISION_ACTIONS = frozenset({PROMOTE_ACTION, DECLINE_ACTION})

#: Decision events read per call, newest first, so a cap drops the oldest.
DEFAULT_DECISION_SCAN_LIMIT = 5000


@dataclass(frozen=True, slots=True)
class LearningDecisionState:
    """The recorded decisions on one learning candidate.

    ``last_action`` is the latest decision (``"promote"`` or ``"decline"``)
    and ``last_decided_at`` when it was recorded. ``last_declined_at`` is the
    latest decline, kept even when a later promote superseded it.
    """

    last_action: str
    last_decided_at: datetime
    last_declined_at: datetime | None


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


@dataclass(frozen=True, slots=True)
class LearningDecisionRead:
    """The decision states one bounded read produced, and whether it was cut.

    ``truncated`` is true when more than ``limit`` decision events matched,
    so the oldest fell outside the read and a candidate decided only by
    them reads as undecided. ``limit`` is the cap the read used.
    """

    states: dict[str, LearningDecisionState]
    truncated: bool
    limit: int


def read_learning_decisions(
    event_log: EventLog,
    *,
    since: datetime | None = None,
    limit: int = DEFAULT_DECISION_SCAN_LIMIT,
) -> dict[str, LearningDecisionState]:
    """Return the decision state per learning candidate id.

    The states of :func:`read_learning_decisions_bounded`; a caller that
    must say when the read was cut calls that instead.
    """
    return read_learning_decisions_bounded(event_log, since=since, limit=limit).states


def read_learning_decisions_bounded(
    event_log: EventLog,
    *,
    since: datetime | None = None,
    limit: int = DEFAULT_DECISION_SCAN_LIMIT,
) -> LearningDecisionRead:
    """Fold the newest ``limit`` decision events and say whether any were left.

    Reads one event past ``limit`` so a cut is observable rather than
    inferred from a full page: exactly ``limit`` matching events is not
    truncated. Only the newest ``limit`` are folded, oldest to newest, so
    the latest decision per candidate wins. Raises whatever the EventLog
    raises; a caller that must degrade wraps the call and says the source
    was unreadable rather than reading empty.
    """
    fetched = event_log.get_events(
        event_type=EventType.REVIEW_DECISION_RECORDED,
        since=since,
        payload_filters={"surface": LEARNING_REVIEW_SURFACE},
        limit=limit + 1,
        order="desc",
    )
    truncated = len(fetched) > limit
    events = fetched[:limit]
    states: dict[str, LearningDecisionState] = {}
    # Reverse first: the sort is stable, so same-instant events keep
    # the store's own order rather than the newest-first read's.
    oldest_first = events[::-1]
    for event in sorted(oldest_first, key=lambda e: _as_utc(e.occurred_at)):
        payload = event.payload or {}
        action = payload.get("action")
        if action not in _DECISION_ACTIONS:
            continue
        at = _as_utc(event.occurred_at)
        for candidate_id in payload.get("candidate_ids") or []:
            if not isinstance(candidate_id, str) or not candidate_id:
                continue
            previous = states.get(candidate_id)
            declined_at = previous.last_declined_at if previous else None
            if action == DECLINE_ACTION:
                declined_at = at
            states[candidate_id] = LearningDecisionState(
                last_action=str(action),
                last_decided_at=at,
                last_declined_at=declined_at,
            )
    return LearningDecisionRead(states=states, truncated=truncated, limit=limit)
