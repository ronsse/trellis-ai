"""Skill-loop convergence scenario — F-phase inner-loop measurement.

Implemented as the **reference-driver build** (issue #249): the
measurement path for axes P / Q / R runs end-to-end against real
Trellis subsystems, with deterministic scenario-local drivers standing
in for the F-phase machinery that has not landed yet.

What is real vs. reference in this build:

- **Real:** every enrichment write goes through the governed mutation
  pipeline (``ENTITY_CREATE`` via ``build_curate_executor``); packs are
  assembled by the real ``PackBuilder``; pack quality is scored by the
  real ``evaluate_pack`` through the assembly-time evaluator hook,
  which emits real ``PACK_QUALITY_SCORED`` events to the real EventLog.
  Axis Q is therefore a genuine measurement: consolidating fragmented
  source notes into a summary document measurably lifts pack quality
  under a fixed item budget.
- **Reference (scenario-local, replaced when F1-F5 land):**
  :class:`_ReferenceCurator` stands in for the F2 curator skill — it
  deterministically consolidates a node's source notes instead of
  running an agent loop. :class:`_ReferenceEvolver` stands in for the
  F5 score-based evolver — its *pruning decisions are driven by the
  real measured pack scores*, but the variant pool itself is synthetic
  (each variant's ``fact_recall`` controls how complete its summaries
  are). Axis R therefore validates the score->prune mechanics and the
  measurement plumbing, **not** a production evolver — do not cite R as
  evidence of F5 value.

The four conceptual phases of a run:

1. **Seed.** Under-populated nodes (governed writes), fragmented
   source notes, and a background corpus + stability panel. See
   :mod:`.seed`.
2. **Loop.** Per period: the reference curator enriches that period's
   node slice (variant-assigned), then the fixed query panel runs
   through ``PackBuilder`` and every pack is scored via the evaluator
   hook.
3. **Evolve.** Every ``periods_per_evolution`` periods the reference
   evolver culls variants whose measured mean pack score trails the
   best variant by more than the margin.
4. **Measure.** Reduce the captured payloads into the three per-axis
   curves. See :mod:`.metrics`.

The F1-F5 seam: when the real harness / curator / evolver land, they
replace the two ``_Reference*`` drivers and the in-memory enrichment
records switch to the F2 ``node.enriched`` event type — the seed
helpers, the panel, the reducers, and the report shape stay as they
are.
"""

from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass, field
from typing import Any

import structlog

