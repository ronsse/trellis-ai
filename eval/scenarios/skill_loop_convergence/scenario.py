"""Skill-loop convergence scenario — F-phase inner-loop measurement (skeleton).

F6 (Wave 1, Unit D of Phase F). Measures whether the inner agent loop
(graph-skill harness + curator skill + feedback + score-based
evolver) converges over time on a synthetic corpus. The four
conceptual phases of a run are:

1. **Seed.** Populate the registry with under-populated nodes, their
   source documents, and a baseline trace + document corpus the
   retrieval-lift metric measures against. See :mod:`.seed`.
2. **Loop.** For each period, dispatch the curator skill against
   the under-populated node set. Capture ``NODE_ENRICHED`` and
   ``CURATION_FEEDBACK_RECORDED`` events.
3. **Evolve.** Every ``periods_per_evolution`` periods, the F5
   score-based evolver inspects the variant pool, scores each
   variant against the captured feedback, and prunes / promotes.
4. **Measure.** Reduce the captured events into the three per-axis
   curves: coverage (axis P, this scenario), retrieval lift (axis
   Q), variant survival (axis R). See :mod:`.metrics`.

Skeleton-only contract:

- Every helper raises :class:`NotImplementedError` with a docstring
  naming the F-phase that fills it in. ``run()`` itself returns a
  ``status="skip"`` report until the F-phase machinery lands so the
  runner can discover the scenario by name without erroring.
- Discoverability gate matches the ``program_convergence_real_llm``
  pattern: the scenario IS in :func:`eval.runner.list_scenarios`,
  but ``run()`` skips when the F-phase opt-in env var is not set.
  CI does not set the env var, so this scenario stays inert by
  default.

The opt-in env var (:data:`OPT_IN_ENV_VAR`,
``TRELLIS_EVAL_SKILL_LOOP``) flips to non-empty when an operator
wants to run the scenario for real. F2 / F5 swarms will extend the
gating to also check for the presence of their machinery (event
types, evolver entry points).

References (paths only — do not assume content; sibling units in
Wave 1 are authoring these in parallel):

- ``docs/design/adr-graph-skill-harness.md`` (Unit A)
- ``docs/design/adr-inner-curation-loop.md`` (Unit B)
- ``docs/research/workflow-engine-disposition.md`` (Unit C)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import structlog

from eval.runner import Finding, ScenarioReport
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
    seed_baseline_corpus,
    seed_documents_for_nodes,
    seed_under_populated_nodes,
)

logger = structlog.get_logger(__name__)


SCENARIO_NAME = "skill_loop_convergence"

#: Opt-in env var. Setting to any non-empty value flips ``run()`` from
#: the always-skip skeleton state into the live path. Until F1-F5 have
#: landed, the live path also raises :class:`NotImplementedError` —
#: the env var is the kill switch that prevents CI from accidentally
#: invoking the unimplemented code.
OPT_IN_ENV_VAR: str = "TRELLIS_EVAL_SKILL_LOOP"

# Default knobs. Set conservatively for an under-an-hour run on dev
# hardware once the F-phases have landed; F1-F5 may revise.
DEFAULT_PERIODS: int = 20
DEFAULT_NODES_PER_PERIOD: int = 5
DEFAULT_DOCS_PER_NODE: int = 3
DEFAULT_PERIODS_PER_EVOLUTION: int = 5
DEFAULT_INITIAL_VARIANT_POOL: int = 4
DEFAULT_TRACES_PER_DOMAIN: int = 6
DEFAULT_ENTITIES_PER_TRACE: int = 3


# ---------------------------------------------------------------------------
# Result aggregate
# ---------------------------------------------------------------------------


@dataclass
class _LoopResult:
    """Output of the inner loop — passed to :func:`_summarise`.

    Held as a private dataclass (not :class:`~trellis.core.base.TrellisModel`)
    because it's an internal handoff between phases of ``run()``, not a
    schema artifact. F-phase swarms may freely add fields without breaking
    a public contract.
    """

    periods_completed: int = 0
    nodes_seeded: int = 0
    documents_seeded: int = 0
    node_enriched_events: list[dict[str, Any]] = field(default_factory=list)
    feedback_events: list[dict[str, Any]] = field(default_factory=list)
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
# Loop phases — all stubs, F-phase swarms fill in
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
    F6 fills this in (this scenario — orchestrates :mod:`.seed`
    helpers). Stub: raises :class:`NotImplementedError`.
    """
    msg = "F6 (this scenario) wires up the seed phase"
    raise NotImplementedError(msg)


