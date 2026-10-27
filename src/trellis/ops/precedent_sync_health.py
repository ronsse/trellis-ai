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

**Two events at the same microsecond break toward the failure.** A failed
reinstate outranks a clean one, which outranks a demote, so a tie leaves the
failure open. The error costs are not symmetric: calling a tie a repair or a
demotion reports all-clear for a precedent one axis may still be withholding,
which nobody hears about, and calling it unresolved costs one look at an id.
Real ties are not expected, since each handler stamps its own ``occurred_at``;
the rule is there so the answer does not depend on the order the two scans
happened to be concatenated in.

**A capped demote read cannot vouch for an older failure.** Reads are newest
first, so a truncated ``PRECEDENT_DEMOTED`` scan drops the *oldest* demotes. A
failed reinstate older than the oldest demote the scan kept may have been
superseded by one it dropped, so it is listed in ``uncertain`` rather than as
unresolved: neither "still withholding" nor "repaired" is something the read can
say. A failure at or after that horizon is judged exactly, because every demote
since it was kept. Only the demote scan matters here. A truncated
``PRECEDENT_REINSTATED`` scan drops the oldest reinstates, so the latest event
for a precedent it still shows is never one a newer event hides. The report is
already ``warn`` on any truncation; ``uncertain`` names which precedents the
cap actually put in doubt.

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
#: its snapshot still reads ``demoted``, which the retrieval collect seam
#: withholds (:func:`trellis.retrieve.lifecycle.partition_demoted`, for every
#: strategy), so semantic retrieval skips the precedent.
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
    #: Precedents whose latest lifecycle event is a failed reinstatement older
    #: than the oldest demote a capped ``PRECEDENT_DEMOTED`` read kept, so a
    #: superseding demote cannot be ruled in or out. Neither unresolved nor
    #: repaired; always empty when that read was not truncated. Sorted.
    uncertain: list[str] = Field(default_factory=list)
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


def _tie_rank(event: Event) -> int:
    """Order of two events at the same ``occurred_at``; the higher one wins.

    A failed reinstate (2) outranks a clean one (1), which outranks a demote
    (0), so a tie is reported as the failure still being open. Why, in the
    module docstring.
    """
    if event.event_type == EventType.PRECEDENT_DEMOTED:
        return 0
    failed = _flagged(event, DOCUMENT_SYNC_FAILED_KEY) or _flagged(
        event, VECTOR_SYNC_FAILED_KEY
    )
    return 2 if failed else 1


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
        key=lambda e: (_as_utc(e.occurred_at), _tie_rank(e)),
    ):
        target = _target_id(event)
        if target is not None:
            latest[target] = event

    # The oldest demote a capped read kept. Older than this, a demote may exist
    # that the cap dropped. ``None`` when the read was complete.
    demote_horizon = (
        min(_as_utc(e.occurred_at) for e in demoted.events)
        if demoted.coverage.truncated and demoted.events
        else None
    )

    for target, event in sorted(latest.items()):
        if event.event_type != EventType.PRECEDENT_REINSTATED:
            continue
        document_failed = _flagged(event, DOCUMENT_SYNC_FAILED_KEY)
        vector_failed = _flagged(event, VECTOR_SYNC_FAILED_KEY)
        if not (document_failed or vector_failed):
            continue
        if demote_horizon is not None and _as_utc(event.occurred_at) < demote_horizon:
            report.uncertain.append(target)
            continue
        if document_failed:
            report.document_unresolved.append(target)
        if vector_failed:
            report.vector_unresolved.append(target)
    return report


#: Ids named in one reason line. The full list is on the report.
_REASON_ID_CAP = 5


def _named(ids: list[str]) -> str:
    shown = ", ".join(ids[:_REASON_ID_CAP])
    extra = len(ids) - _REASON_ID_CAP
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def precedent_sync_reasons(report: PrecedentSyncReport) -> list[str]:
    """One ``warn`` reason per axis still withholding a reinstated precedent.

    Both axes *withhold* rather than serve a stale copy. The document row stays
    ``demoted``, and so does the vector row's snapshot: ``SemanticSearch`` spreads
    that snapshot into the item's metadata and the collect seam's
    ``partition_demoted`` withholds it under the reason ``demoted``, whichever
    strategy produced the item. A vector-only failure therefore under-serves
    exactly as a document failure does, and the line says so.

    The document line names the repair, because it is verified: re-issuing
    ``trellis curate reinstate-precedent`` re-stamps a withheld row. The vector
    line does not, because a vector-only failure is refused on re-issue as
    ``target_not_demoted`` and a repair path this module cannot point at is not
    one it will claim.

    A final line names the precedents a capped demote read left in doubt, which
    is not a claim that anything is withheld.
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
            "the vector row's snapshot at demoted (vector_sync_failed), so "
            "semantic retrieval still withholds it for: "
            f"{_named(report.vector_unresolved)}"
        )
    if report.uncertain:
        reasons.append(
            f"{len(report.uncertain)} precedent reinstatement(s) reported a "
            "failed re-stamp older than the capped precedent.demoted read, so "
            "a later demotion that would have superseded it cannot be ruled "
            f"out: {_named(report.uncertain)}"
        )
    return reasons


__all__ = [
    "DOCUMENT_SYNC_FAILED_KEY",
    "VECTOR_SYNC_FAILED_KEY",
    "PrecedentSyncReport",
    "precedent_sync_reasons",
    "summarize_precedent_sync",
]
