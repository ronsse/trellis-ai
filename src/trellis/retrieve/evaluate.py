"""Pack quality evaluation -- assembly-time scoring across multiple dimensions.

This module measures *pack properties at assembly time* against a declared
scenario. It is the complement to :mod:`trellis.retrieve.effectiveness`, which
measures pack-to-outcome correlation at runtime from :class:`FEEDBACK_RECORDED`
events.

Pack quality evaluation answers: "given that we know what the agent needs,
does the pack we assembled actually look like a good one?" Runtime
effectiveness answers: "did the agent succeed after using this pack?" Both are
necessary and neither subsumes the other.

Entry points:

* :func:`evaluate_pack` — score a single pack against a scenario, optionally
  weighted by an :class:`EvaluationProfile`.
* :class:`QualityDimension` — Protocol for custom scorers.
* Built-in scorers: :class:`CompletenessScorer`, :class:`RelevanceScorer`,
  :class:`NoiseScorer`, :class:`BreadthScorer`, :class:`EfficiencyScorer`.
* Built-in profiles: :data:`CODE_GENERATION_PROFILE`,
  :data:`DOMAIN_CONTEXT_PROFILE`.

Calibration of profile weights (which dimensions actually predict success) is
deliberately out of scope here — see the Pack Quality P3 entry in ``TODO.md``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog
from pydantic import Field, model_validator

from trellis.core.base import TrellisModel
from trellis.schemas.pack import Pack, PackItem

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

_WEIGHT_SUM_TOLERANCE = 1e-6

# Finding thresholds — coarse operator-facing signals, not tuning knobs.
_MIN_COVERAGE_PREVIEW = 5
_LOW_RELEVANCE_THRESHOLD = 0.3
_CLEAN_NOISE_THRESHOLD = 0.7
_BREADTH_GAP_THRESHOLD = 0.5
_LOW_EFFICIENCY_THRESHOLD = 0.3


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class EvaluationScenario(TrellisModel):
    """Ground truth describing what a pack *should* contain for an intent.

    Scenarios are defined by downstream projects (domain-specific fixtures)
    and consumed by the generic scorers here. Everything is optional except
    ``intent`` so partial scenarios still score what they can.
    """

    name: str
    intent: str
    domain: str | None = None
    seed_entity_ids: list[str] = Field(default_factory=list)
    required_coverage: list[str] = Field(default_factory=list)
    expected_categories: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationProfile(TrellisModel):
    """Named weight set for aggregating dimension scores into a single score.

    Weights must sum to 1.0. Dimensions absent from ``weights`` are excluded
    from the weighted aggregate but still reported per-dimension in the
    :class:`QualityReport`.
    """

    name: str
    weights: dict[str, float]

    @model_validator(mode="after")
    def _validate_weights(self) -> EvaluationProfile:
        if not self.weights:
            msg = "EvaluationProfile.weights cannot be empty"
            raise ValueError(msg)
        for dim, weight in self.weights.items():
            if weight < 0.0 or weight > 1.0:
                msg = f"weight for {dim!r} must be in [0, 1], got {weight}"
                raise ValueError(msg)
        total = sum(self.weights.values())
        if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
            msg = f"EvaluationProfile weights must sum to 1.0, got {total}"
            raise ValueError(msg)
        return self


class QualityReport(TrellisModel):
    """Result of scoring a pack against a scenario."""

    scenario_name: str
    pack_id: str | None = None
    profile_name: str | None = None
    dimensions: dict[str, float] = Field(default_factory=dict)
    weighted_score: float = 0.0
    missing_coverage: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Dimension protocol + built-in scorers
# ---------------------------------------------------------------------------


@runtime_checkable
class QualityDimension(Protocol):
    """A single scoring dimension. Implementations must be pure."""

    @property
    def name(self) -> str:  # pragma: no cover - structural
        ...

    def score(
        self,
        pack: Pack,
        scenario: EvaluationScenario,
    ) -> float:
        """Return a score in [0, 1]."""
        ...


def _item_excerpt_lower(item: PackItem) -> str:
    return item.excerpt.lower()


def _item_domains(item: PackItem) -> list[str]:
    """Extract domain tags from a PackItem's metadata, defensively.

    Supports both flat (``metadata["domain"] = [...]``) and nested
    (``metadata["content_tags"]["domain"] = [...]``) layouts because the
    Pack schema stores arbitrary metadata dicts.
    """
    meta = item.metadata or {}
    nested = meta.get("content_tags")
    if isinstance(nested, dict):
        domains = nested.get("domain")
        if isinstance(domains, list):
            return [str(d) for d in domains]
    flat = meta.get("domain")
    if isinstance(flat, list):
        return [str(d) for d in flat]
    if isinstance(flat, str):
        return [flat]
    return []


def _item_content_type(item: PackItem) -> str | None:
    meta = item.metadata or {}
    nested = meta.get("content_tags")
    if isinstance(nested, dict):
        ct = nested.get("content_type")
        if isinstance(ct, str):
            return ct
    flat = meta.get("content_type")
    if isinstance(flat, str):
        return flat
    return None


def _item_tokens(item: PackItem) -> int:
    """Estimated tokens for an item, matching the PackBuilder heuristic."""
    if item.estimated_tokens is not None:
        return max(0, int(item.estimated_tokens))
    return len(item.excerpt) // 4 + 1


class CompletenessScorer:
    """Fraction of ``required_coverage`` keywords present in pack excerpts.

    Case-insensitive substring match. An empty ``required_coverage`` returns
    1.0 (nothing to miss).
    """

    name = "completeness"

    def score(self, pack: Pack, scenario: EvaluationScenario) -> float:
        required = scenario.required_coverage
        if not required:
            return 1.0
        excerpts = [_item_excerpt_lower(i) for i in pack.items]
        hits = sum(
            1 for kw in required if any(kw.lower() in excerpt for excerpt in excerpts)
        )
        return hits / len(required)

    def missing(self, pack: Pack, scenario: EvaluationScenario) -> list[str]:
        excerpts = [_item_excerpt_lower(i) for i in pack.items]
        return [
            kw
            for kw in scenario.required_coverage
            if not any(kw.lower() in excerpt for excerpt in excerpts)
        ]


class RelevanceScorer:
    """Mean ``relevance_score`` across pack items."""

    name = "relevance"

    def score(
        self,
        pack: Pack,
        scenario: EvaluationScenario,  # noqa: ARG002 - protocol signature
    ) -> float:
        if not pack.items:
            return 0.0
        scores = [max(0.0, min(1.0, i.relevance_score)) for i in pack.items]
        return sum(scores) / len(scores)


class NoiseScorer:
    """Fraction of pack items whose tagged domain matches the scenario domain.

    Score is ``1 - (mismatched_items / scored_items)`` so higher = cleaner pack.
    Items with no domain metadata are excluded from both numerator and
    denominator (they can't be judged). When ``scenario.domain`` is None the
    dimension returns 1.0 (no noise filter available).
    """

    name = "noise"

    def score(self, pack: Pack, scenario: EvaluationScenario) -> float:
        if scenario.domain is None:
            return 1.0
        target = scenario.domain.lower()
        scored = 0
        mismatched = 0
        for item in pack.items:
            domains = [d.lower() for d in _item_domains(item)]
            if not domains:
                continue
            scored += 1
            if "all" in domains or target in domains:
                continue
            mismatched += 1
        if scored == 0:
            return 1.0
        return 1.0 - (mismatched / scored)


class BreadthScorer:
    """Fraction of ``expected_categories`` represented by item content_types.

    Empty ``expected_categories`` returns 1.0.
    """

    name = "breadth"

    def score(self, pack: Pack, scenario: EvaluationScenario) -> float:
        expected = scenario.expected_categories
        if not expected:
            return 1.0
        present = {ct for item in pack.items if (ct := _item_content_type(item))}
        hits = sum(1 for category in expected if category in present)
        return hits / len(expected)


class EfficiencyScorer:
    """Fraction of pack tokens carried by items covering required keywords.

    A deterministic proxy for "useful tokens / total tokens" — items that
    touch at least one ``required_coverage`` keyword are counted as useful.
    When ``required_coverage`` is empty the dimension returns 1.0 (nothing
    to judge against). When the pack has no tokens returns 0.0.
    """

    name = "efficiency"

    def score(self, pack: Pack, scenario: EvaluationScenario) -> float:
        if not scenario.required_coverage:
            return 1.0
        total = sum(_item_tokens(i) for i in pack.items)
        if total == 0:
            return 0.0
        lowered = [kw.lower() for kw in scenario.required_coverage]
        useful = sum(
            _item_tokens(i)
            for i in pack.items
            if any(kw in _item_excerpt_lower(i) for kw in lowered)
        )
        return useful / total


DEFAULT_DIMENSIONS: tuple[QualityDimension, ...] = (
    CompletenessScorer(),
    RelevanceScorer(),
    NoiseScorer(),
    BreadthScorer(),
    EfficiencyScorer(),
)


# ---------------------------------------------------------------------------
# Built-in profiles (from fd-poc learnings, 2026-04-05)
# ---------------------------------------------------------------------------


CODE_GENERATION_PROFILE = EvaluationProfile(
    name="code_generation",
    weights={
        "completeness": 0.35,
        "relevance": 0.25,
        "noise": 0.20,
        "breadth": 0.10,
        "efficiency": 0.10,
    },
)

DOMAIN_CONTEXT_PROFILE = EvaluationProfile(
    name="domain_context",
    weights={
        "completeness": 0.20,
        "relevance": 0.20,
        "noise": 0.15,
        "breadth": 0.30,
        "efficiency": 0.15,
    },
)

BUILTIN_PROFILES: dict[str, EvaluationProfile] = {
    CODE_GENERATION_PROFILE.name: CODE_GENERATION_PROFILE,
    DOMAIN_CONTEXT_PROFILE.name: DOMAIN_CONTEXT_PROFILE,
}


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def evaluate_pack(
    pack: Pack,
    scenario: EvaluationScenario,
    profile: EvaluationProfile | None = None,
    dimensions: list[QualityDimension] | None = None,
) -> QualityReport:
    """Score ``pack`` against ``scenario`` across all dimensions.

    Parameters
    ----------
    pack:
        The assembled context pack to score.
    scenario:
        The ground-truth description of what this pack should cover.
    profile:
        Optional weight profile. When omitted the report's
        ``weighted_score`` is a simple mean across all dimensions. When
        provided, dimensions absent from ``profile.weights`` are still
        reported per-dimension but excluded from the weighted aggregate.
    dimensions:
        Optional override for the set of scorers to apply. Defaults to
        the five built-in scorers.
    """
    dims = dimensions if dimensions is not None else list(DEFAULT_DIMENSIONS)
    scores: dict[str, float] = {}
    for dim in dims:
        raw = dim.score(pack, scenario)
        scores[dim.name] = max(0.0, min(1.0, raw))

    if profile is not None:
        covered = {name: scores[name] for name in profile.weights if name in scores}
        if covered:
            weighted = sum(
                profile.weights[name] * value for name, value in covered.items()
            )
            used_weight = sum(profile.weights[name] for name in covered)
            weighted_score = weighted / used_weight if used_weight > 0 else 0.0
        else:
            weighted_score = 0.0
    else:
        weighted_score = sum(scores.values()) / len(scores) if scores else 0.0

    missing_coverage: list[str] = []
    for dim in dims:
        if isinstance(dim, CompletenessScorer):
            missing_coverage = dim.missing(pack, scenario)
            break

    findings = _build_findings(scores, missing_coverage, scenario)

    report = QualityReport(
        scenario_name=scenario.name,
        pack_id=pack.pack_id,
        profile_name=profile.name if profile else None,
        dimensions=scores,
        weighted_score=weighted_score,
        missing_coverage=missing_coverage,
        findings=findings,
    )
    logger.debug(
        "pack_quality_evaluated",
        pack_id=pack.pack_id,
        scenario=scenario.name,
        profile=report.profile_name,
        weighted_score=weighted_score,
        dimensions=scores,
    )
    return report


def _build_findings(
    scores: dict[str, float],
    missing_coverage: list[str],
    scenario: EvaluationScenario,
) -> list[str]:
    findings: list[str] = []
    if missing_coverage:
        preview = ", ".join(missing_coverage[:_MIN_COVERAGE_PREVIEW])
        suffix = "" if len(missing_coverage) <= _MIN_COVERAGE_PREVIEW else " ..."
        findings.append(
            f"completeness: {len(missing_coverage)} required keyword(s) "
            f"missing ({preview}{suffix})"
        )
    if scores.get("relevance", 1.0) < _LOW_RELEVANCE_THRESHOLD:
        findings.append(
            "relevance: mean item relevance_score is below 0.3 — consider "
            "tighter keyword/semantic filters or stronger entity seeds"
        )
    if scenario.domain and scores.get("noise", 1.0) < _CLEAN_NOISE_THRESHOLD:
        findings.append(
            f"noise: >30% of tagged items are outside domain "
            f"{scenario.domain!r} — check domain filter wiring"
        )
    if (
        scores.get("breadth", 1.0) < _BREADTH_GAP_THRESHOLD
        and scenario.expected_categories
    ):
        findings.append(
            "breadth: less than half of expected content categories are "
            "present — classification gap or retrieval under-fetch"
        )
    if scores.get("efficiency", 1.0) < _LOW_EFFICIENCY_THRESHOLD:
        findings.append(
            "efficiency: less than 30% of pack tokens reference required "
            "keywords — budget may be spent on tangential items"
        )
    return findings


# ---------------------------------------------------------------------------
# Dimension predictiveness — do KPIs actually predict task success?
# ---------------------------------------------------------------------------
#
# Joins PACK_QUALITY_SCORED events (emitted by PackBuilder when an evaluator
# is wired) against FEEDBACK_RECORDED events to answer: "for each quality
# dimension, does a higher score correlate with task success?"
#
# This is the prerequisite for auto-calibration of profile weights (P3 in
# TODO.md). Before auto-tuning, we must demonstrate which dimensions are
# actually signal — a dimension with |r| < 0.1 across a meaningful sample is
# a candidate for weight reduction; strong positive correlation justifies
# boosting. The analysis is read-only: no mutation of profiles, scorers, or
# classification state.

#: Minimum samples (matched pack_id across both event types) before the
#: correlation is reported as meaningful. Below this the dimension is still
#: reported but flagged ``INSUFFICIENT_DATA``.
_PREDICTIVENESS_MIN_SAMPLES = 20

#: Correlation magnitude thresholds. These are deliberately coarse — we are
#: separating signal from noise, not doing precise effect-size estimation.
_NOISE_CORRELATION_THRESHOLD = 0.1
_MODERATE_CORRELATION_THRESHOLD = 0.3
_STRONG_CORRELATION_THRESHOLD = 0.5

_PREDICTIVENESS_EVENT_LIMIT = 5000

#: Minimum observations for Pearson correlation to be mathematically defined.
_PEARSON_MIN_SAMPLES = 2


class DimensionPredictiveness(TrellisModel):
    """Predictiveness of a single quality dimension against success feedback."""

    dimension: str
    sample_count: int
    correlation: float | None  # None when undefined (zero variance, <2 samples)
    mean_score_on_success: float | None
    mean_score_on_failure: float | None
    signal_classification: str  # strong | moderate | weak | noise | insufficient_data


class DimensionPredictivenessReport(TrellisModel):
    """Report on which dimensions predict task success."""

    total_packs_scored: int
    total_matched_feedback: int
    overall_success_rate: float
    dimensions: list[DimensionPredictiveness] = Field(default_factory=list)
    weighted_score_predictiveness: DimensionPredictiveness | None = None
    notes: list[str] = Field(default_factory=list)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation between two equal-length lists.

    Returns ``None`` when correlation is undefined — fewer than 2 samples
    or zero variance in either variable. For a binary ``ys`` (success/fail),
    this is the point-biserial correlation.
    """
    n = len(xs)
    if n < _PEARSON_MIN_SAMPLES or len(ys) != n:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0.0 or var_y == 0.0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
    return cov / math.sqrt(var_x * var_y)


def _classify_signal(correlation: float | None, sample_count: int) -> str:
    if sample_count < _PREDICTIVENESS_MIN_SAMPLES:
        return "insufficient_data"
    if correlation is None:
        return "insufficient_data"
    magnitude = abs(correlation)
    if magnitude >= _STRONG_CORRELATION_THRESHOLD:
        return "strong"
    if magnitude >= _MODERATE_CORRELATION_THRESHOLD:
        return "moderate"
    if magnitude >= _NOISE_CORRELATION_THRESHOLD:
        return "weak"
    return "noise"


def analyze_dimension_predictiveness(  # noqa: PLR0912, PLR0915
    event_log: EventLog,
    *,
    days: int = 30,
    success_threshold: float = 0.5,
) -> DimensionPredictivenessReport:
    """Correlate quality-dimension scores with task success.

    Joins :attr:`PACK_QUALITY_SCORED` events with :attr:`FEEDBACK_RECORDED`
    events by ``pack_id``. For each dimension observed, computes the Pearson
    correlation between dimension score and success (0/1) — mathematically
    equivalent to the point-biserial correlation.

    Signal classification:

    * ``strong`` — ``|r| >= 0.5``
    * ``moderate`` — ``|r| >= 0.3``
    * ``weak`` — ``|r| >= 0.1``
    * ``noise`` — ``|r| < 0.1`` (candidate for weight reduction)
    * ``insufficient_data`` — fewer than the minimum sample count, or undefined

    The report is read-only — no mutation of profiles, scorers, or
    classification state. Auto-calibration of profile weights is separate
    P3 work that depends on this analysis as its substrate.
    """
    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    since = datetime.now(tz=UTC) - timedelta(days=days)
    quality_events = event_log.get_events(
        event_type=EventType.PACK_QUALITY_SCORED,
        since=since,
        limit=_PREDICTIVENESS_EVENT_LIMIT,
    )
    feedback_events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        since=since,
        limit=_PREDICTIVENESS_EVENT_LIMIT,
    )

    #: pack_id -> {dimension -> score, "_weighted": score}
    pack_scores: dict[str, dict[str, float]] = {}
    for event in quality_events:
        pack_id = event.payload.get("pack_id") or event.entity_id
        if not pack_id:
            continue
        dims = event.payload.get("dimensions") or {}
        if not isinstance(dims, dict):
            continue
        record: dict[str, float] = {}
        for name, value in dims.items():
            if isinstance(value, int | float):
                record[str(name)] = float(value)
        weighted = event.payload.get("weighted_score")
        if isinstance(weighted, int | float):
            record["_weighted"] = float(weighted)
        if record:
            pack_scores[pack_id] = record

    #: pack_id -> success_bool. Latest feedback wins when duplicated.
    pack_success: dict[str, bool] = {}
    for event in feedback_events:
        pack_id = event.payload.get("pack_id") or event.entity_id
        if not pack_id or pack_id not in pack_scores:
            continue
        explicit = event.payload.get("success")
        if isinstance(explicit, bool):
            pack_success[pack_id] = explicit
            continue
        rating = event.payload.get("rating")
        if isinstance(rating, int | float):
            pack_success[pack_id] = bool(rating >= success_threshold)

    matched_ids = list(pack_success.keys())
    matched_feedback = len(matched_ids)

    per_dim_xs: dict[str, list[float]] = defaultdict(list)
    per_dim_ys: dict[str, list[float]] = defaultdict(list)
    for pack_id in matched_ids:
        y = 1.0 if pack_success[pack_id] else 0.0
        for name, score in pack_scores[pack_id].items():
            per_dim_xs[name].append(score)
            per_dim_ys[name].append(y)

    successes = sum(1 for pid in matched_ids if pack_success[pid])
    overall_success_rate = successes / matched_feedback if matched_feedback > 0 else 0.0

    dimensions: list[DimensionPredictiveness] = []
    weighted_entry: DimensionPredictiveness | None = None
    for name in sorted(per_dim_xs):
        xs = per_dim_xs[name]
        ys = per_dim_ys[name]
        r = _pearson(xs, ys)
        n_success = sum(1 for y in ys if y > 0.5)  # noqa: PLR2004
        n_failure = len(ys) - n_success
        mean_success = (
            sum(x for x, y in zip(xs, ys, strict=False) if y > 0.5) / n_success  # noqa: PLR2004
            if n_success > 0
            else None
        )
        mean_failure = (
            sum(x for x, y in zip(xs, ys, strict=False) if y <= 0.5) / n_failure  # noqa: PLR2004
            if n_failure > 0
            else None
        )
        entry = DimensionPredictiveness(
            dimension=name if name != "_weighted" else "weighted_score",
            sample_count=len(xs),
            correlation=r,
            mean_score_on_success=mean_success,
            mean_score_on_failure=mean_failure,
            signal_classification=_classify_signal(r, len(xs)),
        )
        if name == "_weighted":
            weighted_entry = entry
        else:
            dimensions.append(entry)

    notes: list[str] = []
    if matched_feedback == 0:
        notes.append(
            "No packs have both PACK_QUALITY_SCORED and FEEDBACK_RECORDED "
            "events in this window. Wire a PackBuilder evaluator and collect "
            "feedback via record_feedback before this report becomes useful."
        )
    elif matched_feedback < _PREDICTIVENESS_MIN_SAMPLES:
        notes.append(
            f"Only {matched_feedback} matched pack(s) in this window — "
            f"below the {_PREDICTIVENESS_MIN_SAMPLES}-sample threshold for "
            f"reliable correlation. All dimensions will report as "
            f"insufficient_data."
        )
    noise_dims = [d.dimension for d in dimensions if d.signal_classification == "noise"]
    if noise_dims:
        notes.append(
            "Dimensions classified as noise (|r| < 0.1) are candidates for "
            f"weight reduction in profiles: {', '.join(noise_dims)}."
        )

    report = DimensionPredictivenessReport(
        total_packs_scored=len(pack_scores),
        total_matched_feedback=matched_feedback,
        overall_success_rate=overall_success_rate,
        dimensions=dimensions,
        weighted_score_predictiveness=weighted_entry,
        notes=notes,
    )
    logger.info(
        "dimension_predictiveness_analyzed",
        days=days,
        packs_scored=len(pack_scores),
        matched_feedback=matched_feedback,
        success_rate=overall_success_rate,
    )
    return report


__all__ = [
    "BUILTIN_PROFILES",
    "BreadthScorer",
    "CODE_GENERATION_PROFILE",
    "CompletenessScorer",
    "DEFAULT_DIMENSIONS",
    "DOMAIN_CONTEXT_PROFILE",
    "DimensionPredictiveness",
    "DimensionPredictivenessReport",
    "EfficiencyScorer",
    "EvaluationProfile",
    "EvaluationScenario",
    "NoiseScorer",
    "QualityDimension",
    "QualityReport",
    "RelevanceScorer",
    "analyze_dimension_predictiveness",
    "evaluate_pack",
]
