"""Capture coverage — what fraction of eligible sessions produced a memory.

#332 fixed a reader rule that was discarding 61% of transcripts. Nothing
measured that, so it was found by reading code rather than by an alarm, and
the next regression of the same family would be equally invisible. #255 is
the cautionary case: session auto-capture shipped in July and did not write a
single memory until August — blocked turn ordering, a context-window coupling
that made the local judge fabricate, and ``non_derivable=False`` on 9 of 9
candidates so the worthiness gate rejected everything. It reported success
throughout, because the only thing anyone could observe was that the sweep
exited zero.

This module is the instrument those two failures needed. Three commitments
shape it.

**A ratio is never reported without the population it came from.** Every
rate carries its ``n``, and a rate below :data:`MIN_ELIGIBLE_SESSIONS` is
``None`` with a ``suppressed_reason`` naming why — the
:mod:`trellis.retrieve.pack_value` posture. A single session producing
nothing is not a 0%-coverage system.

**Absence of data is reported as absence, never as zero.** This is the whole
design. A coverage number that reads 0.0 when the metric has never been
deployed, when the sweep has stopped running, and when the sweep runs but
adjudicates nothing is a metric that cannot do its job — those three call for
completely different fixes (ship it / restart it / debug the pipeline), and
collapsing them into one low number is how a missing deployment gets filed as
a code defect. :class:`CaptureCoverageReport.state` separates them, and it is
earned from evidence in the log rather than asserted:

===============  ===========================================================
``state``        What the log showed
===============  ===========================================================
``unobserved``   No sweep has *ever* reported, inside the window or before
                 it. Either the emitting code is not deployed on whatever
                 writes to this store, or no sweep has run at all. **No
                 ratio is reported.**
``stale``        Sweeps reported before the window but none inside it. The
                 pipeline existed and stopped. ``last_sweep_at`` says when.
``degraded``     Sweeps ran inside the window and adjudicated nothing —
                 every session was skipped, sampled out, or lost to an
                 unreachable judge. ``degraded_reason`` names the stage.
``measured``     Sweeps ran and adjudicated sessions. The ratio means what
                 it says.
===============  ===========================================================

Deliberately **not** keyed on ``write_provenance``. That is the obvious way
to answer "is the current build deployed", and on this deployment it is a
trap: ``hatch-vcs`` stamps at install time, so the host's editable install
brands every write with a specific, confident, *wrong* commit
(`#348 <https://github.com/ronsse/trellis-ai/issues/348>`_), and the capture
sweep is a host-run worker. A liveness signal built on a known-false input
would be worse than none. The evidence used here is the sweep's own report —
a thing that either exists or does not.

**The denominator is the pipeline's own eligibility rule, not a new one.**
Eligible means *triggered*: parsed, passed
:func:`~trellis_workers.session_capture.gating.should_distill`, and handed to
a reachable judge. Inventing a fresh notion of "a session that should have
produced a memory" is precisely how a metric ends up wired to a constant, so
this reuses the deployed deterministic gate and reports the stages it drops
sessions at rather than second-guessing them.

Two populations are deliberately outside the denominator, because neither
ever had a chance to produce anything: sessions **sampled out** (a cost
decision — including them would cap coverage at ``1/sample_denominator`` and
make the metric move when the operator turns a knob) and sessions lost to an
**unreachable judge** (left un-watermarked for retry). Both are still
reported, as counts, in :class:`CaptureFunnel`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventLog,
    EventType,
    ScanCoverage,
    merge_coverage,
    scan_events,
)

logger = structlog.get_logger(__name__)

#: Source string the capture worker stamps on everything it emits.
CAPTURE_SOURCE = "worker:session-capture"

#: Below this many eligible sessions the rate is suppressed rather than
#: reported. Matches :data:`trellis.retrieve.pack_value.MIN_ATTRIBUTED_PACKS`
#: and ``write_health._MIN_ATTEMPTS_FOR_RATE`` — one deployment, one idea of
#: what counts as too thin to divide.
MIN_ELIGIBLE_SESSIONS = 5

#: Reason slug when the eligible population is below the minimum.
SUPPRESSED_THIN_SAMPLE = "below_min_eligible_sessions"
#: Reason slug when no sweep has ever reported.
SUPPRESSED_UNOBSERVED = "no_capture_sweep_observed"
#: Reason slug when sweeps exist but none landed in the window.
SUPPRESSED_STALE = "no_capture_sweep_in_window"
#: Reason slug when sweeps ran but adjudicated no sessions at all.
SUPPRESSED_DEGRADED = "no_eligible_sessions_adjudicated"

#: How far back to look for *any* sweep when the window holds none. Only
#: distinguishes "never ran" from "stopped running", so it is generous.
UNOBSERVED_LOOKBACK_DAYS = 365

#: Per-event-type read cap, applied through
#: :func:`~trellis.stores.base.event_log.scan_events` so the newest events
#: survive it and a capped report says so (#374). ``last_sweep_at`` is the
#: field that cared most: under the old ascending default a truncated read
#: made it the newest sweep *of the oldest slice*, which is a plausible
#: timestamp for a pipeline that has since stopped — the exact reading
#: this module exists to distinguish.
_DEFAULT_EVENT_LIMIT = DEFAULT_SCAN_LIMIT


class CaptureFunnel(TrellisModel):
    """Where sessions went, summed over every sweep in the window.

    Sums, not averages: sweeps overlap (a session skipped by the watermark
    today was adjudicated on an earlier one), so only the drop *stages* are
    meaningfully additive. ``sessions_seen`` in particular is dominated by
    watermark skips and is **not** a coverage denominator — read it as sweep
    volume.
    """

    sweeps: int = 0
    sessions_seen: int = 0
    sessions_skipped_watermark: int = 0
    sessions_skipped_ephemeral: int = 0
    sessions_parsed: int = 0
    #: Parsed to zero natural-language turns. The #332 detector: a reader
    #: regression lands here, and used to land in ``sessions_sampled_out``
    #: where it looked like a sampling decision.
    sessions_skipped_empty: int = 0
    sessions_sampled_out: int = 0
    sessions_triggered: int = 0
    sessions_judge_unavailable: int = 0
    sessions_with_memory: int = 0
    memories_written: int = 0
    candidates_distilled: int = 0
    candidates_rejected_worthiness: int = 0
    candidates_rejected_injection: int = 0
    candidates_blocked_scan: int = 0


class CaptureCoverageReport(TrellisModel):
    """Coverage of session capture over one window, with its provenance."""

    window_days: int
    #: ``unobserved`` | ``stale`` | ``degraded`` | ``measured``. See the
    #: module docstring — these are four different problems, not four
    #: shades of one number.
    state: str = "unobserved"
    #: Sessions the pipeline adjudicated: parsed, past ``should_distill``,
    #: judge reachable. The denominator.
    eligible_sessions: int = 0
    #: Of those, the ones that yielded at least one memory past every gate.
    sessions_with_memory: int = 0
    #: ``sessions_with_memory / eligible_sessions``. ``None`` — never 0.0 —
    #: whenever the population cannot support a rate.
    capture_rate: float | None = None
    #: Why :attr:`capture_rate` is ``None``, when it is.
    suppressed_reason: str = ""
    #: Which funnel stage consumed everything, when ``state="degraded"``.
    degraded_reason: str = ""
    #: Most recent sweep in the window, or the most recent one found at all
    #: when the window holds none.
    last_sweep_at: datetime | None = None
    funnel: CaptureFunnel = Field(default_factory=CaptureFunnel)
    #: Distinct sessions observed writing a memory in the window, derived
    #: independently from ``MEMORY_STORED`` metadata rather than from the
    #: sweep's self-report. A cross-check: the two count different things
    #: (this one sees only sessions whose memory was *newly stored*, and it
    #: keeps working on a deployment whose sweeps predate the funnel event),
    #: so they are reported side by side instead of one overwriting the
    #: other. A large, persistent gap means the sweep's arithmetic and the
    #: write path disagree.
    sessions_with_stored_memory: int = 0
    dry_run_sweeps_excluded: int = 0
    #: Coverage of the EventLog reads behind this report (#374).
    scan: ScanCoverage = Field(default_factory=ScanCoverage)
    notes: list[str] = Field(default_factory=list)


def _funnel_from_events(events: list[Any]) -> tuple[CaptureFunnel, int]:
    """Sum sweep payloads into a funnel; also count dry runs skipped."""
    funnel = CaptureFunnel()
    dry_runs = 0
    fields = set(CaptureFunnel.model_fields) - {"sweeps"}
    for event in events:
        payload = event.payload or {}
        if payload.get("dry_run"):
            # A dry run plans the sweep without writing, so its
            # ``sessions_with_memory`` is structurally zero. Folding it in
            # would drag coverage down in proportion to how often an
            # operator debugs the sweep.
            dry_runs += 1
            continue
        funnel.sweeps += 1
        for name in fields:
            value = payload.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(funnel, name, getattr(funnel, name) + value)
    return funnel, dry_runs


def _degraded_reason(funnel: CaptureFunnel) -> str:
    """Name the stage that consumed every session, worst-first."""
    if funnel.sessions_judge_unavailable > 0:
        return (
            f"{funnel.sessions_judge_unavailable} session(s) left unjudged — "
            "the distillation judge was unreachable, so nothing could be "
            "captured; check the llm: block and the local model endpoint"
        )
    if funnel.sessions_skipped_empty > 0:
        return (
            f"{funnel.sessions_skipped_empty} session(s) parsed to zero "
            "natural-language turns — a reader-side regression looks exactly "
            "like this (#332), and it is not a sampling decision"
        )
    if funnel.sessions_sampled_out > 0:
        return (
            f"all {funnel.sessions_sampled_out} parsed session(s) were "
            "sampled out — raise TRELLIS_CAPTURE_SAMPLE_DENOMINATOR's "
            "selectivity or accept that this window says nothing"
        )
    if funnel.sessions_parsed == 0 and funnel.sessions_seen > 0:
        return (
            f"{funnel.sessions_seen} transcript(s) seen but none parsed — "
            f"{funnel.sessions_skipped_watermark} were watermark-skipped and "
            f"{funnel.sessions_skipped_ephemeral} ran in ephemeral projects"
        )
    return "sweeps ran but adjudicated no sessions"


def _sessions_with_stored_memory(
    event_log: EventLog, *, since: datetime, limit: int
) -> tuple[int, ScanCoverage]:
    """Distinct capture ``session_id``s seen in ``MEMORY_STORED``.

    Derived from the write path rather than the sweep's own report, so it
    remains available on a deployment whose sweeps predate
    ``CAPTURE_SWEEP_COMPLETED`` — and disagrees loudly if the two ever drift.
    """
    session_ids: set[str] = set()
    scan = scan_events(
        event_log, event_type=EventType.MEMORY_STORED, since=since, limit=limit
    )
    for event in scan.events:
        if event.source != CAPTURE_SOURCE:
            continue
        metadata = (event.payload or {}).get("metadata")
        if not isinstance(metadata, dict):
            continue
        session_id = metadata.get("session_id")
        if isinstance(session_id, str) and session_id:
            session_ids.add(session_id)
    return len(session_ids), scan.coverage


def summarize_capture_coverage(
    event_log: EventLog,
    *,
    days: int = 7,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> CaptureCoverageReport:
    """Coverage of session capture over the trailing *days*.

    Never raises on an empty or unwired log: absence resolves to
    ``state="unobserved"`` with no rate, which is the honest reading and the
    one that keeps a missing deployment from being filed as a code defect.
    """
    now = datetime.now(tz=UTC)
    since = now - timedelta(days=days)
    report = CaptureCoverageReport(window_days=days)

    sweep_scan = scan_events(
        event_log,
        event_type=EventType.CAPTURE_SWEEP_COMPLETED,
        since=since,
        limit=limit,
    )
    events = sweep_scan.events
    stored_sessions, stored_coverage = _sessions_with_stored_memory(
        event_log, since=since, limit=limit
    )
    report.sessions_with_stored_memory = stored_sessions
    report.scan = merge_coverage(sweep_scan.coverage, stored_coverage)
    if report.scan.truncated and report.scan.note:
        report.notes.append(report.scan.note)

    if not events:
        earlier_scan = scan_events(
            event_log,
            event_type=EventType.CAPTURE_SWEEP_COMPLETED,
            since=now - timedelta(days=UNOBSERVED_LOOKBACK_DAYS),
            limit=limit,
        )
        earlier = earlier_scan.events
        if earlier:
            report.state = "stale"
            report.suppressed_reason = SUPPRESSED_STALE
            report.last_sweep_at = max(e.occurred_at for e in earlier)
            report.notes.append(
                f"no capture sweep reported in the last {days} day(s); the "
                f"most recent was {report.last_sweep_at:%Y-%m-%d %H:%M} UTC — "
                "the pipeline ran and stopped"
            )
        else:
            report.state = "unobserved"
            report.suppressed_reason = SUPPRESSED_UNOBSERVED
            report.notes.append(
                "no capture sweep has ever reported to this event log. Either "
                "session capture is not deployed against this store, or the "
                "build running it predates capture.sweep_completed — this is "
                "not a coverage of zero, it is an absence of measurement"
            )
        if report.sessions_with_stored_memory:
            report.notes.append(
                f"{report.sessions_with_stored_memory} session(s) did store a "
                "captured memory in this window, so capture is running "
                "somewhere — only its funnel is unreported"
            )
        return report

    funnel, dry_runs = _funnel_from_events(events)
    report.funnel = funnel
    report.dry_run_sweeps_excluded = dry_runs
    report.last_sweep_at = max(e.occurred_at for e in events)
    report.eligible_sessions = funnel.sessions_triggered
    report.sessions_with_memory = funnel.sessions_with_memory

    if funnel.sessions_triggered == 0:
        report.state = "degraded"
        report.suppressed_reason = SUPPRESSED_DEGRADED
        report.degraded_reason = _degraded_reason(funnel)
        return report

    report.state = "measured"
    if funnel.sessions_triggered < MIN_ELIGIBLE_SESSIONS:
        report.suppressed_reason = SUPPRESSED_THIN_SAMPLE
        report.notes.append(
            f"{funnel.sessions_triggered} eligible session(s) is below the "
            f"{MIN_ELIGIBLE_SESSIONS}-session minimum; counts are reported, "
            "the rate is not"
        )
        return report

    report.capture_rate = round(
        funnel.sessions_with_memory / funnel.sessions_triggered, 4
    )
    if funnel.sessions_skipped_empty > 0:
        report.notes.append(
            f"{funnel.sessions_skipped_empty} session(s) parsed to zero turns "
            "— outside the denominator by construction, and the shape a "
            "reader regression takes (#332). Watch it, not the rate"
        )
    return report


__all__ = [
    "CAPTURE_SOURCE",
    "MIN_ELIGIBLE_SESSIONS",
    "SUPPRESSED_DEGRADED",
    "SUPPRESSED_STALE",
    "SUPPRESSED_THIN_SAMPLE",
    "SUPPRESSED_UNOBSERVED",
    "CaptureCoverageReport",
    "CaptureFunnel",
    "summarize_capture_coverage",
]
