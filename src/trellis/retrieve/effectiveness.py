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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.base import TrellisModel
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


class AdvisoryEffectivenessReport(TrellisModel):
    """Report on advisory-outcome correlation."""

    total_packs_with_advisories: int
    total_feedback: int
    advisory_scores: list[AdvisoryScore]
    advisories_boosted: list[str]
    advisories_suppressed: list[str]


def analyze_advisory_effectiveness(  # noqa: PLR0912 — registry resolution adds one branch beyond the limit, but the logic is a straight-line analysis pipeline
    event_log: EventLog,
    advisory_store: AdvisoryStore,  # noqa: ARG001 — kept for API symmetry with run_advisory_fitness_loop
    *,
    days: int = 30,
    min_presentations: int | None = None,
    registry: ParameterRegistry | None = None,
) -> AdvisoryEffectivenessReport:
    """Analyze how advisories correlate with pack outcomes.

    Joins PACK_ASSEMBLED events (which include ``advisory_ids``) with
    FEEDBACK_RECORDED events to compute per-advisory success rates and
    compare against baseline (packs without the advisory).

    Args:
        event_log: The event log to query.
        advisory_store: Advisory store for looking up advisory metadata.
        days: How many days of history to analyze.
        min_presentations: Minimum times an advisory must be presented
            before it is scored.

    Returns:
        AdvisoryEffectivenessReport with per-advisory fitness scores.
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

    # Build pack_id -> advisory_ids mapping
    pack_advisories: dict[str, list[str]] = {}
    for event in pack_events:
        pack_id = event.entity_id
        if pack_id:
            pack_advisories[pack_id] = event.payload.get("advisory_ids", [])

    # Build pack_id -> success mapping
    pack_feedback: dict[str, bool] = {}
    for event in feedback_events:
        pack_id = event.payload.get("pack_id") or event.entity_id
        if pack_id and pack_id in pack_advisories:
            rating = event.payload.get("rating", 0.0)
            pack_feedback[pack_id] = event.payload.get(
                "success", rating >= success_threshold
            )

    # Count per-advisory outcomes
    adv_successes: dict[str, int] = defaultdict(int)
    adv_failures: dict[str, int] = defaultdict(int)
    adv_presentations: dict[str, int] = defaultdict(int)

    packs_with_any_advisory = 0
    for pack_id, advisory_ids in pack_advisories.items():
        if pack_id not in pack_feedback:
            continue
        if advisory_ids:
            packs_with_any_advisory += 1
        success = pack_feedback[pack_id]
        for adv_id in advisory_ids:
            adv_presentations[adv_id] += 1
            if success:
                adv_successes[adv_id] += 1
            else:
                adv_failures[adv_id] += 1

    # Overall baseline: success rate across all packs with feedback
    total_feedback = len(pack_feedback)
    total_successes = sum(1 for v in pack_feedback.values() if v)
    overall_baseline = total_successes / total_feedback if total_feedback > 0 else 0.0

    # Score each advisory
    advisory_scores: list[AdvisoryScore] = []
    for adv_id, presentations in adv_presentations.items():
        if presentations < min_presentations:
            continue

        s = adv_successes.get(adv_id, 0)
        f = adv_failures.get(adv_id, 0)
        rate = s / presentations

        # Baseline: success rate of packs *without* this advisory
        packs_without = total_feedback - presentations
        success_without = total_successes - s
        baseline = (
            success_without / packs_without if packs_without > 0 else overall_baseline
        )

        advisory_scores.append(
            AdvisoryScore(
                advisory_id=adv_id,
                presentations=presentations,
                successes=s,
                failures=f,
                success_rate=round(rate, 3),
                baseline_rate=round(baseline, 3),
                lift=round(rate - baseline, 3),
            )
        )

    advisory_scores.sort(key=lambda x: x.lift, reverse=True)

    return AdvisoryEffectivenessReport(
        total_packs_with_advisories=packs_with_any_advisory,
        total_feedback=total_feedback,
        advisory_scores=advisory_scores,
        advisories_boosted=[],
        advisories_suppressed=[],
    )


def run_advisory_fitness_loop(
    event_log: EventLog,
    advisory_store: AdvisoryStore,
    *,
    days: int = 30,
    min_presentations: int | None = None,
    suppress_below: float | None = None,
    blend_weight: float | None = None,
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
        registry=registry,
    )

    boosted: list[str] = []
    suppressed: list[str] = []

    for score in report.advisory_scores:
        advisory = advisory_store.get(score.advisory_id)
        if advisory is None:
            continue

        old_confidence = advisory.confidence
        # Blend original confidence with observed fitness
        new_confidence = round(
            (1 - blend_weight) * old_confidence + blend_weight * score.success_rate,
            3,
        )

        if new_confidence < suppress_below:
            advisory_store.remove(score.advisory_id)
            suppressed.append(score.advisory_id)
            logger.info(
                "advisory_suppressed",
                advisory_id=score.advisory_id,
                old_confidence=old_confidence,
                new_confidence=new_confidence,
                success_rate=score.success_rate,
                presentations=score.presentations,
            )
        else:
            # Update confidence on the advisory
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

    # Return report with boosted/suppressed lists filled in
    return report.model_copy(
        update={
            "advisories_boosted": boosted,
            "advisories_suppressed": suppressed,
        }
    )
