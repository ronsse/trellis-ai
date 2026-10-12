"""Per-loop health: is each learning / curation loop doing anything?

The nightly cron (``03:30 worker curate``, invoked manually for
``worker tune``) is the only thing that drives most of these loops, and
until now its result lived in a structlog line a cron wrapper piped to
a host-only log file the API container cannot read. An operator asking
"is advisory fitness actually adjusting anything, or has it been
silently inert for a month" had to SSH in and tail a file.

:func:`summarize_loop_health` answers that from the EventLog alone (plus
one documented exception below), reading ``CURATE_CYCLE_COMPLETED`` /
``TUNE_CYCLE_COMPLETED`` (:mod:`trellis_cli.worker`), ``PRECEDENT_PROMOTED``,
``PARAMS_AUTO_PROMOTED``, ``FEEDBACK_RECORDED`` and
``LEARNING_AUTO_PROMOTE_EVALUATED`` (the last one for the
``learning_candidate_auto_promote`` row, which also folds in
:func:`trellis.learning.candidate_auto_promote.compute_agreement_metric`'s
read of ``REVIEW_DECISION_RECORDED``). It adds no new probe.

**Never-run vs ran-and-counted-zero.** ``LoopHealthRow.last_run_at`` /
``last_status`` are ``None`` together exactly when the loop has produced
no event this EventLog can see — "never run (or ran before this
build)". A loop that ran and did nothing reports a real timestamp and a
``counters`` dict whose values are legitimately ``0``. Collapsing those
two into one ``0`` is the exact failure CLAUDE.md's health-signal rule
exists to prevent.

**Documented exception to "events only".** The tuner row's
``pending_count`` reads :meth:`TunerStateStore.list_proposals` directly
rather than deriving it from an event. ``TUNER_PROPOSAL_CREATED`` is
referenced in prose but never emitted anywhere in the codebase, so
there is no event this number could come from, and it is live state
("how many proposals are pending right now") that a per-run event
stream could not answer anyway — a backlog built across several tuner
passes has one true current size, not a per-run one. ``GET
/admin/proposals`` already makes the identical read; this reuses that
path rather than opening a new one.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.learning.candidate_auto_promote import compute_agreement_metric
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventLog,
    EventScan,
    EventType,
    ScanCoverage,
    merge_coverage,
    scan_events,
)
from trellis.stores.base.tuner_state import TunerStateStore

#: Proposals fetched per status when sizing the tuner's live pending
#: backlog. Generous relative to ``GET /admin/proposals``'s default
#: (100): this is a count, not a page a human will scroll, and
#: undercounting a backlog as healthy is the wrong direction to err in.
_PENDING_PROPOSAL_LIMIT = 2000

#: Windows the feedback-intake row reports, per the brief's literal ask
#: ("FEEDBACK_RECORDED last 7d/30d").
_FEEDBACK_WINDOWS_DAYS = (7, 30)

#: Which catalog job (``trellis.schedule.catalog.JOB_CATALOG``) drives
#: each loop below — the single place this mapping is maintained. A loop
#: absent here has no scheduled job behind it at all: ``precedent_promotion``
#: is a human action (Review tab / ``trellis curate promote-learning``) and
#: ``feedback_intake`` is an agent's own ``record_feedback`` call, neither
#: of which a cron job drives.
LOOP_JOB: MappingABC[str, str] = {
    "noise_demotion": "curate-nightly",
    "advisory_generation": "curate-nightly",
    "advisory_fitness": "curate-nightly",
    "learning_candidate_scoring": "curate-nightly",
    "learning_candidate_auto_promote": "auto-promote-candidates",
    "tuner": "tune",
}


def _invert_loop_job(
    mapping: MappingABC[str, str],
) -> MappingABC[str, tuple[str, ...]]:
    inverse: dict[str, list[str]] = {}
    for loop_name, job_name in mapping.items():
        inverse.setdefault(job_name, []).append(loop_name)
    return {job_name: tuple(loops) for job_name, loops in inverse.items()}


#: Inverse of :data:`LOOP_JOB`, derived once at import time: which loops a
#: given catalog job drives. ``trellis_api.routes.admin`` threads this onto
#: ``AdminJobSummary.loops`` so the Schedules tab can name what a job
#: drives (and what disabling it stops) from this one mapping, rather than
#: a second, independently-maintained one.
JOB_LOOPS: MappingABC[str, tuple[str, ...]] = _invert_loop_job(LOOP_JOB)


class LoopHealthRow(TrellisModel):
    """One loop's last-run snapshot.

    ``last_run_at`` / ``last_status`` / ``counters`` are all ``None``
    together when the loop has never produced a visible event — see
    module docstring. ``actuates`` is whether the loop can write without
    a human approving each change; ``what_it_changes`` is the one-line
    explanation a UI renders next to it either way. ``job_name`` is the
    catalog job (see :data:`LOOP_JOB`) that drives this loop, or ``None``
    when nothing schedules it. ``operator_hint``, when set, is the CLI
    invocation or setting name an operator would need to act on this loop
    by hand (for example a manual promotion command); a renderer shows it
    as a small secondary footnote, never folded into ``description`` or
    ``what_it_changes``, which stay plain operator prose.
    """

    name: str
    description: str
    actuates: bool
    what_it_changes: str
    job_name: str | None = None
    operator_hint: str | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    counters: dict[str, Any] | None = None


class LoopHealthReport(TrellisModel):
    """``GET /api/v1/loops`` payload: one row per loop."""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    loops: list[LoopHealthRow] = Field(default_factory=list)
    scan: ScanCoverage = Field(default_factory=ScanCoverage)


def _scan(event_log: EventLog, event_type: EventType, *, limit: int) -> EventScan:
    return scan_events(event_log, event_type=event_type, limit=limit)


def _build_candidate_auto_promote_row(
    event_log: EventLog, *, limit: int
) -> tuple[LoopHealthRow, ScanCoverage]:
    """Build the ``learning_candidate_auto_promote`` row.

    Factored out of :func:`summarize_loop_health` to keep that function's
    statement count under the lint threshold; the row itself follows the
    same ``_scan`` + latest-event pattern as every other row there.
    """
    candidate_scan = _scan(
        event_log, EventType.LEARNING_AUTO_PROMOTE_EVALUATED, limit=limit
    )
    latest = candidate_scan.events[-1] if candidate_scan.events else None

    # compute_agreement_metric reads REVIEW_DECISION_RECORDED / its own
    # event type directly (not via scan_events), matching the tuner row's
    # documented live-read exception rather than this file's
    # scan_events + ScanCoverage pattern.
    agreement = compute_agreement_metric(event_log, limit=limit)

    counters: dict[str, Any] | None = None
    last_status: str | None = None
    if latest is not None:
        counts = latest.payload.get("counts") or {}
        snapshot = latest.payload.get("policy_snapshot") or {}
        skipped = counts.get("skipped") or 0
        counters = {
            "last_run_mode": latest.payload.get("mode"),
            "last_run_would_promote": counts.get("would_promote"),
            "last_run_already_promoted": counts.get("already_promoted"),
            "last_run_promoted": counts.get("promoted"),
            "last_run_ineligible": counts.get("ineligible"),
            "last_run_skipped": skipped,
            "configured_enabled": snapshot.get("enabled"),
            # The run's true configured mode (pre-``--dry-run``, pre- any
            # forced-report-only override) is a top-level payload field,
            # not ``policy_snapshot.mode`` -- the snapshot reflects the
            # *effective* policy the run actually used
            # (``LearningCandidateAutoPromotePolicy`` can never even be
            # constructed with ``mode="commit"``, so the snapshot can
            # only ever read "report_only"). Reading the snapshot here
            # silently discarded the one field that lets an operator see
            # "commit is configured, --dry-run / enabled=false is the
            # only reason nothing happened."
            "configured_mode": latest.payload.get("configured_mode"),
            "agreement_agree": agreement.agree,
            "agreement_disagree": agreement.disagree,
            "agreement_undecided": agreement.undecided,
            "agreement_rate": agreement.rate,
        }
        # ``outcome`` is the one vocabulary key every
        # LEARNING_AUTO_PROMOTE_EVALUATED payload carries uniformly,
        # whether the run completed an evaluation ("ok"/"degraded",
        # emitted by ``_emit_evaluated_event``) or never reached one
        # ("refused"/"failed", emitted by
        # ``emit_candidate_auto_promote_failure_event``). A refused or
        # failed run must not read as "ok" just because it produced an
        # event at all. Fall back to the pre-``outcome`` skipped-based
        # rule for an older event recorded before that field existed,
        # rather than reporting a misleading ``None``/``"ok"`` for it.
        outcome = latest.payload.get("outcome")
        last_status = (
            outcome if outcome is not None else ("degraded" if skipped else "ok")
        )
        # Staleness (is this job still firing at all, vs a stale status
        # from a run that stopped happening) is reported as a separate
        # field, computed uniformly for every loop with a scheduled job
        # from the resolved schedule row -- see the ``/loops`` route. It
        # is not folded into ``last_status`` here, so a quiet job reads
        # its true last outcome rather than a status this function
        # invents from a cadence assumption it cannot itself verify
        # against any stored schedule override.

    row = LoopHealthRow(
        name="learning_candidate_auto_promote",
        description=(
            "Evaluates learning candidates and reports what it would "
            "promote to a precedent."
        ),
        actuates=False,
        what_it_changes=(
            "Nothing yet: committing a promotion automatically is refused "
            "until a follow-up ships auto-demotion, so every run today "
            "only reports what it would do for each candidate — it never "
            "writes a precedent."
        ),
        operator_hint=(
            "CLI: `trellis curate auto-promote-candidates`. Automatic "
            "commits are refused; only report-only runs succeed today."
        ),
        job_name=LOOP_JOB.get("learning_candidate_auto_promote"),
        last_run_at=latest.occurred_at if latest else None,
        last_status=last_status,
        counters=counters,
    )
    return row, candidate_scan.coverage


def summarize_loop_health(
    event_log: EventLog,
    tuner_state: TunerStateStore,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_SCAN_LIMIT,
    auto_promote_enabled: bool = False,
) -> LoopHealthReport:
    """Build the ``GET /api/v1/loops`` report from existing events (+ one live read).

    Eight rows, in the order the nightly cycle runs them: noise
    demotion, advisory generation, advisory fitness, learning-candidate
    scoring, precedent promotion, learning-candidate auto-promotion,
    tuner, feedback intake.

    ``auto_promote_enabled`` is the caller's already-resolved (env >
    settings > config.yaml > default) ``learning.auto_promote.enabled``
    value. The tuner row's ``actuates`` is true only when that resolves
    true AND a non-dry-run tune has actually happened
    (``TUNE_CYCLE_COMPLETED`` is only ever emitted for one -- the
    scheduled run always passes ``--dry-run``); this function has no
    settings access of its own, so it cannot resolve that boolean
    itself.
    """
    now = now or datetime.now(UTC)

    curate_scan = _scan(event_log, EventType.CURATE_CYCLE_COMPLETED, limit=limit)
    curate_latest = curate_scan.events[-1] if curate_scan.events else None
    curate_payload = curate_latest.payload if curate_latest is not None else {}
    curate_skipped = set(curate_payload.get("skipped_stages") or ())

    def curate_status(skip_label: str, *, degrade_aware: bool = False) -> str | None:
        if curate_latest is None:
            return None
        if skip_label in curate_skipped:
            return "skipped"
        if degrade_aware and curate_payload.get("advisory_store_degraded"):
            return "degraded"
        if degrade_aware and curate_payload.get("advisory_store_stale"):
            return "stale"
        return "ok"

    def curate_counters(*keys: str) -> dict[str, Any] | None:
        if curate_latest is None:
            return None
        return {key: curate_payload.get(key) for key in keys}

    noise_row = LoopHealthRow(
        name="noise_demotion",
        description=(
            'Demotes low-value items to a "noise" quality based on '
            "serve/feedback evidence, so they stop being candidates for "
            "new packs."
        ),
        actuates=True,
        what_it_changes=(
            "Writes a noise-quality tag onto each demoted item during the "
            "nightly curation run; demotion requires evidence of "
            "unhelpfulness, never merely an absence of evidence."
        ),
        job_name=LOOP_JOB.get("noise_demotion"),
        last_run_at=curate_latest.occurred_at if curate_latest else None,
        last_status=curate_status("noise_tags"),
        counters=curate_counters("noise_tagged", "noise_refused_non_document"),
    )

    advisory_generation_row = LoopHealthRow(
        name="advisory_generation",
        description=(
            "Mines new advisories from outcome data during the nightly curation run."
        ),
        actuates=True,
        what_it_changes=(
            "Writes new Advisory rows to the advisory store; a generated "
            "advisory can be served in a pack starting with the next "
            "retrieval."
        ),
        job_name=LOOP_JOB.get("advisory_generation"),
        last_run_at=curate_latest.occurred_at if curate_latest else None,
        last_status=curate_status("advisories", degrade_aware=True),
        counters=curate_counters("advisories_generated", "advisories_refused"),
    )

    advisory_fitness_row = LoopHealthRow(
        name="advisory_fitness",
        description=(
            "Adjusts advisory confidence and suppresses/restores advisories "
            "based on how often each one gets cited as helpful."
        ),
        actuates=True,
        what_it_changes=(
            "Rewrites confidence on existing advisories and flips them "
            "suppressed/restored during the nightly curation run; a "
            "suppressed advisory stops being served without being deleted."
        ),
        job_name=LOOP_JOB.get("advisory_fitness"),
        last_run_at=curate_latest.occurred_at if curate_latest else None,
        last_status=curate_status("advisories", degrade_aware=True),
        counters=curate_counters("advisories_suppressed", "advisories_boosted"),
    )

    learning_counters = None
    if curate_latest is not None:
        ready = curate_payload.get("learning_promotion_ready") or {}
        learning_counters = {
            "learning_observations": curate_payload.get("learning_observations"),
            "learning_candidates": curate_payload.get("learning_candidates"),
            "learning_promotion_ready_count": ready.get("count"),
        }
    learning_row = LoopHealthRow(
        name="learning_candidate_scoring",
        description=(
            "Scores learning-candidate observations and writes them to a "
            "review queue for a human to evaluate."
        ),
        actuates=False,
        what_it_changes=(
            "Adds candidates to the review queue during the nightly "
            "curation run; it never promotes a candidate to a precedent "
            "itself — that decision is always made by a human."
        ),
        job_name=LOOP_JOB.get("learning_candidate_scoring"),
        last_run_at=curate_latest.occurred_at if curate_latest else None,
        last_status=curate_status("learning"),
        counters=learning_counters,
    )

    precedent_scan = _scan(event_log, EventType.PRECEDENT_PROMOTED, limit=limit)
    precedent_events = precedent_scan.events
    precedent_latest = precedent_events[-1] if precedent_events else None
    precedent_row = LoopHealthRow(
        name="precedent_promotion",
        description=(
            "Turns a reviewed, promotion-ready learning candidate into a "
            "precedent that agents can retrieve and the Precedents tab "
            "lists."
        ),
        actuates=False,
        what_it_changes=(
            "Nothing schedules a promotion automatically: a human approves "
            "each one from the Review tab. The auto-promotion loop below "
            "only ever reports what it would promote; committing one "
            "automatically is refused until a follow-up ships "
            "auto-demotion. This count includes any promotion run by hand "
            "as well as from the review queue."
        ),
        operator_hint="CLI: `trellis curate promote-learning`.",
        job_name=LOOP_JOB.get("precedent_promotion"),
        last_run_at=precedent_latest.occurred_at if precedent_latest else None,
        # PRECEDENT_PROMOTED carries no explicit outcome field -- "ran"
        # (not "ok") so the badge never implies a success determination
        # this event never actually made.
        last_status="ran" if precedent_latest else None,
        counters=(
            {
                "promoted_last_7d": sum(
                    1
                    for e in precedent_events
                    if e.occurred_at >= now - timedelta(days=7)
                ),
                "promoted_last_30d": sum(
                    1
                    for e in precedent_events
                    if e.occurred_at >= now - timedelta(days=30)
                ),
            }
            if precedent_latest
            else None
        ),
    )

    candidate_auto_promote_row, candidate_auto_promote_coverage = (
        _build_candidate_auto_promote_row(event_log, limit=limit)
    )

    tune_scan = _scan(event_log, EventType.TUNE_CYCLE_COMPLETED, limit=limit)
    tune_latest = tune_scan.events[-1] if tune_scan.events else None
    auto_promoted_scan = _scan(event_log, EventType.PARAMS_AUTO_PROMOTED, limit=limit)
    pending_count = len(
        tuner_state.list_proposals(status="pending", limit=_PENDING_PROPOSAL_LIMIT)
    )
    tuner_counters: dict[str, Any] = {
        # Live store state, not event-derived — see module docstring.
        "pending_count": pending_count,
        "auto_promoted_total": len(auto_promoted_scan.events),
    }
    if tune_latest is not None:
        tune_payload = tune_latest.payload
        tuner_counters["last_run_proposals_considered"] = tune_payload.get(
            "proposals_considered"
        )
        tuner_counters["last_run_auto_promoted"] = tune_payload.get("auto_promoted")
        tuner_counters["last_run_pending_manual"] = tune_payload.get("pending_manual")
    tuner_row = LoopHealthRow(
        name="tuner",
        description=(
            "Looks for rule-tuning opportunities in recent outcomes. The "
            "scheduled run only ever proposes changes; it never promotes "
            "one automatically."
        ),
        # Resolved settings alone are not enough: the schedule's own run
        # is permanently --dry-run (see description above), so this is
        # only ever true once a manual non-dry-run tune has actually
        # happened -- which TUNE_CYCLE_COMPLETED's mere existence proves,
        # since that event is only emitted for one.
        actuates=auto_promote_enabled and tune_latest is not None,
        what_it_changes=(
            "The scheduled run writes nothing but pending proposals; "
            "until auto-promotion is both enabled and run by hand, every "
            "proposal stays pending for a human to review."
        ),
        operator_hint=(
            "Auto-promotion needs `learning.auto_promote.enabled` set AND "
            "a manual, non-dry-run `trellis worker tune` run. Manual "
            "promotion: `trellis metrics promote --commit`."
        ),
        job_name=LOOP_JOB.get("tuner"),
        last_run_at=tune_latest.occurred_at if tune_latest else None,
        # TUNE_CYCLE_COMPLETED carries no explicit outcome field -- "ran"
        # (not "ok") so the badge never implies a success determination
        # this event never actually made.
        last_status="ran" if tune_latest else None,
        counters=tuner_counters,
    )

    feedback_scan = _scan(event_log, EventType.FEEDBACK_RECORDED, limit=limit)
    feedback_events = feedback_scan.events
    latest_feedback = feedback_events[-1] if feedback_events else None
    feedback_row = LoopHealthRow(
        name="feedback_intake",
        description=(
            "Records agent-reported outcome/helpfulness signal against "
            "served packs and traces."
        ),
        actuates=False,
        what_it_changes=(
            "Records the signal itself; it does not change behavior or "
            "content on its own — it is the input the other loops above "
            "consume."
        ),
        job_name=LOOP_JOB.get("feedback_intake"),
        last_run_at=latest_feedback.occurred_at if latest_feedback else None,
        # FEEDBACK_RECORDED carries no explicit outcome field -- "ran"
        # (not "ok") so the badge never implies a success determination
        # this event never actually made.
        last_status="ran" if latest_feedback else None,
        counters=(
            {
                f"feedback_last_{days}d": sum(
                    1
                    for e in feedback_events
                    if e.occurred_at >= now - timedelta(days=days)
                )
                for days in _FEEDBACK_WINDOWS_DAYS
            }
            if latest_feedback
            else None
        ),
    )

    scan_coverage = merge_coverage(
        curate_scan.coverage,
        precedent_scan.coverage,
        candidate_auto_promote_coverage,
        tune_scan.coverage,
        auto_promoted_scan.coverage,
        feedback_scan.coverage,
    )

    return LoopHealthReport(
        generated_at=now,
        loops=[
            noise_row,
            advisory_generation_row,
            advisory_fitness_row,
            learning_row,
            precedent_row,
            candidate_auto_promote_row,
            tuner_row,
            feedback_row,
        ],
        scan=scan_coverage,
    )