def _loop(
    registry: StoreRegistry,
    seed_node_ids: list[str],
    *,
    periods: int,
    periods_per_evolution: int,
    initial_variant_pool: int,
    run_id: str,
) -> _LoopResult:
    """Run phases 2 + 3 — per-period curate + score-based evolve.

    Dispatches the curator skill once per period against the seed
    nodes, captures ``NODE_ENRICHED`` + ``CURATION_FEEDBACK_RECORDED``
    + ``PACK_QUALITY_SCORED`` events, and every
    ``periods_per_evolution`` periods invokes the F5 evolver to
    update the variant pool.

    F2 + F3 + F5 fill this in jointly:

    - F2 — curator skill dispatch and ``NODE_ENRICHED`` emit.
    - F3 — feedback path (``CURATION_FEEDBACK_RECORDED``,
      ``PACK_QUALITY_SCORED``).
    - F5 — score-based evolver invocation + variant-pool events.

    Stub: raises :class:`NotImplementedError`.
    """
    msg = "F2 (curator) + F3 (feedback) + F5 (evolver) fill this in"
    raise NotImplementedError(msg)


def _measure(
    loop_result: _LoopResult,
) -> tuple[CoverageCurve, LiftCurve, VariantSurvival]:
    """Run phase 4 — reduce captured events into the three per-axis curves.

    Delegates to :func:`.metrics.coverage_curve`,
    :func:`.metrics.retrieval_lift_curve`,
    :func:`.metrics.variant_survival_rate`. F6 fills this in (this
    scenario — assembles the report). Stub: raises
    :class:`NotImplementedError`.
    """
    msg = "F6 (this scenario) wires up the measure phase"
    raise NotImplementedError(msg)


def _summarise(
    coverage: CoverageCurve,
    lift: LiftCurve,
    survival: VariantSurvival,
) -> tuple[dict[str, float | str], list[Finding], str]:
    """Reduce the three curves into ``(metrics, findings, decision)``.

    F6 fills this in (this scenario — assembles the report payload
    matching :class:`eval.runner.ScenarioReport`). Stub: raises
    :class:`NotImplementedError`.
    """
    msg = "F6 (this scenario) wires up the summary"
    raise NotImplementedError(msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _validate_run_kwargs(
    *,
    periods: int,
    periods_per_evolution: int,
    nodes_per_period: int,
) -> None:
    """Validate kwargs before any work. Raises :class:`ValueError` on bad input.

    F6 fills this in (this scenario — the same loud-on-bad-kwargs
    discipline the other convergence scenarios use). Stub: raises
    :class:`NotImplementedError`.
    """
    msg = "F6 (this scenario) wires up kwarg validation"
    raise NotImplementedError(msg)


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

    Run semantics (skeleton-only until F1-F5 land):

    - :data:`OPT_IN_ENV_VAR` set → executes the four-phase flow
      (seed → loop → evolve → measure). Until F1-F5 have populated
      the phase helpers, this path raises
      :class:`NotImplementedError` from whichever helper is hit
      first. The kwarg validation happens before any helper is
      invoked so bad inputs fail loudly even when the helpers
      themselves are stubs.

    F-phase fill-in map:

    ============================  ===========================
    Helper                        Phase
    ============================  ===========================
    :func:`_seed`                 F6 (this scenario)
    :func:`_loop`                 F2 + F3 + F5
    :func:`_measure`              F6 (this scenario)
    :func:`_summarise`            F6 (this scenario)
    :func:`.metrics.coverage_curve`           F1 + F2
    :func:`.metrics.retrieval_lift_curve`     F3
    :func:`.metrics.variant_survival_rate`    F5
    :func:`.seed.seed_under_populated_nodes`  F1
    :func:`.seed.seed_documents_for_nodes`    F2
    :func:`.seed.seed_baseline_corpus`        F6
    ============================  ===========================
    """
    if not _opt_in_enabled():
        return _skip_report(
            message=(
                f"set {OPT_IN_ENV_VAR}=1 to run skill_loop_convergence. "
                "Skeleton-only until F1-F5 land; see scenario.py "
                "module docstring for the F-phase fill-in map."
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
        run_id=run_id,
    )
    loop_result.nodes_seeded = len(seed_node_ids)
    loop_result.documents_seeded = doc_count

    coverage, lift, survival = _measure(loop_result)
    metrics, findings, decision = _summarise(coverage, lift, survival)

    metrics.setdefault("periods", float(periods))
    metrics.setdefault("nodes_seeded", float(loop_result.nodes_seeded))
    metrics.setdefault("documents_seeded", float(loop_result.documents_seeded))

    return ScenarioReport(
        name=SCENARIO_NAME,
        status="pass",
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