from eval.runner import Finding, ScenarioReport
from trellis.mutate import Command, CommandStatus, Operation, build_curate_executor
from trellis.retrieve.evaluate import (
    EvaluationProfile,
    EvaluationScenario,
    evaluate_pack,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas.pack import PackBudget
from trellis.stores.registry import StoreRegistry

from .metrics import (
    CoverageCurve,
    LiftCurve,
    VariantSurvival,
    coverage_curve,
    retrieval_lift_curve,
    variant_survival_rate,
)
from .seed import (
    SKILL_DOMAIN,
    facts_for_node,
    node_name,
    seed_baseline_corpus,
    seed_documents_for_nodes,
    seed_under_populated_nodes,
)

logger = structlog.get_logger(__name__)


SCENARIO_NAME = "skill_loop_convergence"

#: Opt-in env var. Setting to any non-empty value flips ``run()`` from
#: skip into the live path. CI does not set it, so the scenario stays
#: inert by default — it is an operator-run measurement, not a gate.
OPT_IN_ENV_VAR: str = "TRELLIS_EVAL_SKILL_LOOP"

# Default knobs. Conservative for an under-an-hour run on dev hardware;
# unit tests dial them down hard. ``docs_per_node`` sits two above the
# panel budget so a weak variant's dropped facts cannot all be
# back-filled by the pack's remaining budget slots — that headroom is
# what keeps the variant quality differential measurable.
DEFAULT_PERIODS: int = 20
DEFAULT_NODES_PER_PERIOD: int = 5
DEFAULT_DOCS_PER_NODE: int = 4
DEFAULT_PERIODS_PER_EVOLUTION: int = 5
DEFAULT_INITIAL_VARIANT_POOL: int = 4
DEFAULT_TRACES_PER_DOMAIN: int = 6
DEFAULT_ENTITIES_PER_TRACE: int = 3

#: Panel pack budget. ``max_items`` must sit below ``docs_per_node`` so
#: baseline packs cannot cover a node's facts with fragmented notes —
#: that headroom is what consolidation converts into measured lift.
PANEL_MAX_ITEMS: int = 2
PANEL_MAX_TOKENS: int = 1200

#: Evolver cull policy: a variant is culled when its measured mean pack
#: score trails the best variant's by more than this margin at an
#: evolution checkpoint. The pool never shrinks below the floor.
CULL_MARGIN: float = 0.05
MIN_POOL_SIZE: int = 1

#: A score differential needs at least two participants — both the
#: evolver's cull step and the pool-size validation gate on this.
MIN_VARIANTS_FOR_DIFFERENTIAL: int = 2

#: Panel scoring profile. The panel's ground truth is fact coverage, so
#: completeness carries most of the weight — an unweighted six-dimension
#: mean dilutes the coverage differential (the other five dimensions sit
#: near 1.0 on this corpus) below what the evolver's margin can resolve.
_PANEL_PROFILE = EvaluationProfile(
    name="skill_loop_panel",
    weights={"completeness": 0.6, "efficiency": 0.2, "relevance": 0.2},
)

#: Weakest variant's fact recall. Variant ``i`` in the initial pool
#: recalls ``max(FLOOR, 1.0 - i * STEP)`` of a node's facts when it
#: writes the consolidated summary.
VARIANT_RECALL_STEP: float = 0.2
VARIANT_RECALL_FLOOR: float = 0.4


# ---------------------------------------------------------------------------
# Reference drivers — replaced by F1-F5 machinery when it lands
# ---------------------------------------------------------------------------


@dataclass
class _PromptVariant:
    """A synthetic curator prompt variant (F5 stand-in).

    ``fact_recall`` is the hidden quality parameter: the fraction of a
    node's facts this variant's consolidated summary captures. The
    evolver never reads it — culling is driven only by measured pack
    scores, so the scenario checks that measurement alone finds the
    weak variants.
    """

    variant_id: str
    fact_recall: float
    scores: list[float] = field(default_factory=list)

    @property
    def mean_score(self) -> float | None:
        return statistics.fmean(self.scores) if self.scores else None


def _initial_variant_pool(size: int) -> list[_PromptVariant]:
    return [
        _PromptVariant(
            variant_id=f"variant-{i:02d}",
            fact_recall=max(VARIANT_RECALL_FLOOR, 1.0 - i * VARIANT_RECALL_STEP),
        )
        for i in range(size)
    ]


class _ReferenceCurator:
    """Deterministic stand-in for the F2 curator skill.

    Consolidates a node's fragmented source notes into one summary
    document and lands the node's ``description`` through the governed
    pipeline. The assigned variant's ``fact_recall`` bounds how many
    facts the summary captures — the deliberate quality differential
    the evolver has to detect from measurements.
    """

    def __init__(self, registry: StoreRegistry, *, docs_per_node: int) -> None:
        self._registry = registry
        self._executor = build_curate_executor(registry)
        self._docs_per_node = docs_per_node

    def enrich(self, node_id: str, variant: _PromptVariant) -> None:
        facts = facts_for_node(node_id, docs_per_node=self._docs_per_node)
        recalled_count = max(1, math.ceil(variant.fact_recall * len(facts)))
        recalled = facts[:recalled_count]
        name = node_name(node_id)

        summary = (
            f"Consolidated summary of {name}. Key facts: {', '.join(recalled)}. "
            f"Compiled from {self._docs_per_node} source notes."
        )
        self._registry.knowledge.document_store.put(
            doc_id=f"doc:enriched:{node_id}",
            content=summary,
            metadata={
                "entity_id": node_id,
                "domain": SKILL_DOMAIN,
                "domains": [SKILL_DOMAIN],
                "content_type": "consolidated_summary",
                "content_tags": {"signal_quality": "standard"},
                "enriched_by": variant.variant_id,
            },
        )

        # Governed node update: ENTITY_CREATE with an existing entity_id
        # is the SCD-2 upsert path — the prior (description-less) version
        # is closed and a new enriched version opens.
        result = self._executor.execute(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": node_id,
                    "entity_type": "concept",
                    "name": name,
                    "properties": {
                        "name": name,
                        "description": summary,
                        "enriched_by": variant.variant_id,
                    },
                },
                requested_by="eval:skill_loop_convergence:reference_curator",
            )
        )
        if result.status is not CommandStatus.SUCCESS:
            msg = f"enrichment of {node_id!r} failed: {result.message}"
            raise RuntimeError(msg)


