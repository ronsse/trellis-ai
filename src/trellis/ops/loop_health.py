"""Per-loop health: is each learning / curation loop doing anything? (e167)

The nightly cron (``03:30 worker curate``, invoked manually for
``worker tune``) is the only thing that drives most of these loops, and
until now its result lived in a structlog line a cron wrapper piped to
a host-only log file the API container cannot read. An operator asking
"is advisory fitness actually adjusting anything, or has it been
silently inert for a month" had to SSH in and tail a file.

:func:`summarize_loop_health` answers that from the EventLog alone (plus
one documented exception below), reading ``CURATE_CYCLE_COMPLETED`` /
``TUNE_CYCLE_COMPLETED`` (:mod:`trellis_cli.worker`), ``PRECEDENT_PROMOTED``,
``PARAMS_AUTO_PROMOTED`` and ``FEEDBACK_RECORDED``. It adds no new probe.

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

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field

from trellis.core.base import TrellisModel
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


class LoopHealthRow(TrellisModel):
    """One loop's last-run snapshot.

    ``last_run_at`` / ``last_status`` / ``counters`` are all ``None``
    together when the loop has never produced a visible event — see
    module docstring. ``actuates`` is whether the loop can write without
    a human approving each change; ``what_it_changes`` is the one-line
    explanation a UI renders next to it either way.
    """

    name: str
    description: str
    actuates: bool
    what_it_changes: str
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


def summarize_loop_health(
    event_log: EventLog,
    tuner_state: TunerStateStore,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> LoopHealthReport:
    """Build the ``GET /api/v1/loops`` report from existing events (+ one live read).

    Seven rows, in the order the nightly cycle runs them: noise
    demotion, advisory generation, advisory fitness, learning-candidate
    scoring, precedent promotion, tuner, feedback intake.
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
            'Demotes low-value items to signal_quality="noise" based on '
            "serve/feedback evidence, so they stop being candidates for "
            "new packs."
        ),
        actuates=True,
        what_it_changes=(
            "Writes a noise-quality tag onto each demoted item during "
            "worker curate; demotion requires evidence of unhelpfulness, "
            "never absence of evidence (demotion_gate.py)."
        ),
        last_run_at=curate_latest.occurred_at if curate_latest else None,
        last_status=curate_status("noise_tags"),
        counters=curate_counters("noise_tagged", "noise_refused_non_document"),
    )

    advisory_generation_row = LoopHealthRow(
        name="advisory_generation",
        description="Mines new advisories from outcome data during worker curate.",
        actuates=True,
        what_it_changes=(
            "Writes new Advisory rows to the advisory store; a generated "
            "advisory can be served in a pack starting with the next "
            "retrieval."
        ),
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
            "Rewrites confidence on existing Advisory rows and flips "
            "suppressed/restored during worker curate; a suppressed "
            "advisory stops being served without being deleted."
        ),
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
            "Scores learning-candidate observations from the EventLog and "
            "writes a review artifact (candidates.jsonl / decisions.jsonl)."
        ),
        actuates=False,
        what_it_changes=(
            "Writes review-artifact files to disk during worker curate; "
            "it never promotes a candidate to a precedent itself — that is "
            "a separate human-run command (sensing only)."
        ),
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
            "durable precedent in the knowledge graph."
        ),
        actuates=False,
        what_it_changes=(
            "A human runs the promotion command; nothing here promotes a "
            "candidate on its own (sensing only — this row counts the "
            "manual promotions that have happened)."
        ),
        last_run_at=precedent_latest.occurred_at if precedent_latest else None,
        last_status="ok" if precedent_latest else None,
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

    tune_scan = _scan(event_log, EventType.TUNE_CYCLE_COMPLETED, limit=limit)
    tune_latest = tune_scan.events[-1] if tune_scan.events else None
    auto_promoted_scan = _scan(event_log, EventType.PARAMS_AUTO_PROMOTED, limit=limit)
    pending_count = len(
        tuner_state.list_proposals(status="pending", limit=_PENDING_PROPOSAL_LIMIT)
    )
    tuner_counters: dict[str, Any] = {
        # Live store state, not event-derived — see module docstring.
        "pending_count": pending_count,
        "promoted_total": len(auto_promoted_scan.events),
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
            "Runs a RuleTuner pass over outcomes and, when "
            "learning.auto_promote.enabled is set, auto-promotes "
            "qualifying parameter proposals."
        ),
        actuates=True,
        what_it_changes=(
            "Auto-promotes a parameter proposal ONLY when "
            "learning.auto_promote.enabled is set (off by default); "
            "otherwise every proposal stays pending for a human to run "
            "`trellis metrics promote --commit`."
        ),
        last_run_at=tune_latest.occurred_at if tune_latest else None,
        last_status="ok" if tune_latest else None,
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
            "Appends a FEEDBACK_RECORDED event; it does not itself change "
            "behavior or content — it is the input the other loops above "
            "consume (sensing only)."
        ),
        last_run_at=latest_feedback.occurred_at if latest_feedback else None,
        last_status="ok" if latest_feedback else None,
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
            tuner_row,
            feedback_row,
        ],
        scan=scan_coverage,
    )
