"""Precedent reinstatements whose document or vector re-stamp did not land.

``precedent.reinstate`` writes the node first and then re-stamps the
precedent's document row (keyword axis) and vector row (semantic axis). Both
re-stamps are fail-soft once the node is current, so a store outage leaves the
reinstatement standing and one axis still withholding the precedent. The handler
says so twice, on the ``CommandResult`` warnings and on the
``PRECEDENT_REINSTATED`` event (``document_sync_failed``, ``vector_sync_failed``),
and the first reader of either is a human who happened to read the result. This
module is the reader of the event: ``trellis analyze health`` carries it, so a
reinstatement that is quietly not serving shows up without anyone replaying the
log. It adds no probe; the signal is the event the handler already writes.

**A failure counts until something repairs it, not for as long as the window
holds it.** Per target, the latest lifecycle event decides:

* a later ``PRECEDENT_REINSTATED`` without the flag repairs it. Re-issuing the
  reinstate does that for a document failure (the node is already current and
  the row is still ``demoted``, which the handler accepts and answers with a
  ``document_resync_only`` event), and that re-issue re-syncs the vector row
  too;
* a later ``PRECEDENT_DEMOTED`` supersedes it. Demotion re-stamps the row and
  the vector copy as ``demoted``, so there is nothing left to be stale. A
  failure of the demotion's own re-stamp is the demote handler's to report and
  is not read here.

Without the second rule a reinstate whose stamp failed and that was then demoted
again would warn until its event aged out of the window, for a state that is
consistent.

**The window is the limit of what this can see.** A failed reinstatement older
than ``days`` is not read, so an unrepaired one stops warning when its event
leaves the window while its axis is still withholding it. That is the same bound
every reader in :mod:`trellis.ops.write_health` has, and it is why the report
carries the ``scan`` coverage and why the repair is worth doing inside the
window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    Event,
    EventLog,
    EventType,
    ScanCoverage,
    _as_utc,
    merge_coverage,
    scan_events,
)

#: ``PRECEDENT_REINSTATED`` payload key: the document row was not re-stamped, so
#: keyword retrieval keeps withholding the precedent.
DOCUMENT_SYNC_FAILED_KEY = "document_sync_failed"

#: ``PRECEDENT_REINSTATED`` payload key: the vector row was not re-stamped, so
#: semantic retrieval keeps the pre-reinstate snapshot.
VECTOR_SYNC_FAILED_KEY = "vector_sync_failed"


class PrecedentSyncReport(TrellisModel):
    """What the window's ``PRECEDENT_REINSTATED`` events say about re-stamping."""

    window_days: int
    #: ``PRECEDENT_REINSTATED`` events read, repaired failures included.
    reinstatements: int = 0
    #: Of those, the ones that reported a failed document or vector re-stamp,
    #: whether or not it has been repaired since. The rate is the signal that a
    #: store is flaky; the lists below are the ones still open.
    failed_reinstatements: int = 0
    #: Precedents whose latest lifecycle event is a reinstatement that left the
    #: document row withheld. Sorted.
    document_unresolved: list[str] = Field(default_factory=list)
    #: Precedents whose latest lifecycle event is a reinstatement that left the
    #: vector row stale. Sorted.
    vector_unresolved: list[str] = Field(default_factory=list)
    #: ``occurred_at`` of the latest failed reinstatement read, or ``None``.
    latest_failure_at: datetime | None = None
    scan: ScanCoverage = Field(default_factory=ScanCoverage)


def _target_id(event: Event) -> str | None:
    """The precedent an event is about: the payload's id, else the entity id."""
    target = event.payload.get("target_id") or event.entity_id
    return target if isinstance(target, str) and target else None


def _flagged(event: Event, key: str) -> bool:
    """Whether the handler set ``key``, which it writes as ``True`` or not at all."""
    return event.payload.get(key) is True


def summarize_precedent_sync(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> PrecedentSyncReport:
    """Reinstatements in the trailing *days* whose re-stamp is still outstanding."""
    since = datetime.now(tz=UTC) - timedelta(days=days)
    reinstated = scan_events(
        event_log,
        event_type=EventType.PRECEDENT_REINSTATED,
        since=since,
        limit=limit,
    )
    demoted = scan_events(
        event_log,
        event_type=EventType.PRECEDENT_DEMOTED,
        since=since,
        limit=limit,
    )
    report = PrecedentSyncReport(
        window_days=days,
        reinstatements=len(reinstated.events),
        scan=merge_coverage(reinstated.coverage, demoted.coverage),
    )

    for event in reinstated.events:
        if _flagged(event, DOCUMENT_SYNC_FAILED_KEY) or _flagged(
            event, VECTOR_SYNC_FAILED_KEY
        ):
            report.failed_reinstatements += 1
            stamp = _as_utc(event.occurred_at)
            if report.latest_failure_at is None or stamp > report.latest_failure_at:
                report.latest_failure_at = stamp

    # The latest lifecycle event per precedent decides; see the module docstring.
    latest: dict[str, Event] = {}
    for event in sorted(
        [*reinstated.events, *demoted.events],
        key=lambda e: _as_utc(e.occurred_at),
    ):
        target = _target_id(event)
        if target is not None:
            latest[target] = event

    for target, event in sorted(latest.items()):
        if event.event_type != EventType.PRECEDENT_REINSTATED:
            continue
        if _flagged(event, DOCUMENT_SYNC_FAILED_KEY):
            report.document_unresolved.append(target)
        if _flagged(event, VECTOR_SYNC_FAILED_KEY):
            report.vector_unresolved.append(target)
    return report


#: Ids named in one reason line. The full list is on the report.
_REASON_ID_CAP = 5


def _named(ids: list[str]) -> str:
    shown = ", ".join(ids[:_REASON_ID_CAP])
    extra = len(ids) - _REASON_ID_CAP
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def precedent_sync_reasons(report: PrecedentSyncReport) -> list[str]:
    """One ``warn`` reason per axis still withholding or serving a stale precedent.

    The document line names the repair, because it is verified: re-issuing
    ``trellis curate reinstate-precedent`` re-stamps a withheld row. The vector
    line does not, because a vector-only failure is refused on re-issue as
    ``target_not_demoted`` and a repair path this module cannot point at is not
    one it will claim.
    """
    reasons: list[str] = []
    if report.document_unresolved:
        reasons.append(
            f"{len(report.document_unresolved)} precedent reinstatement(s) left "
            "the document row withheld (document_sync_failed), so keyword "
            "retrieval still skips it; re-issue `trellis curate "
            f"reinstate-precedent` for: {_named(report.document_unresolved)}"
        )
    if report.vector_unresolved:
        reasons.append(
            f"{len(report.vector_unresolved)} precedent reinstatement(s) left "
            "the vector row stale (vector_sync_failed), so semantic retrieval "
            "still serves the pre-reinstate snapshot for: "
            f"{_named(report.vector_unresolved)}"
        )
    return reasons


__all__ = [
    "DOCUMENT_SYNC_FAILED_KEY",
    "VECTOR_SYNC_FAILED_KEY",
    "PrecedentSyncReport",
    "precedent_sync_reasons",
    "summarize_precedent_sync",
]
