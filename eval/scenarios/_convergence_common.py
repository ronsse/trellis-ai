"""Shared convergence-loop helpers for the Tier-2 scenarios.

Three convergence scenarios share the same dual-loop math, periodic
loop firing schedule, and per-round bookkeeping shapes:

- ``agent_loop_convergence`` — synthetic corpus, KeywordSearch only.
- ``dbt_corpus_convergence`` — Jaffle Shop dbt manifest, Keyword +
  Semantic + SeededGraph.
- ``github_corpus_convergence`` — trellis-ai PR snapshot, same
  strategies as dbt.
- ``agent_loop_convergence_real_llm`` — Phase A: synthetic corpus +
  real LLM/embedder; reuses agent_loop's helpers.

This module collects the round-shape-agnostic helpers — anything that
operates on the *outcome* of a round (``weighted_score`` /
``items_served`` / ``items_referenced``) rather than its
scenario-specific fields. Per-scenario ``_RoundResult`` dataclasses stay
scenario-local so they can carry their own discriminators (``domain``
vs ``skill`` + ``difficulty``).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from eval.runner import Finding
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    run_advisory_fitness_loop,
    run_effectiveness_feedback,
)
from trellis.schemas.pack import Pack
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry

# ---------------------------------------------------------------------------
# Constants — defaults shared across all convergence scenarios
# ---------------------------------------------------------------------------

DEFAULT_ROUNDS = 30
DEFAULT_FEEDBACK_BATCH_SIZE = 5
DEFAULT_PACK_MAX_ITEMS = 8
DEFAULT_PACK_MAX_TOKENS = 1_500
DEFAULT_SUCCESS_COVERAGE_THRESHOLD = 0.6
DEFAULT_PROFILE_NAME = "domain_context"
CONVERGENCE_DELTA_REGRESS_THRESHOLD = -0.05
ROUND_WINDOW_FRACTION = 4  # compare first vs last quarter of rounds
DEFAULT_ADVISORY_MIN_SAMPLE_SIZE = 5
DEFAULT_FITNESS_MIN_PRESENTATIONS = 2  # synthetic corpora are small;
# production gate is 30+


# ---------------------------------------------------------------------------
# Round outcome — shape :func:`_convergence_stats` needs from a round
# ---------------------------------------------------------------------------


class _RoundOutcome(Protocol):
    """Minimal shape :func:`_convergence_stats` needs from a per-round record.

    Each scenario's ``_RoundResult`` provides these attributes plus its
    own discriminator fields (``domain`` / ``skill`` etc).
    """

    weighted_score: float
    items_served: int
    items_referenced: int
    coverage_fraction: float
    success: bool


# ---------------------------------------------------------------------------
# Loop stats — accumulator surfaced by :func:`_run_periodic_loops`
# ---------------------------------------------------------------------------


@dataclass
class _LoopStats:
    """Cumulative counts surfaced from the periodic loops."""

    effectiveness_runs: int = 0
    noise_items_tagged_total: int = 0
    advisory_runs: int = 0
    advisories_generated_total: int = 0
    advisories_suppressed_total: int = 0
    advisories_restored_total: int = 0
    advisories_boosted_total: int = 0
    suppressed_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Convergence stats — first-vs-last-quarter deltas
# ---------------------------------------------------------------------------


@dataclass
class _ConvergenceStats:
    weighted_first_quarter_mean: float
    weighted_last_quarter_mean: float
    weighted_delta: float
    useful_first_quarter_mean: float
    useful_last_quarter_mean: float
    useful_delta: float


def _quarter_means(values: list[float]) -> tuple[float, float]:
    """Return ``(first_quarter_mean, last_quarter_mean)``.

    Defensive against tiny round counts: when fewer than four samples
    are available, both quarters fall back to the full-sample mean,
    so the resulting delta is zero rather than misleadingly large.
    """
    if not values:
        return 0.0, 0.0
    if len(values) < ROUND_WINDOW_FRACTION:
        full = statistics.fmean(values)
        return full, full
    window = max(1, len(values) // ROUND_WINDOW_FRACTION)
    return (
        statistics.fmean(values[:window]),
        statistics.fmean(values[-window:]),
    )


def _convergence_stats(rounds: Sequence[_RoundOutcome]) -> _ConvergenceStats:
    weighted = [r.weighted_score for r in rounds]
    useful = [
        (r.items_referenced / r.items_served) if r.items_served else 0.0
        for r in rounds
    ]
    w_first, w_last = _quarter_means(weighted)
    u_first, u_last = _quarter_means(useful)
    return _ConvergenceStats(
        weighted_first_quarter_mean=w_first,
        weighted_last_quarter_mean=w_last,
        weighted_delta=w_last - w_first,
        useful_first_quarter_mean=u_first,
        useful_last_quarter_mean=u_last,
        useful_delta=u_last - u_first,
    )


def _convergence_metrics(c: _ConvergenceStats) -> dict[str, float]:
    return {
        "convergence.weighted_first_quarter_mean": round(
            c.weighted_first_quarter_mean, 4
        ),
        "convergence.weighted_last_quarter_mean": round(
            c.weighted_last_quarter_mean, 4
        ),
        "convergence.weighted_delta": round(c.weighted_delta, 4),
        "convergence.useful_first_quarter_mean": round(
            c.useful_first_quarter_mean, 4
        ),
        "convergence.useful_last_quarter_mean": round(
            c.useful_last_quarter_mean, 4
        ),
        "convergence.useful_delta": round(c.useful_delta, 4),
    }


def _loop_metrics(s: _LoopStats) -> dict[str, float]:
    return {
        "loops.effectiveness_runs": float(s.effectiveness_runs),
        "loops.noise_items_tagged_total": float(s.noise_items_tagged_total),
        "loops.advisory_runs": float(s.advisory_runs),
        "loops.advisories_generated_total": float(s.advisories_generated_total),
        "loops.advisories_suppressed_total": float(s.advisories_suppressed_total),
        "loops.advisories_restored_total": float(s.advisories_restored_total),
        "loops.advisories_boosted_total": float(s.advisories_boosted_total),
    }


# ---------------------------------------------------------------------------
# Per-round metrics — scenario-agnostic core. Per-domain / per-skill
# breakdowns stay in the scenario module since the discriminator differs.
# ---------------------------------------------------------------------------


def _base_round_metrics(rounds: Sequence[_RoundOutcome]) -> dict[str, float]:
    if not rounds:
        return {}
    weighted_scores = [r.weighted_score for r in rounds]
    coverage = [r.coverage_fraction for r in rounds]
    successes = sum(1 for r in rounds if r.success)
    served = sum(r.items_served for r in rounds)
    referenced = sum(r.items_referenced for r in rounds)
    return {
        "round_weighted_score_mean": round(statistics.fmean(weighted_scores), 4),
        "round_weighted_score_min": round(min(weighted_scores), 4),
        "round_weighted_score_max": round(max(weighted_scores), 4),
        "round_coverage_mean": round(statistics.fmean(coverage), 4),
        "round_success_rate": round(successes / len(rounds), 4),
        "round_total_items_served": float(served),
        "round_total_items_referenced": float(referenced),
        "round_useful_fraction_overall": (
            round(referenced / served, 4) if served else 0.0
        ),
    }


# ---------------------------------------------------------------------------
# Periodic loops — same shape every scenario
# ---------------------------------------------------------------------------


def _run_periodic_loops(
    *,
    registry: StoreRegistry,
    advisory_store: AdvisoryStore,
    stats: _LoopStats,
    generate_advisories: bool,
    advisory_min_sample_size: int = DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    fitness_min_presentations: int = DEFAULT_FITNESS_MIN_PRESENTATIONS,
) -> None:
    """Run the noise-tagging + advisory loops once.

    ``generate_advisories`` controls whether ``AdvisoryGenerator.generate``
    fires this pass. Scenarios should pass ``True`` only on the *first*
    periodic pass: ``AdvisoryGenerator`` mints fresh ULIDs every call
    without deduplicating against the existing store, so regenerating
    each batch would saddle every subsequent fitness pass with a brand-new
    cohort of zero-presentation advisories — convergence becomes
    invisible to the suppression gate. Generating once after the first
    feedback batch lets advisory IDs stay stable so presentations
    accumulate across the remaining rounds.
    """
    knowledge = registry.knowledge
    operational = registry.operational

    effectiveness = run_effectiveness_feedback(
        operational.event_log,
        knowledge.document_store,
        # min_appearances=2 (the default) is fine for synthetic corpora.
    )
    stats.effectiveness_runs += 1
    stats.noise_items_tagged_total += len(effectiveness.noise_candidates)

    if generate_advisories:
        advisory_report = AdvisoryGenerator(
            operational.event_log,
            advisory_store,
            min_sample_size=advisory_min_sample_size,
        ).generate()
        stats.advisories_generated_total += advisory_report.advisories_generated
    stats.advisory_runs += 1

    fitness = run_advisory_fitness_loop(
        operational.event_log,
        advisory_store,
        min_presentations=fitness_min_presentations,
    )
    stats.advisories_boosted_total += len(fitness.advisories_boosted)
    stats.advisories_suppressed_total += len(fitness.advisories_suppressed)
    stats.advisories_restored_total += len(fitness.advisories_restored)
    stats.suppressed_ids.extend(fitness.advisories_suppressed)


# ---------------------------------------------------------------------------
# Round feedback recording — same shape across scenarios; the caller
# extracts ``intent`` / ``intent_family`` from its scenario-specific
# query type.
# ---------------------------------------------------------------------------


def _record_round_feedback(
    *,
    feedback_log_dir: Path,
    registry: StoreRegistry,
    pack: Pack,
    intent: str,
    intent_family: str,
    referenced: list[str],
    success: bool,
    round_index: int,
    run_id: str,
    agent_id: str,
) -> None:
    feedback = PackFeedback(
        run_id=run_id,
        phase=f"round_{round_index:03d}",
        intent=intent,
        outcome="success" if success else "failure",
        items_served=[item.item_id for item in pack.items],
        items_referenced=referenced,
        intent_family=intent_family,
        agent_id=agent_id,
    )
    record_feedback(
        feedback,
        log_dir=feedback_log_dir,
        event_log=registry.operational.event_log,
        pack_id=pack.pack_id,
    )


# ---------------------------------------------------------------------------
# Findings — info-level summaries used by every scenario at end-of-run
# ---------------------------------------------------------------------------


def _convergence_summary_finding(c: _ConvergenceStats) -> Finding:
    return Finding(
        severity="info",
        message=(
            f"weighted score: {c.weighted_first_quarter_mean:.3f} "
            f"→ {c.weighted_last_quarter_mean:.3f} "
            f"(Δ {c.weighted_delta:+.3f})"
        ),
        detail={
            "useful_fraction_first_quarter": round(c.useful_first_quarter_mean, 4),
            "useful_fraction_last_quarter": round(c.useful_last_quarter_mean, 4),
            "useful_delta": round(c.useful_delta, 4),
        },
    )


def _loops_summary_finding(stats: _LoopStats) -> Finding:
    return Finding(
        severity="info",
        message=(
            f"loops fired: {stats.effectiveness_runs} effectiveness, "
            f"{stats.advisory_runs} advisory; "
            f"noise tags applied: {stats.noise_items_tagged_total}; "
            f"advisories — generated {stats.advisories_generated_total}, "
            f"suppressed {stats.advisories_suppressed_total}, "
            f"restored {stats.advisories_restored_total}, "
            f"boosted {stats.advisories_boosted_total}"
        ),
        detail={"suppressed_ids": stats.suppressed_ids[:20]},
    )


# ---------------------------------------------------------------------------
# Validation — basic kwarg guards every scenario shares
# ---------------------------------------------------------------------------


def _validate_basic_kwargs(*, rounds: int, feedback_batch_size: int) -> None:
    if rounds <= 0:
        msg = "rounds must be positive"
        raise ValueError(msg)
    if feedback_batch_size <= 0:
        msg = "feedback_batch_size must be positive"
        raise ValueError(msg)
