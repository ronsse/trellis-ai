"""Context effectiveness analysis -- measures pack item success correlation.

Provides four entry points:

* :func:`analyze_effectiveness` — read-only analysis producing an
  :class:`EffectivenessReport` with ``noise_candidates``.
* :func:`run_effectiveness_feedback` — full loop: analyse **then** apply
  noise tags via :func:`~trellis.classify.feedback.apply_noise_tags`.
* :func:`analyze_advisory_effectiveness` — read-only analysis measuring
  how advisories correlate with pack outcomes.
* :func:`run_advisory_fitness_loop` — full loop: analyse advisory
  effectiveness **then** adjust confidence and suppress weak advisories.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.schemas.advisory import DriftPattern
from trellis.schemas.parameters import ParameterScope
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.document import DocumentStore
from trellis.stores.base.event_log import EventLog, EventType

if TYPE_CHECKING:
    from trellis.ops.registry import ParameterRegistry

logger = structlog.get_logger(__name__)

# Thresholds for classification.  Still the canonical defaults — the
# ``registry`` parameter on each function can override them per scope.
_SUCCESS_RATING_THRESHOLD = 0.5
_NOISE_RATE_THRESHOLD = 0.3

# Advisory fitness thresholds
_ADVISORY_MIN_PRESENTATIONS = 3
_ADVISORY_SUPPRESS_CONFIDENCE = 0.1
_CONFIDENCE_BLEND_WEIGHT = 0.3  # how much observed fitness influences confidence
#: Hysteresis margin: a suppressed advisory's confidence must climb
#: above ``suppress_below + _RESTORE_HYSTERESIS`` before auto-restore
#: fires. Prevents flapping around the threshold when sample counts
#: are low.
_RESTORE_HYSTERESIS = 0.05

# --- Drift detection (Gap 2.4) ---------------------------------------
#
# Smoothed confidence updates (``_CONFIDENCE_BLEND_WEIGHT = 0.3``) mask
# regime shifts. These constants parametrise a stateless, windowed
# comparison between the full analysis window and a shorter "recent"
# sub-window — any advisory whose recent outcomes diverge materially
# from its full-window baseline gets flagged via
# ``ADVISORY_DRIFT_DETECTED`` so an operator can review before the
# gradual confidence update absorbs the shift.
#
# The default recent window (7d against a 30d full window) is wide
# enough to suppress single-day noise, narrow enough to catch a task-mix
# regime change.
_DEFAULT_DRIFT_WINDOW_DAYS = 7
#: Minimum presentations inside the recent window before an advisory
#: can be flagged. Gap 2.4 is explicitly NOT about sparse-data noise;
#: this floor prevents 1-sample drift alerts from flooding the signal.
_DRIFT_MIN_RECENT_PRESENTATIONS = 2
#: Absolute drop in success_rate (recent - full) that constitutes a
#: regime-shift decline. 0.25 is a deliberate coarse threshold — finer
#: tuning should wait for a design partner's operational signal.
_DRIFT_REGIME_SHIFT_THRESHOLD = 0.25
#: Minimum |recent_lift| for a sign-flip to count. Stops noise around
#: lift ≈ 0 from tripping the detector.
_DRIFT_LIFT_FLIP_MAGNITUDE = 0.1

# Component IDs used when resolving registry overrides.
_ITEMS_COMPONENT = "retrieve.effectiveness.items"
_ADVISORY_COMPONENT = "retrieve.effectiveness.advisory"


def _resolve_param(
    registry: ParameterRegistry | None,
    component_id: str,
    key: str,
    default: Any,
) -> Any:
    """Read ``key`` from ``registry`` for the given component, else ``default``."""
    if registry is None:
        return default
    return registry.get(ParameterScope(component_id=component_id), key, default)


def _lift_vs_baseline(
    successes: int,
    presentations: int,
    total_successes: int,
    total_feedback: int,
) -> tuple[float, float, float]:
    """Compute ``(success_rate, baseline_rate, lift)`` for one advisory.

    ``baseline_rate`` is the success rate of packs *without* this
    advisory. When every pack in the window carried the advisory
    (``packs_without == 0``), we fall back to the overall window rate
    so downstream math still produces a meaningful number instead of
    dividing by zero — this single fallback is the authoritative
    zero-sample policy for the module.
    """
    rate = successes / presentations if presentations > 0 else 0.0
    packs_without = total_feedback - presentations
    success_without = total_successes - successes
    if packs_without > 0:
        baseline = success_without / packs_without
    elif total_feedback > 0:
        baseline = total_successes / total_feedback
    else:
        baseline = 0.0
    return rate, baseline, rate - baseline


class EffectivenessReport(TrellisModel):
    """Report on context pack effectiveness."""

    total_packs: int
    total_feedback: int
    success_rate: float
    item_scores: list[dict[str, Any]]
    noise_candidates: list[str]


def analyze_effectiveness(
    event_log: EventLog,
    *,
    days: int = 30,
    min_appearances: int = 2,
    registry: ParameterRegistry | None = None,
) -> EffectivenessReport:
    """Analyze which injected context items correlate with task success.

    Joins PACK_ASSEMBLED events with FEEDBACK_RECORDED events to compute
    per-item success rates.

    Args:
        event_log: The event log to query.
        days: How many days of history to analyze.
        min_appearances: Minimum times an item must appear to be scored.
        registry: Optional parameter registry.  When provided, overrides
            the success-rating and noise-rate thresholds from the active
            parameter snapshot.

    Returns:
        EffectivenessReport with per-item success rates and noise candidates.
    """
    success_threshold = _resolve_param(
        registry,
        _ITEMS_COMPONENT,
        "success_rating_threshold",
        _SUCCESS_RATING_THRESHOLD,
    )
    noise_threshold = _resolve_param(
        registry, _ITEMS_COMPONENT, "noise_rate_threshold", _NOISE_RATE_THRESHOLD
    )

    since = datetime.now(tz=UTC) - timedelta(days=days)

    # Get all pack assembly events
    pack_events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED,
        since=since,
        limit=1000,
    )

    # Get all feedback events
    feedback_events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        since=since,
        limit=1000,
    )

    # Build pack_id -> injected_item_ids mapping
    pack_items: dict[str, list[str]] = {}
    for event in pack_events:
        pack_id = event.entity_id
        if pack_id:
            pack_items[pack_id] = event.payload.get("injected_item_ids", [])

    # Build pack_id -> feedback mapping
    pack_feedback: dict[str, bool] = {}
    for event in feedback_events:
        pack_id = event.payload.get("pack_id") or event.entity_id
        if pack_id and pack_id in pack_items:
            rating = event.payload.get("rating", 0.0)
            pack_feedback[pack_id] = event.payload.get(
                "success", rating >= success_threshold
            )

    # Calculate per-item success rates
    item_successes: dict[str, int] = defaultdict(int)
    item_failures: dict[str, int] = defaultdict(int)
    item_appearances: dict[str, int] = defaultdict(int)

    for pack_id, items in pack_items.items():
        if pack_id not in pack_feedback:
            continue
        success = pack_feedback[pack_id]
        for item_id in items:
            item_appearances[item_id] += 1
            if success:
                item_successes[item_id] += 1
            else:
                item_failures[item_id] += 1

    # Build scored items list
    item_scores: list[dict[str, Any]] = []
    noise_candidates: list[str] = []

    for item_id, count in item_appearances.items():
        if count < min_appearances:
            continue
        successes = item_successes[item_id]
        failures = item_failures[item_id]
        rate = successes / count if count > 0 else 0.0

        item_scores.append(
            {
                "item_id": item_id,
                "appearances": count,
                "successes": successes,
                "failures": failures,
                "success_rate": round(rate, 3),
            }
        )

        # Flag items that appear frequently but correlate with failure
        if rate < noise_threshold and count >= min_appearances:
            noise_candidates.append(item_id)

    item_scores.sort(key=lambda x: x["success_rate"], reverse=True)

    # Overall success rate
    total_feedback = len(pack_feedback)
    total_successes = sum(1 for v in pack_feedback.values() if v)
    overall_rate = total_successes / total_feedback if total_feedback > 0 else 0.0

    return EffectivenessReport(
        total_packs=len(pack_events),
        total_feedback=total_feedback,
        success_rate=round(overall_rate, 3),
        item_scores=item_scores,
        noise_candidates=noise_candidates,
    )


def run_effectiveness_feedback(
    event_log: EventLog,
    document_store: DocumentStore,
    *,
    days: int = 30,
    min_appearances: int = 2,
    registry: ParameterRegistry | None = None,
) -> EffectivenessReport:
    """Analyse effectiveness **and** apply noise tags in one call.

    This closes the feedback loop that the ADR (``adr-deferred-cognition.md``)
    identifies as a Tier 1 gap:

    1. Run :func:`analyze_effectiveness` to find noise candidates.
    2. Call :func:`~trellis.classify.feedback.apply_noise_tags` to set
       ``signal_quality="noise"`` on those items in the document store.
    3. Log the outcome for operational visibility.

    Returns the :class:`EffectivenessReport` (unchanged from step 1).
    """
    from trellis.classify.feedback import apply_noise_tags  # noqa: PLC0415

    report = analyze_effectiveness(
        event_log,
        days=days,
        min_appearances=min_appearances,
        registry=registry,
    )

    if report.noise_candidates:
        updated = apply_noise_tags(report.noise_candidates, document_store)
        logger.info(
            "effectiveness_feedback_applied",
            noise_candidates=len(report.noise_candidates),
            items_updated=updated,
            days=days,
            success_rate=report.success_rate,
        )
    else:
        logger.debug(
            "effectiveness_feedback_noop",
            message="No noise candidates found",
            days=days,
            success_rate=report.success_rate,
        )

    return report


# --- Advisory effectiveness ---


class AdvisoryScore(TrellisModel):
    """Per-advisory fitness metrics."""

    advisory_id: str
    presentations: int
    successes: int
    failures: int
    success_rate: float
    baseline_rate: float  # success rate of packs *without* this advisory
    lift: float  # success_rate - baseline_rate


class AdvisoryDriftAlert(TrellisModel):
    """Regime-shift alert for an advisory (Gap 2.4).

    Raised when recent outcomes diverge materially from the full-window
    baseline. The smoothed confidence update does not surface these
    shifts — this alert (and the ``ADVISORY_DRIFT_DETECTED`` event it
    backs) is the operator-review signal.

    ``full_*`` fields duplicate values already present on the
    associated ``AdvisoryScore``; this is deliberate so the event
    payload (see ``_emit_drift_event``) is self-contained for audit
    consumers and no cross-join is required.
    """

    advisory_id: str
    pattern: DriftPattern
    full_success_rate: float
    recent_success_rate: float
    full_lift: float
    recent_lift: float
    full_presentations: int
    recent_presentations: int
    window_days: int
    recent_window_days: int


class AdvisoryEffectivenessReport(TrellisModel):
    """Report on advisory-outcome correlation."""

    total_packs_with_advisories: int
    total_feedback: int
    advisory_scores: list[AdvisoryScore]
    advisories_boosted: list[str]
    advisories_suppressed: list[str]
    advisories_restored: list[str] = Field(default_factory=list)
    #: Advisories whose recent fitness has diverged from the full-window
    #: baseline. Populated by the drift detector (Gap 2.4) — consumers
    #: should treat this as an operator-review signal, not an automatic
    #: demotion input.
    advisories_drifting: list[AdvisoryDriftAlert] = Field(default_factory=list)


@dataclass(frozen=True)
class _WindowAggregate:
    """Per-advisory outcomes over one time window.

    Packaged as a single value so the aggregator returns one object,
    the drift detector consumes one object, and the call site doesn't
    juggle five parallel dicts/ints.
    """

    successes: dict[str, int]
    presentations: dict[str, int]
    total_feedback: int
    total_successes: int
    packs_with_any_advisory: int


def _aggregate_advisory_outcomes(
    pack_advisories: dict[str, list[str]],
    pack_feedback: dict[str, bool],
    pack_occurred_at: dict[str, datetime],
    *,
    since: datetime | None = None,
) -> _WindowAggregate:
    """Aggregate per-advisory outcomes over packs assembled at-or-after ``since``.

    Pass ``since=None`` to aggregate over all packs in the input maps.
    """
    successes: dict[str, int] = defaultdict(int)
    presentations: dict[str, int] = defaultdict(int)
    total_feedback = 0
    total_successes = 0
    packs_with_any_advisory = 0

    for pack_id, advisory_ids in pack_advisories.items():
        if pack_id not in pack_feedback:
            continue
        if since is not None:
            occurred = pack_occurred_at.get(pack_id)
            if occurred is None or occurred < since:
                continue
        total_feedback += 1
        success = pack_feedback[pack_id]
        if success:
            total_successes += 1
        if advisory_ids:
            packs_with_any_advisory += 1
        for adv_id in advisory_ids:
            presentations[adv_id] += 1
            if success:
                successes[adv_id] += 1

    return _WindowAggregate(
        successes=successes,
        presentations=presentations,
        total_feedback=total_feedback,
        total_successes=total_successes,
        packs_with_any_advisory=packs_with_any_advisory,
    )


def _detect_drift_alerts(
    advisory_scores: list[AdvisoryScore],
    recent: _WindowAggregate,
    *,
    window_days: int,
    recent_window_days: int,
    min_recent_presentations: int,
) -> list[AdvisoryDriftAlert]:
    """Compare full-window scores to recent-window outcomes and flag drift.

    Two patterns (stateless, threshold-based):

    * ``REGIME_SHIFT_DECLINE`` — recent success_rate has dropped by at
      least :data:`_DRIFT_REGIME_SHIFT_THRESHOLD` vs. the full window.
    * ``LIFT_SIGN_FLIP`` — recent lift and full lift have opposite signs
      and ``|recent_lift|`` clears :data:`_DRIFT_LIFT_FLIP_MAGNITUDE`.

    When both fire, regime shift wins — absolute success drop is a
    stronger operational signal than correlation direction. One alert
    per advisory.
    """
    alerts: list[AdvisoryDriftAlert] = []
    for score in advisory_scores:
        recent_presentations = recent.presentations.get(score.advisory_id, 0)
        if recent_presentations < min_recent_presentations:
            continue

        recent_successes = recent.successes.get(score.advisory_id, 0)
        recent_rate, _, recent_lift = _lift_vs_baseline(
            recent_successes,
            recent_presentations,
            recent.total_successes,
            recent.total_feedback,
        )

        rate_drop = score.success_rate - recent_rate
        is_regime_shift = rate_drop >= _DRIFT_REGIME_SHIFT_THRESHOLD
        is_sign_flip = (
            score.lift * recent_lift < 0
            and abs(recent_lift) >= _DRIFT_LIFT_FLIP_MAGNITUDE
        )

        if not (is_regime_shift or is_sign_flip):
            continue

        pattern = (
            DriftPattern.REGIME_SHIFT_DECLINE
            if is_regime_shift
            else DriftPattern.LIFT_SIGN_FLIP
        )

        alerts.append(
            AdvisoryDriftAlert(
                advisory_id=score.advisory_id,
                pattern=pattern,
                full_success_rate=score.success_rate,
                recent_success_rate=round(recent_rate, 3),
                full_lift=score.lift,
                recent_lift=round(recent_lift, 3),
                full_presentations=score.presentations,
                recent_presentations=recent_presentations,
                window_days=window_days,
                recent_window_days=recent_window_days,
            )
        )

    return alerts


def analyze_advisory_effectiveness(
    event_log: EventLog,
    advisory_store: AdvisoryStore,  # noqa: ARG001 — kept for API symmetry with run_advisory_fitness_loop
    *,
    days: int = 30,
    min_presentations: int | None = None,
    drift_window_days: int | None = _DEFAULT_DRIFT_WINDOW_DAYS,
    registry: ParameterRegistry | None = None,
) -> AdvisoryEffectivenessReport:
    """Analyze how advisories correlate with pack outcomes.

    Joins PACK_ASSEMBLED events (which include ``advisory_ids``) with
    FEEDBACK_RECORDED events to compute per-advisory success rates and
    compare against baseline (packs without the advisory).

    When ``drift_window_days`` is set and smaller than ``days``, the
    analyser also computes outcomes over that sub-window and flags
    advisories whose recent behaviour diverges materially from the full
    window (Gap 2.4). Drift alerts are returned on
    ``AdvisoryEffectivenessReport.advisories_drifting``; this function
    itself does NOT emit events — the fitness loop does.

    Args:
        event_log: The event log to query.
        advisory_store: Advisory store for looking up advisory metadata.
        days: How many days of history to analyze.
        min_presentations: Minimum times an advisory must be presented
            before it is scored.
        drift_window_days: Width of the "recent" sub-window for drift
            detection. Set to ``None`` or a value ``>= days`` to skip
            drift analysis entirely.

    Returns:
        AdvisoryEffectivenessReport with per-advisory fitness scores
        and (optionally) drift alerts.
    """
    success_threshold = _resolve_param(
        registry,
        _ADVISORY_COMPONENT,
        "success_rating_threshold",
        _SUCCESS_RATING_THRESHOLD,
    )
    if min_presentations is None:
        min_presentations = _resolve_param(
            registry,
            _ADVISORY_COMPONENT,
            "min_presentations",
            _ADVISORY_MIN_PRESENTATIONS,
        )

    since = datetime.now(tz=UTC) - timedelta(days=days)

    pack_events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED,
        since=since,
        limit=5000,
    )
    feedback_events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        since=since,
        limit=5000,
    )

    pack_advisories: dict[str, list[str]] = {}
    pack_occurred_at: dict[str, datetime] = {}
    for event in pack_events:
        pack_id = event.entity_id
        if pack_id:
            pack_advisories[pack_id] = event.payload.get("advisory_ids", [])
            pack_occurred_at[pack_id] = event.occurred_at

    pack_feedback: dict[str, bool] = {}
    for event in feedback_events:
        pack_id = event.payload.get("pack_id") or event.entity_id
        if pack_id and pack_id in pack_advisories:
            rating = event.payload.get("rating", 0.0)
            pack_feedback[pack_id] = event.payload.get(
                "success", rating >= success_threshold
            )

    full = _aggregate_advisory_outcomes(
        pack_advisories, pack_feedback, pack_occurred_at, since=None
    )

    advisory_scores: list[AdvisoryScore] = []
    for adv_id, presentations in full.presentations.items():
        if presentations < min_presentations:
            continue
        s = full.successes.get(adv_id, 0)
        rate, baseline, lift = _lift_vs_baseline(
            s, presentations, full.total_successes, full.total_feedback
        )
        advisory_scores.append(
            AdvisoryScore(
                advisory_id=adv_id,
                presentations=presentations,
                successes=s,
                failures=presentations - s,
                success_rate=round(rate, 3),
                baseline_rate=round(baseline, 3),
                lift=round(lift, 3),
            )
        )

    advisory_scores.sort(key=lambda x: x.lift, reverse=True)

    drift_alerts: list[AdvisoryDriftAlert] = []
    if (
        drift_window_days is not None
        and 0 < drift_window_days < days
        and advisory_scores
    ):
        recent_since = datetime.now(tz=UTC) - timedelta(days=drift_window_days)
        recent = _aggregate_advisory_outcomes(
            pack_advisories, pack_feedback, pack_occurred_at, since=recent_since
        )
        drift_alerts = _detect_drift_alerts(
            advisory_scores,
            recent,
            window_days=days,
            recent_window_days=drift_window_days,
            min_recent_presentations=_DRIFT_MIN_RECENT_PRESENTATIONS,
        )

    return AdvisoryEffectivenessReport(
        total_packs_with_advisories=full.packs_with_any_advisory,
        total_feedback=full.total_feedback,
        advisory_scores=advisory_scores,
        advisories_boosted=[],
        advisories_suppressed=[],
        advisories_drifting=drift_alerts,
    )


def run_advisory_fitness_loop(
    event_log: EventLog,
    advisory_store: AdvisoryStore,
    *,
    days: int = 30,
    min_presentations: int | None = None,
    suppress_below: float | None = None,
    blend_weight: float | None = None,
    drift_window_days: int | None = _DEFAULT_DRIFT_WINDOW_DAYS,
    registry: ParameterRegistry | None = None,
) -> AdvisoryEffectivenessReport:
    """Analyze advisory effectiveness and adjust confidence accordingly.

    This is the advisory-level analogue of :func:`run_effectiveness_feedback`:

    1. Run :func:`analyze_advisory_effectiveness` to score each advisory.
    2. Adjust confidence: blend original confidence with observed fitness.
    3. Suppress advisories whose adjusted confidence falls below threshold.
    4. Persist changes to the advisory store.

    The confidence update formula is::

        new_confidence = (1 - blend_weight) * old_confidence
                       + blend_weight * observed_success_rate

    This ensures that advisories which consistently correlate with failure
    lose confidence and are eventually suppressed, while those that
    correlate with success gain confidence.

    Args:
        event_log: The event log to query.
        advisory_store: Store to read and update advisories.
        days: Time window for analysis.
        min_presentations: Minimum advisory presentations to score.
        suppress_below: Confidence threshold below which advisories
            are removed from the store.
        blend_weight: How much the observed success rate influences
            the confidence (0.0 = keep original, 1.0 = fully replace).

    Returns:
        AdvisoryEffectivenessReport including lists of boosted and
        suppressed advisory IDs.
    """
    if min_presentations is None:
        min_presentations = _resolve_param(
            registry,
            _ADVISORY_COMPONENT,
            "min_presentations",
            _ADVISORY_MIN_PRESENTATIONS,
        )
    if suppress_below is None:
        suppress_below = _resolve_param(
            registry,
            _ADVISORY_COMPONENT,
            "suppress_confidence",
            _ADVISORY_SUPPRESS_CONFIDENCE,
        )
    if blend_weight is None:
        blend_weight = _resolve_param(
            registry,
            _ADVISORY_COMPONENT,
            "blend_weight",
            _CONFIDENCE_BLEND_WEIGHT,
        )

    report = analyze_advisory_effectiveness(
        event_log,
        advisory_store,
        days=days,
        min_presentations=min_presentations,
        drift_window_days=drift_window_days,
        registry=registry,
    )

    # Emit drift events before confidence updates so operators see the
    # regime-shift signal distinct from the smoothed blend that follows.
    for alert in report.advisories_drifting:
        _emit_drift_event(event_log, alert)
        logger.info(
            "advisory_drift_detected",
            advisory_id=alert.advisory_id,
            pattern=alert.pattern,
            full_success_rate=alert.full_success_rate,
            recent_success_rate=alert.recent_success_rate,
            full_lift=alert.full_lift,
            recent_lift=alert.recent_lift,
        )

    boosted: list[str] = []
    suppressed: list[str] = []
    restored: list[str] = []
    restore_above = suppress_below + _RESTORE_HYSTERESIS

    for score in report.advisory_scores:
        # Score suppressed advisories too so they can be restored when
        # new evidence shifts their fitness back above the threshold.
        advisory = advisory_store.get(score.advisory_id)
        if advisory is None:
            continue

        from trellis.schemas.advisory import AdvisoryStatus  # noqa: PLC0415

        old_confidence = advisory.confidence
        is_suppressed = advisory.status == AdvisoryStatus.SUPPRESSED
        # Blend original confidence with observed fitness
        new_confidence = round(
            (1 - blend_weight) * old_confidence + blend_weight * score.success_rate,
            3,
        )

        if is_suppressed:
            # Suppressed branch: only two outcomes — restore (with margin)
            # or remain suppressed. Confidence is still updated either way
            # so the store reflects the latest evidence.
            updated = advisory.model_copy(update={"confidence": new_confidence})
            advisory_store.put(updated)
            if new_confidence >= restore_above:
                advisory_store.restore(score.advisory_id)
                restored.append(score.advisory_id)
                _emit_advisory_event(
                    event_log,
                    EventType.ADVISORY_RESTORED,
                    advisory_id=score.advisory_id,
                    old_confidence=old_confidence,
                    new_confidence=new_confidence,
                    lift=score.lift,
                )
                logger.info(
                    "advisory_restored",
                    advisory_id=score.advisory_id,
                    old_confidence=old_confidence,
                    new_confidence=new_confidence,
                    success_rate=score.success_rate,
                )
            else:
                logger.debug(
                    "advisory_remains_suppressed",
                    advisory_id=score.advisory_id,
                    new_confidence=new_confidence,
                    restore_threshold=restore_above,
                )
        elif new_confidence < suppress_below:
            reason = (
                f"confidence {new_confidence:.3f} < suppress_below "
                f"{suppress_below:.3f} (success_rate={score.success_rate:.3f}, "
                f"presentations={score.presentations})"
            )
            # Soft-suppress: flip status, keep the record for potential
            # restoration. Persist new confidence first so scoring remains
            # consistent with evidence on subsequent passes.
            updated = advisory.model_copy(update={"confidence": new_confidence})
            advisory_store.put(updated)
            advisory_store.suppress(score.advisory_id, reason=reason)
            suppressed.append(score.advisory_id)
            _emit_advisory_event(
                event_log,
                EventType.ADVISORY_SUPPRESSED,
                advisory_id=score.advisory_id,
                old_confidence=old_confidence,
                new_confidence=new_confidence,
                lift=score.lift,
                reason=reason,
            )
            logger.info(
                "advisory_suppressed",
                advisory_id=score.advisory_id,
                old_confidence=old_confidence,
                new_confidence=new_confidence,
                success_rate=score.success_rate,
                presentations=score.presentations,
            )
        else:
            # Active branch: adjust confidence, record boost if applicable.
            updated = advisory.model_copy(update={"confidence": new_confidence})
            advisory_store.put(updated)
            if new_confidence > old_confidence:
                boosted.append(score.advisory_id)
            logger.info(
                "advisory_confidence_adjusted",
                advisory_id=score.advisory_id,
                old_confidence=old_confidence,
                new_confidence=new_confidence,
                lift=score.lift,
            )

    # Return report with boosted/suppressed/restored lists filled in
    return report.model_copy(
        update={
            "advisories_boosted": boosted,
            "advisories_suppressed": suppressed,
            "advisories_restored": restored,
        }
    )


_FITNESS_EVENT_SOURCE = "retrieve.effectiveness.fitness_loop"


def _emit_fitness_event(
    event_log: EventLog,
    event_type: EventType,
    *,
    advisory_id: str,
    payload: dict[str, Any],
) -> None:
    """Emit a fitness-loop advisory event, fail-soft.

    Store writes in the fitness loop are the source of truth
    (suppression status, confidence). Event emission is an audit
    side-effect that must never block the underlying update.
    """
    try:
        event_log.emit(
            event_type,
            source=_FITNESS_EVENT_SOURCE,
            entity_id=advisory_id,
            entity_type="advisory",
            payload=payload,
        )
    except Exception:
        logger.exception(
            "advisory_fitness_event_emit_failed",
            event_type=event_type.value,
            advisory_id=advisory_id,
        )


def _emit_drift_event(event_log: EventLog, alert: AdvisoryDriftAlert) -> None:
    _emit_fitness_event(
        event_log,
        EventType.ADVISORY_DRIFT_DETECTED,
        advisory_id=alert.advisory_id,
        payload=alert.model_dump(mode="json"),
    )


def _emit_advisory_event(
    event_log: EventLog,
    event_type: EventType,
    *,
    advisory_id: str,
    old_confidence: float,
    new_confidence: float,
    lift: float,
    reason: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "advisory_id": advisory_id,
        "old_confidence": old_confidence,
        "new_confidence": new_confidence,
        "lift": lift,
    }
    if reason is not None:
        payload["reason"] = reason
    _emit_fitness_event(event_log, event_type, advisory_id=advisory_id, payload=payload)
