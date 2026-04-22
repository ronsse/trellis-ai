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

from typing import Any, Protocol, runtime_checkable

import structlog
from pydantic import Field, model_validator

from trellis.core.base import TrellisModel
from trellis.schemas.pack import Pack, PackItem

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
            1
            for kw in required
            if any(kw.lower() in excerpt for excerpt in excerpts)
        )
        return hits / len(required)

    def missing(
        self, pack: Pack, scenario: EvaluationScenario
    ) -> list[str]:
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
        present = {
            ct for item in pack.items if (ct := _item_content_type(item))
        }
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
        covered = {
            name: scores[name] for name in profile.weights if name in scores
        }
        if covered:
            weighted = sum(
                profile.weights[name] * value for name, value in covered.items()
            )
            used_weight = sum(profile.weights[name] for name in covered)
            weighted_score = weighted / used_weight if used_weight > 0 else 0.0
        else:
            weighted_score = 0.0
    else:
        weighted_score = (
            sum(scores.values()) / len(scores) if scores else 0.0
        )

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
        suffix = (
            "" if len(missing_coverage) <= _MIN_COVERAGE_PREVIEW else " ..."
        )
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


__all__ = [
    "BUILTIN_PROFILES",
    "BreadthScorer",
    "CODE_GENERATION_PROFILE",
    "CompletenessScorer",
    "DEFAULT_DIMENSIONS",
    "DOMAIN_CONTEXT_PROFILE",
    "EfficiencyScorer",
    "EvaluationProfile",
    "EvaluationScenario",
    "NoiseScorer",
    "QualityDimension",
    "QualityReport",
    "RelevanceScorer",
    "evaluate_pack",
]