class _ReferenceEvolver:
    """Score-based pruning over the variant pool (F5 stand-in).

    Pruning inputs are the *measured* pack scores attributed to each
    variant's enrichments — never the hidden ``fact_recall``. At each
    checkpoint, variants whose mean trails the best scored variant by
    more than :data:`CULL_MARGIN` are culled (down to
    :data:`MIN_POOL_SIZE`). Falling-then-flattening survival is the
    expected signature: weak variants get culled early, then the
    surviving pool is homogeneous and stable.
    """

    def __init__(self, pool: list[_PromptVariant]) -> None:
        self.alive: list[_PromptVariant] = list(pool)
        self.culled_total = 0

    def assign(self, index: int) -> _PromptVariant:
        """Round-robin variant assignment for the ``index``-th enrichment."""
        return self.alive[index % len(self.alive)]

    def evolve(self) -> list[str]:
        """Cull under-performing variants; return culled variant ids."""
        scored = [v for v in self.alive if v.mean_score is not None]
        if len(scored) < MIN_VARIANTS_FOR_DIFFERENTIAL:
            return []
        best = max(score for v in scored if (score := v.mean_score) is not None)
        culled: list[str] = []
        for variant in sorted(scored, key=lambda v: v.mean_score or 0.0):
            if len(self.alive) <= MIN_POOL_SIZE:
                break
            mean = variant.mean_score
            if mean is not None and best - mean > CULL_MARGIN:
                self.alive.remove(variant)
                culled.append(variant.variant_id)
        self.culled_total += len(culled)
        return culled


# ---------------------------------------------------------------------------
# Result aggregate
# ---------------------------------------------------------------------------


@dataclass
class _LoopResult:
    """Output of the inner loop — passed to :func:`_measure`.

    Held as a private dataclass (not :class:`~trellis.core.base.TrellisModel`)
    because it's an internal handoff between phases of ``run()``, not a
    schema artifact. F-phase swarms may freely add fields without breaking
    a public contract.
    """

    periods_completed: int = 0
    nodes_seeded: int = 0
    documents_seeded: int = 0
    seed_node_ids: list[str] = field(default_factory=list)
    baseline_score: float = 0.0
    stability_baseline: float = 0.0
    stability_final: float = 0.0
    initial_variant_pool: int = 0
    variants_culled_total: int = 0
    node_enriched_events: list[dict[str, Any]] = field(default_factory=list)
    pack_quality_events: list[dict[str, Any]] = field(default_factory=list)
    evolver_events: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------


def _opt_in_enabled() -> bool:
    """Return ``True`` iff :data:`OPT_IN_ENV_VAR` is set to a non-empty value."""
    return bool(os.environ.get(OPT_IN_ENV_VAR))


def _skip_report(*, message: str, decision: str) -> ScenarioReport:
    """Return a ``status="skip"`` report with the given info finding."""
    return ScenarioReport(
        name=SCENARIO_NAME,
        status="skip",
        findings=[Finding(severity="info", message=message)],
        decision=decision,
    )


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


