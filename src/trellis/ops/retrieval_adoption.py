"""Whether agents retrieve at all: the ``retrieval`` section of ``analyze health``.

Every other serve signal in ``trellis analyze health`` starts from a pack that
exists. A deployment whose agents never call retrieval has no packs, so those
signals read as an empty window rather than as a failure. This section starts
one step earlier, from the sessions the capture sweep parsed: of the sessions
captured in the window, how many were served at least one pack.

It adds no probe. The signal is the ``CAPTURE_SESSION_PACKS`` event the sweep
already writes, one per session, keyed on the session id, whose ``pack_ids``
payload names the packs the session's transcript shows it receiving. A
session's join is re-written when its transcript grows, so a session's latest
event in the window is its join and earlier ones are not counted again.

**What counts.** A session counts as having retrieved when its latest join
names at least one pack id. A pack the holdout withheld still counts: the agent
asked and was answered, and this section measures asking. A sub-agent's
transcript has its own join and is one session here, as it is in the capture
sweep.

**Thresholds are constants, not knobs.** :data:`RETRIEVAL_ADOPTION_FLOOR` and
:data:`RETRIEVAL_ADOPTION_MIN_SESSIONS` are the two numbers this section
judges by. They are module constants, with the reasoning below, because a
health threshold an operator can tune per deployment stops being a comparable
reading across deployments.

* Below ``RETRIEVAL_ADOPTION_MIN_SESSIONS`` captured sessions the ratio is
  reported as ``insufficient_data`` and never warns: one session without a
  retrieval in a window of three is a 67% reading with no information in it.
* At or above the minimum, an adoption ratio below
  ``RETRIEVAL_ADOPTION_FLOOR`` is ``warn``. The ratio is the share of captured
  sessions, not of all sessions: a deployment whose capture is not running has
  no sessions to count and lands in ``insufficient_data``, which is the honest
  reading (capture's own state is reported by the ``capture`` section).

A capped read drops the *oldest* joins, and the report carries the read's
``scan`` so ``analyze health`` states the truncation as it does for every other
section.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    Event,
    EventLog,
    EventType,
    ScanCoverage,
    resolve_scan_window,
    scan_events,
)

#: Adoption below this share of captured sessions is ``warn``. A fixed
#: constant: see the module docstring for why it is not a setting.
RETRIEVAL_ADOPTION_FLOOR = 0.5

#: Fewer captured sessions than this and the ratio is ``insufficient_data``.
RETRIEVAL_ADOPTION_MIN_SESSIONS = 10

#: ``CAPTURE_SESSION_PACKS`` payload key naming the packs a session received.
PACK_IDS_KEY = "pack_ids"

RetrievalAdoptionState = Literal["ok", "warn", "insufficient_data"]


class RetrievalAdoptionReport(TrellisModel):
    """What the window's ``CAPTURE_SESSION_PACKS`` joins say about adoption."""

    window_days: int
    #: ``ok`` at or above the floor, ``warn`` below it, and
    #: ``insufficient_data`` below the session minimum, whatever the ratio.
    state: RetrievalAdoptionState = "insufficient_data"
    #: Distinct sessions with a join in the window.
    sessions: int = 0
    #: Of those, the sessions whose latest join names at least one pack.
    sessions_with_packs: int = 0
    #: ``sessions_with_packs / sessions``, or ``None`` when ``state`` is
    #: ``insufficient_data``: a ratio over too few sessions is withheld, not
    #: reported with a caveat.
    adoption: float | None = None
    floor: float = RETRIEVAL_ADOPTION_FLOOR
    min_sessions: int = RETRIEVAL_ADOPTION_MIN_SESSIONS
    scan: ScanCoverage = Field(default_factory=ScanCoverage)


def _names_a_pack(payload: dict[str, Any] | None) -> bool:
    """Whether a join payload lists at least one non-empty pack id."""
    ids = (payload or {}).get(PACK_IDS_KEY)
    if not isinstance(ids, list):
        return False
    return any(isinstance(item, str) and item.strip() for item in ids)


def summarize_retrieval_adoption(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = DEFAULT_SCAN_LIMIT,
    now: datetime | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> RetrievalAdoptionReport:
    """Retrieval adoption over the trailing *days* of captured sessions.

    ``now`` pins the window's end for a caller that needs the edge exact; the
    window opens at ``now - days``. ``since`` and ``until`` name a window that
    is not trailing (a calendar week); both default to ``None``, which leaves
    the window as it was.
    """
    since, until = resolve_scan_window(days, since=since, until=until, now=now)
    scan = scan_events(
        event_log,
        event_type=EventType.CAPTURE_SESSION_PACKS,
        since=since,
        until=until,
        limit=limit,
    )
    # Ascending, so a later event for the same session overwrites the earlier:
    # a session's latest join is its join.
    latest: dict[str, Event] = {}
    for event in scan.events:
        if event.entity_id:
            latest[event.entity_id] = event
    sessions = len(latest)
    with_packs = sum(1 for event in latest.values() if _names_a_pack(event.payload))
    if sessions < RETRIEVAL_ADOPTION_MIN_SESSIONS:
        return RetrievalAdoptionReport(
            window_days=days,
            sessions=sessions,
            sessions_with_packs=with_packs,
            scan=scan.coverage,
        )
    adoption = with_packs / sessions
    return RetrievalAdoptionReport(
        window_days=days,
        state="warn" if adoption < RETRIEVAL_ADOPTION_FLOOR else "ok",
        sessions=sessions,
        sessions_with_packs=with_packs,
        adoption=adoption,
        scan=scan.coverage,
    )


def retrieval_adoption_reasons(report: RetrievalAdoptionReport) -> list[str]:
    """One ``warn`` reason when adoption is below the floor, else none.

    Worded as a measurement of sessions, not a claim about agents: a join that
    names no pack is also what a transcript with an unparseable retrieval
    result looks like.
    """
    if report.state != "warn" or report.adoption is None:
        return []
    return [
        (
            f"retrieval adoption {report.adoption:.0%} "
            f"({report.sessions_with_packs}/{report.sessions} captured sessions "
            f"received a pack in {report.window_days} day(s)) is below the "
            f"{report.floor:.0%} floor"
        )
    ]


__all__ = [
    "RETRIEVAL_ADOPTION_FLOOR",
    "RETRIEVAL_ADOPTION_MIN_SESSIONS",
    "RetrievalAdoptionReport",
    "RetrievalAdoptionState",
    "retrieval_adoption_reasons",
    "summarize_retrieval_adoption",
]