def _panel_scenarios(
    node_ids: list[str], *, docs_per_node: int
) -> list[EvaluationScenario]:
    """One fixed evaluation scenario per seed node.

    ``required_coverage`` is the node's name plus every fact token, so
    a pack that retrieves only fragmented notes under the item budget
    scores partial completeness and the consolidated summary scores
    full — the differential axis Q measures.
    """
    panel: list[EvaluationScenario] = []
    for node_id in node_ids:
        name = node_name(node_id)
        facts = facts_for_node(node_id, docs_per_node=docs_per_node)
        panel.append(
            EvaluationScenario(
                name=f"panel:{node_id}",
                intent=f"summarize {name} covering {' '.join(facts)}",
                domain=SKILL_DOMAIN,
                required_coverage=[name, *facts],
                metadata={"node_id": node_id},
            )
        )
    return panel


class _PanelRunner:
    """Runs a fixed query panel through the real PackBuilder + evaluator.

    The evaluator hook needs the ground-truth scenario for the pack it
    is scoring; ``self._current`` carries it across the closure. Every
    scored pack emits a real ``PACK_QUALITY_SCORED`` event because the
    builder is constructed with the registry's EventLog.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._current: EvaluationScenario | None = None
        self._builder = PackBuilder(
            strategies=[KeywordSearch(registry.knowledge.document_store)],
            event_log=registry.operational.event_log,
            evaluator=self._evaluate,
        )

    def _evaluate(self, pack: Any) -> Any:
        if self._current is None:
            return None
        return evaluate_pack(pack, self._current, profile=_PANEL_PROFILE)

    def run(
        self, panel: list[EvaluationScenario]
    ) -> list[tuple[EvaluationScenario, float]]:
        """Build + score one pack per panel scenario; return (scenario, score)."""
        results: list[tuple[EvaluationScenario, float]] = []
        for scenario in panel:
            self._current = scenario
            try:
                pack = self._builder.build(
                    intent=scenario.intent,
                    domain=scenario.domain,
                    budget=PackBudget(
                        max_items=PANEL_MAX_ITEMS, max_tokens=PANEL_MAX_TOKENS
                    ),
                )
            finally:
                self._current = None
            report = pack.metadata.get("quality_report") or {}
            results.append((scenario, float(report.get("weighted_score", 0.0))))
        return results


def _mean_score(results: list[tuple[EvaluationScenario, float]]) -> float:
    return statistics.fmean(score for _, score in results) if results else 0.0


# ---------------------------------------------------------------------------
# Loop phases
# ---------------------------------------------------------------------------


def _seed(
    registry: StoreRegistry,
    *,
    seed: int,
    nodes_per_period: int,
    periods: int,
    docs_per_node: int,
    traces_per_domain: int,
    entities_per_trace: int,
) -> tuple[list[str], int, dict[str, Any]]:
    """Run phase 1 — seed the corpus the loop curates.

    Returns ``(seed_node_ids, document_count, baseline_manifest)``.
    """
    node_count = nodes_per_period * periods
    seed_node_ids = seed_under_populated_nodes(
        registry, seed=seed, node_count=node_count
    )
    doc_count = seed_documents_for_nodes(
        registry, seed_node_ids, seed=seed, docs_per_node=docs_per_node
    )
    baseline_manifest = seed_baseline_corpus(
        registry,
        seed=seed,
        traces_per_domain=traces_per_domain,
        entities_per_trace=entities_per_trace,
    )
    return seed_node_ids, doc_count, baseline_manifest


def _stability_panel(manifest: dict[str, Any]) -> list[EvaluationScenario]:
    """Panel of enrichment-unrelated queries whose scores should stay flat."""
    return [
        EvaluationScenario(
            name=f"stability:{q['domain']}",
            intent=q["intent"],
            domain=q["domain"],
            required_coverage=list(q["required_coverage"]),
        )
        for q in manifest.get("stability_queries", [])
    ]


def _loop(
    registry: StoreRegistry,
    seed_node_ids: list[str],
    *,
    periods: int,
    periods_per_evolution: int,
    initial_variant_pool: int,
    docs_per_node: int,
    baseline_manifest: dict[str, Any],
    run_id: str,
) -> _LoopResult:
    """Run phases 2 + 3 — per-period curate + score-based evolve.

    Reference-driver build: :class:`_ReferenceCurator` enriches each
    period's node slice (variant-assigned round-robin), the fixed panel
    runs through the real ``PackBuilder`` + evaluator hook, per-node
    scores feed variant means, and :class:`_ReferenceEvolver` culls at
    each evolution checkpoint.
    """
    result = _LoopResult(
        seed_node_ids=list(seed_node_ids),
        initial_variant_pool=initial_variant_pool,
    )
    curator = _ReferenceCurator(registry, docs_per_node=docs_per_node)
    evolver = _ReferenceEvolver(_initial_variant_pool(initial_variant_pool))
    panel = _panel_scenarios(seed_node_ids, docs_per_node=docs_per_node)
    runner = _PanelRunner(registry)
    stability = _stability_panel(baseline_manifest)

    # Pre-loop baselines (period stamp -1: excluded from period curves).
    baseline_results = runner.run(panel)
    result.baseline_score = _mean_score(baseline_results)
    result.stability_baseline = _mean_score(runner.run(stability))

    nodes_per_period = len(seed_node_ids) // periods if periods else 0
    enriched_by: dict[str, _PromptVariant] = {}

    for period in range(periods):
        start = period * nodes_per_period
        slice_ids = seed_node_ids[start : start + nodes_per_period]
        for idx, node_id in enumerate(slice_ids):
            variant = evolver.assign(start + idx)
            curator.enrich(node_id, variant)
            enriched_by[node_id] = variant
            result.node_enriched_events.append(
                {
                    "node_id": node_id,
                    "period": period,
                    "variant_id": variant.variant_id,
                    "run_id": run_id,
                }
            )

        # Panel pass — real packs, real scores, real PACK_QUALITY_SCORED
        # events. Scores for nodes enriched THIS period feed their
        # variant's running mean (fresh measurement of that variant's
        # output under the same budget every other node gets).
        for scenario, score in runner.run(panel):
            node_id = scenario.metadata.get("node_id")
            result.pack_quality_events.append(
                {
                    "period": period,
                    "node_id": node_id,
                    "scenario_name": scenario.name,
                    "weighted_score": score,
                }
            )
            if node_id in enriched_by and any(
                e["node_id"] == node_id and e["period"] == period
                for e in result.node_enriched_events
            ):
                enriched_by[node_id].scores.append(score)

        culled: list[str] = []
        if (period + 1) % periods_per_evolution == 0:
            culled = evolver.evolve()
        result.evolver_events.append(
            {
                "period": period,
                "alive": len(evolver.alive),
                "culled": len(culled),
                "culled_ids": culled,
            }
        )

        result.periods_completed = period + 1

    result.stability_final = _mean_score(runner.run(stability))
    result.variants_culled_total = evolver.culled_total
    return result


def _measure(
    loop_result: _LoopResult,
) -> tuple[CoverageCurve, LiftCurve, VariantSurvival]:
    """Run phase 4 — reduce captured events into the three per-axis curves."""
    coverage = coverage_curve(
        loop_result.node_enriched_events,
        seed_node_ids=loop_result.seed_node_ids,
        periods=loop_result.periods_completed,
    )
    lift = retrieval_lift_curve(
        loop_result.pack_quality_events,
        baseline=loop_result.baseline_score,
        periods=loop_result.periods_completed,
    )
    survival = variant_survival_rate(
        loop_result.evolver_events,
        initial_pool_size=loop_result.initial_variant_pool,
    )
    return coverage, lift, survival


def _summarise(
    coverage: CoverageCurve,
    lift: LiftCurve,
    survival: VariantSurvival,
) -> tuple[dict[str, float | str], list[Finding], str]:
    """Reduce the three curves into ``(metrics, findings, decision)``."""
    final_lift = lift.lift[-1] if lift.lift else 0.0
    final_survival = survival.survival_rate[-1] if survival.survival_rate else 0.0
    metrics: dict[str, float | str] = {
        "coverage_final": coverage.final_coverage,
        "baseline_score": lift.baseline,
        "final_score": lift.per_period_score[-1] if lift.per_period_score else 0.0,
        "final_lift": final_lift,
        "survival_final": final_survival,
        "variants_culled": float(
            sum(survival.per_period_culled) if survival.per_period_culled else 0
        ),
    }

    findings = [
        Finding(
            severity="info",
            message=(
                f"P coverage {coverage.final_coverage:.2f} over "
                f"{coverage.seed_node_count} seed nodes; "
                f"Q lift {final_lift:+.3f} vs baseline {lift.baseline:.3f}; "
                f"R survival {final_survival:.2f} "
                f"({int(metrics['variants_culled'])} culled)."
            ),
        ),
        Finding(
            severity="info",
            message=(
                "Axis R uses the reference evolver (scenario-local pool; "
                "pruning driven by measured pack scores). It validates the "
                "measurement + score->prune mechanics, not a production F5 "
                "evolver — do not cite R as F5 evidence."
            ),
        ),
    ]
    if coverage.final_coverage < 1.0:
        findings.append(
            Finding(
                severity="warning",
                message=(
                    f"coverage ended at {coverage.final_coverage:.2f} < 1.0 — "
                    "the reference curator should reach every seed node."
                ),
            )
        )
    if final_lift <= 0.0:
        findings.append(
            Finding(
                severity="warning",
                message=(
                    f"final retrieval lift {final_lift:+.3f} is not positive — "
                    "consolidation did not improve panel pack quality."
                ),
            )
        )

    decision = (
        f"P climbed to {coverage.final_coverage:.2f}; Q ended {final_lift:+.3f} "
        f"over baseline; R fell to {final_survival:.2f} and plateaued. "
        "Reference-driver build: P/Q measured on real subsystems "
        "(governed mutations, PackBuilder, evaluate_pack, EventLog); "
        "R exercises the measurement path only."
    )
    return metrics, findings, decision


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _validate_run_kwargs(
    *,
    periods: int,
    periods_per_evolution: int,
    nodes_per_period: int,
    docs_per_node: int = DEFAULT_DOCS_PER_NODE,
    initial_variant_pool: int = DEFAULT_INITIAL_VARIANT_POOL,
) -> None:
    """Validate kwargs before any work. Raises :class:`ValueError` on bad input."""
    if periods <= 0:
        msg = f"periods must be positive, got {periods}"
        raise ValueError(msg)
    if nodes_per_period <= 0:
        msg = f"nodes_per_period must be positive, got {nodes_per_period}"
        raise ValueError(msg)
    if not 0 < periods_per_evolution <= periods:
        msg = (
            "periods_per_evolution must be in [1, periods], got "
            f"{periods_per_evolution} (periods={periods})"
        )
        raise ValueError(msg)
    if docs_per_node <= PANEL_MAX_ITEMS:
        msg = (
            f"docs_per_node must exceed the panel budget ({PANEL_MAX_ITEMS}) "
            f"so consolidation has measurable headroom, got {docs_per_node}"
        )
        raise ValueError(msg)
    if initial_variant_pool < MIN_VARIANTS_FOR_DIFFERENTIAL:
        msg = (
            f"initial_variant_pool must be >= {MIN_VARIANTS_FOR_DIFFERENTIAL} "
            f"(the evolver needs a differential to act on), got "
            f"{initial_variant_pool}"
        )
        raise ValueError(msg)


def run(
    registry: StoreRegistry,
    *,
    seed: int = 0,
    periods: int = DEFAULT_PERIODS,
    nodes_per_period: int = DEFAULT_NODES_PER_PERIOD,
    docs_per_node: int = DEFAULT_DOCS_PER_NODE,
    periods_per_evolution: int = DEFAULT_PERIODS_PER_EVOLUTION,
    initial_variant_pool: int = DEFAULT_INITIAL_VARIANT_POOL,
    traces_per_domain: int = DEFAULT_TRACES_PER_DOMAIN,
    entities_per_trace: int = DEFAULT_ENTITIES_PER_TRACE,
) -> ScenarioReport:
    """Execute the skill-loop convergence scenario.

    Skip semantics (no work done, no registry touched):

    - :data:`OPT_IN_ENV_VAR` unset / empty → ``status="skip"`` with an
      info finding pointing at the env var. This is the default CI
      path; the scenario is discoverable but inert.

    Run semantics: the four-phase flow (seed → loop → evolve →
    measure) with the reference drivers documented in the module
    docstring. ``status`` is ``"regress"`` when the final retrieval
    lift is non-positive or coverage falls short of 1.0 — on this
    deterministic corpus both indicate a real regression in the
    measured subsystems, not noise.
    """
    if not _opt_in_enabled():
        return _skip_report(
            message=(
                f"set {OPT_IN_ENV_VAR}=1 to run skill_loop_convergence "
                "(reference-driver build; see scenario.py module docstring)."
            ),
            decision=(
                "Scenario skipped — F-phase opt-in env var "
                f"({OPT_IN_ENV_VAR}) not set. CI does not set it; the "
                "scenario is discoverable but inert until operators "
                "explicitly enable it."
            ),
        )

    _validate_run_kwargs(
        periods=periods,
        periods_per_evolution=periods_per_evolution,
        nodes_per_period=nodes_per_period,
        docs_per_node=docs_per_node,
        initial_variant_pool=initial_variant_pool,
    )

    run_id = f"skill_loop_convergence_{seed:04d}"
    logger.info(
        "skill_loop_convergence.run_start",
        run_id=run_id,
        periods=periods,
        seed=seed,
    )

    seed_node_ids, doc_count, baseline_manifest = _seed(
        registry,
        seed=seed,
        nodes_per_period=nodes_per_period,
        periods=periods,
        docs_per_node=docs_per_node,
        traces_per_domain=traces_per_domain,
        entities_per_trace=entities_per_trace,
    )

    loop_result = _loop(
        registry,
        seed_node_ids,
        periods=periods,
        periods_per_evolution=periods_per_evolution,
        initial_variant_pool=initial_variant_pool,
        docs_per_node=docs_per_node,
        baseline_manifest=baseline_manifest,
        run_id=run_id,
    )
    loop_result.nodes_seeded = len(seed_node_ids)
    loop_result.documents_seeded = doc_count

    coverage, lift, survival = _measure(loop_result)
    metrics, findings, decision = _summarise(coverage, lift, survival)

    metrics.setdefault("periods", float(periods))
    metrics.setdefault("nodes_seeded", float(loop_result.nodes_seeded))
    metrics.setdefault("documents_seeded", float(loop_result.documents_seeded))
    metrics.setdefault(
        "stability_delta",
        loop_result.stability_final - loop_result.stability_baseline,
    )

    final_lift = float(metrics.get("final_lift", 0.0))
    coverage_final = float(metrics.get("coverage_final", 0.0))
    status = "pass" if final_lift > 0.0 and coverage_final >= 1.0 else "regress"

    return ScenarioReport(
        name=SCENARIO_NAME,
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
        convergence_stats={
            "baseline_manifest": baseline_manifest,
            "coverage": coverage.model_dump(),
            "lift": lift.model_dump(),
            "survival": survival.model_dump(),
        },
    )


# Re-exports so callers can write
# ``from eval.scenarios.skill_loop_convergence.scenario import ...``
# rather than reaching into the sibling modules. Mirrors the pattern
# the other convergence scenarios use for their per-axis helpers.
__all__ = [
    "DEFAULT_DOCS_PER_NODE",
    "DEFAULT_ENTITIES_PER_TRACE",
    "DEFAULT_INITIAL_VARIANT_POOL",
    "DEFAULT_NODES_PER_PERIOD",
    "DEFAULT_PERIODS",
    "DEFAULT_PERIODS_PER_EVOLUTION",
    "DEFAULT_TRACES_PER_DOMAIN",
    "OPT_IN_ENV_VAR",
    "SCENARIO_NAME",
    "CoverageCurve",
    "LiftCurve",
    "VariantSurvival",
    "coverage_curve",
    "retrieval_lift_curve",
    "run",
    "seed_baseline_corpus",
    "seed_documents_for_nodes",
    "seed_under_populated_nodes",
    "variant_survival_rate",
]
