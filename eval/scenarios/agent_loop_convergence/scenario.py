"""Agent-loop convergence scenario.

Mechanism summary (full prose in the README):

1. Build the same domain-templated trace corpus scenario 5.2 uses, so
   ground-truth follow-up queries already exist.
2. Populate the document store with:
     * one ``entity_summary`` doc per real entity (matches scenario 5.2),
     * a small set of *distractor* docs whose content overlaps with
       query keywords but contains no required-coverage entities. The
       distractors are what the effectiveness loop has to learn to
       suppress; without them there is nothing for convergence to fix.
3. Run N rounds of:
     * pick a query (round-robin across domains),
     * build a pack via ``PackBuilder`` with ``tag_filters={}`` so the
       default ``signal_quality`` filter is engaged (noise items stay
       out once tagged),
     * grade success deterministically: a round is a "success" when
       ``coverage_fraction >= success_coverage_threshold``,
     * synthesize ``items_referenced`` from the pack items whose
       ``item_id`` is in the query's required coverage,
     * call :func:`record_feedback` with the registry's EventLog so the
       advisory + effectiveness analysers see the signal,
     * score the pack with :func:`evaluate_pack` for the per-round
       quality trace.
4. Every ``feedback_batch_size`` rounds, run the two convergence loops:
     * :func:`run_effectiveness_feedback` — flags noise items and tags
       them so the next round's pack excludes them,
     * :class:`AdvisoryGenerator` then :func:`run_advisory_fitness_loop`
       — produce + grade advisories, suppress under-performing ones.
5. Aggregate per-round metrics and compute a simple convergence delta
   (mean weighted score on the last quarter minus the first quarter).
   Positive delta ⇒ the loop converged on better packs.

Single backend per run — multi-backend equivalence is scenario 5.1's
job. The runner-supplied registry is honoured.
"""

from __future__ import annotations

import statistics
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from eval.generators.trace_generator import (
    DOMAIN_TEMPLATES,
    EvalQuery,
    GeneratedCorpus,
    GeneratedTrace,
    generate_corpus,
)
from eval.runner import Finding, ScenarioReport, ScenarioStatus
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.retrieve.effectiveness import (
    run_advisory_fitness_loop,
    run_effectiveness_feedback,
)
from trellis.retrieve.evaluate import (
    BUILTIN_PROFILES,
    EvaluationScenario,
    evaluate_pack,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas.pack import Pack, PackBudget
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

DEFAULT_ROUNDS = 30
DEFAULT_FEEDBACK_BATCH_SIZE = 5
DEFAULT_PACK_MAX_ITEMS = 8
DEFAULT_PACK_MAX_TOKENS = 1_500
DEFAULT_TRACES_PER_DOMAIN = 6
DEFAULT_ENTITIES_PER_TRACE = 3
DEFAULT_SUCCESS_COVERAGE_THRESHOLD = 0.6
DEFAULT_PROFILE_NAME = "domain_context"
CONVERGENCE_DELTA_REGRESS_THRESHOLD = -0.05
ROUND_WINDOW_FRACTION = 4  # compare first vs last quarter of rounds

# Production default for AdvisoryGenerator.min_sample_size is 5. The
# scenario corpus is small (~5 packs by the time the first periodic
# pass fires), so per-entity sample sizes never reach 5 — only the
# global "keyword strategy" advisory ever forms. Lower this kwarg
# when running with regime_shift_round so per-entity advisories form
# at batch 1 and have something to be suppressed when the regime
# shifts. Production gates remain at their defaults.
DEFAULT_ADVISORY_MIN_SAMPLE_SIZE = 5

# Regime-shift mode (opt-in). When ``regime_shift_round`` is set, the
# agent grades post-shift rounds against a modified required_coverage
# where the first N entities are replaced with unreachable
# placeholders. Coverage drops, packs start failing, and any advisory
# that formed pre-shift around the dropped entities sees its lift
# collapse → fitness loop suppresses it. Restoration would require an
# evidence rebound that the current scenario does not stage.
DEFAULT_REGIME_SHIFT_REPLACEMENT_COUNT = 2
_REGIME_SHIFT_PLACEHOLDER_PREFIX = "_unreachable_post_shift_"

# Per-domain distractor docs. Each has at least one keyword from the
# domain's `query_intent` so KeywordSearch picks it up, but contains
# none of the `required_coverage` entity ids — so a feedback-driven
# loop should learn to demote them.
_DISTRACTOR_DOCS: dict[str, list[tuple[str, str]]] = {
    "software_engineering": [
        (
            "doc:distractor:session_timeout",
            (
                "session timeout policy retro. Mentions session lifecycle in "
                "general; no token validation guidance."
            ),
        ),
        (
            "doc:distractor:validation_history",
            (
                "Historical notes on validation queue tooling; not specific "
                "to session token flow."
            ),
        ),
    ],
    "data_pipeline": [
        (
            "doc:distractor:fact_table_archive",
            (
                "fact_table archive cleanup runbook. References fact_table "
                "by name but covers archival, not backfills."
            ),
        ),
        (
            "doc:distractor:dependencies_glossary",
            (
                "Generic upstream dependencies glossary. Mentions "
                "dependencies in passing; no concrete backfill steps."
            ),
        ),
    ],
    "customer_support": [
        (
            "doc:distractor:refund_marketing",
            (
                "Marketing brief for refund campaigns. Mentions refund and "
                "billing tone but not policy."
            ),
        ),
        (
            "doc:distractor:billing_disputes_summary",
            (
                "Quarterly summary of billing disputes volume. Numbers, "
                "no policy guidance."
            ),
        ),
    ],
}


# ---------------------------------------------------------------------------
# Per-round bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _RoundResult:
    round_index: int
    domain: str
    pack_id: str
    items_served: int
    items_referenced: int
    coverage_fraction: float
    weighted_score: float
    success: bool


@dataclass
class _ConvergenceStats:
    weighted_first_quarter_mean: float
    weighted_last_quarter_mean: float
    weighted_delta: float
    useful_first_quarter_mean: float
    useful_last_quarter_mean: float
    useful_delta: float


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
# Setup helpers
# ---------------------------------------------------------------------------


def _ingest_traces(registry: StoreRegistry, corpus: GeneratedCorpus) -> int:
    trace_store = registry.operational.trace_store
    for gt in corpus.traces:
        trace_store.append(gt.trace)
    return len(corpus.traces)


def _populate_entity_documents(registry: StoreRegistry, corpus: GeneratedCorpus) -> int:
    """One document per real entity. Same shape as scenario 5.2.

    Doc retrievability across the domain's query depends on the query
    text mentioning each required entity by name — that's a corpus
    property of `DOMAIN_TEMPLATES.query_intent` in
    `eval/generators/trace_generator.py`. We deliberately do *not*
    inject the domain query into every entity's doc here: that would
    flatten the corpus so much that AdvisoryGenerator can't compute
    per-entity lift (success_rate_with vs success_rate_without
    becomes 0 when every entity ends up in every pack). The corpus
    keeps per-doc differentiation; the query phrasing carries the
    burden of naming required entities.
    """
    knowledge = registry.knowledge
    graph_store = knowledge.graph_store
    document_store = knowledge.document_store

    by_entity: dict[str, list[GeneratedTrace]] = {}
    for gt in corpus.traces:
        for entity in gt.entities:
            by_entity.setdefault(entity, []).append(gt)

    for entity, traces in by_entity.items():
        domain = traces[0].domain
        graph_store.upsert_node(
            node_id=entity,
            node_type="entity",
            properties={"name": entity, "domain": domain},
        )
        intents = sorted({t.trace.intent for t in traces})
        content = (
            f"{entity} ({domain}). Referenced by {len(traces)} traces. "
            f"Sample intents: {'; '.join(intents[:5])}."
        )
        document_store.put(
            doc_id=f"doc:{entity}",
            content=content,
            metadata={
                "entity_id": entity,
                "domain": domain,
                "content_type": "entity_summary",
                "domains": [domain],
                # Seed every doc with ``signal_quality="standard"`` so it
                # passes PackBuilder's default ``signal_quality`` filter.
                # The effectiveness loop later flips under-performing
                # docs to ``"noise"`` — which is the convergence signal
                # this scenario measures.
                "content_tags": {"signal_quality": "standard"},
            },
        )
    return len(by_entity)


def _populate_distractor_documents(registry: StoreRegistry) -> int:
    """Plant per-domain distractor docs the feedback loop should suppress."""
    document_store = registry.knowledge.document_store
    planted = 0
    for domain, docs in _DISTRACTOR_DOCS.items():
        for doc_id, content in docs:
            document_store.put(
                doc_id=doc_id,
                content=content,
                metadata={
                    "domain": domain,
                    "content_type": "entity_summary",
                    "domains": [domain],
                    # Same default-pass tag as real entities — the
                    # effectiveness loop is what should learn to flip
                    # these to ``"noise"`` over time.
                    "content_tags": {"signal_quality": "standard"},
                },
            )
            planted += 1
    return planted


# ---------------------------------------------------------------------------
# Per-round work
# ---------------------------------------------------------------------------


def _round_query(corpus: GeneratedCorpus, round_index: int) -> EvalQuery:
    return corpus.queries[round_index % len(corpus.queries)]


def _build_pack(builder: PackBuilder, query: EvalQuery) -> Pack:
    return builder.build(
        intent=query.intent,
        domain=query.domain,
        budget=PackBudget(
            max_items=DEFAULT_PACK_MAX_ITEMS,
            max_tokens=DEFAULT_PACK_MAX_TOKENS,
        ),
        # Empty dict triggers the PackBuilder default that excludes
        # ``signal_quality="noise"`` items — without this, noise tags
        # applied by the effectiveness loop would have no read-time
        # effect and convergence couldn't be measured.
        tag_filters={},
    )


def _effective_required_coverage(
    query: EvalQuery,
    *,
    round_index: int,
    regime_shift_round: int | None,
    regime_shift_replacement_count: int,
) -> list[str]:
    """Return the entity list the agent grades against this round.

    Pre-shift (or when ``regime_shift_round`` is ``None``): the query's
    canonical ``required_coverage``. Post-shift: the first
    ``regime_shift_replacement_count`` entries are swapped for
    unreachable placeholders so coverage drops mechanically — packs
    that previously succeeded now fail, and advisories formed around
    the dropped entities lose their pack-level lift.
    """
    required = list(query.required_coverage)
    if regime_shift_round is None or round_index < regime_shift_round:
        return required
    swap_count = min(regime_shift_replacement_count, len(required))
    for i in range(swap_count):
        required[i] = f"{_REGIME_SHIFT_PLACEHOLDER_PREFIX}{i}"
    return required


def _grade_round(
    pack: Pack,
    query: EvalQuery,
    *,
    coverage_threshold: float,
    round_index: int = 0,
    regime_shift_round: int | None = None,
    regime_shift_replacement_count: int = DEFAULT_REGIME_SHIFT_REPLACEMENT_COUNT,
) -> tuple[list[str], float, bool]:
    """Determine which served items map to ground-truth entities.

    Returns ``(items_referenced, coverage_fraction, success)``.

    ``items_referenced`` is the subset of pack item ids that match the
    *effective* required entity set for this round (see
    :func:`_effective_required_coverage`). Coverage is the fraction of
    required entities present in the pack.
    """
    required = _effective_required_coverage(
        query,
        round_index=round_index,
        regime_shift_round=regime_shift_round,
        regime_shift_replacement_count=regime_shift_replacement_count,
    )
    pack_doc_ids = {item.item_id for item in pack.items}
    required_doc_ids = {f"doc:{entity}" for entity in required}
    referenced = sorted(pack_doc_ids & required_doc_ids)
    coverage = 1.0 if not required else len(referenced) / len(required)
    return referenced, coverage, coverage >= coverage_threshold


def _score_pack(pack: Pack, query: EvalQuery) -> dict[str, float]:
    eval_scenario = EvaluationScenario(
        name=f"convergence_{query.domain}",
        intent=query.intent,
        domain=query.domain,
        required_coverage=query.required_coverage,
        expected_categories=["entity_summary"],
    )
    report = evaluate_pack(
        pack,
        eval_scenario,
        profile=BUILTIN_PROFILES.get(DEFAULT_PROFILE_NAME),
    )
    return {
        **report.dimensions,
        "weighted_score": report.weighted_score,
    }


def _record_round_feedback(
    *,
    feedback_log_dir: Path,
    registry: StoreRegistry,
    pack: Pack,
    query: EvalQuery,
    referenced: list[str],
    success: bool,
    round_index: int,
    run_id: str,
) -> None:
    feedback = PackFeedback(
        run_id=run_id,
        phase=f"round_{round_index:03d}",
        intent=query.intent,
        outcome="success" if success else "failure",
        items_served=[item.item_id for item in pack.items],
        items_referenced=referenced,
        intent_family=query.domain,
        agent_id="synthetic_convergence_agent",
    )
    record_feedback(
        feedback,
        log_dir=feedback_log_dir,
        event_log=registry.operational.event_log,
        pack_id=pack.pack_id,
    )


# ---------------------------------------------------------------------------
# Periodic convergence loops
# ---------------------------------------------------------------------------


def _run_periodic_loops(
    *,
    registry: StoreRegistry,
    advisory_store: AdvisoryStore,
    stats: _LoopStats,
    generate_advisories: bool,
    advisory_min_sample_size: int = DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
) -> None:
    """Run the noise-tagging + advisory loops once.

    ``generate_advisories`` controls whether ``AdvisoryGenerator.generate``
    fires this pass. We only generate on the *first* periodic pass:
    ``AdvisoryGenerator`` mints fresh ULIDs every call without
    deduplicating against the existing store, so regenerating each
    batch would saddle every subsequent fitness pass with a brand-new
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
        # min_appearances=2 (the default) is fine — distractors recur
        # quickly under the round-robin query schedule.
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
        # Use small thresholds so the synthetic corpus surfaces
        # decisions; the production defaults expect 30+ presentations.
        min_presentations=2,
    )
    stats.advisories_boosted_total += len(fitness.advisories_boosted)
    stats.advisories_suppressed_total += len(fitness.advisories_suppressed)
    stats.advisories_restored_total += len(fitness.advisories_restored)
    stats.suppressed_ids.extend(fitness.advisories_suppressed)


# ---------------------------------------------------------------------------
# Convergence math
# ---------------------------------------------------------------------------


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


def _convergence_stats(rounds: list[_RoundResult]) -> _ConvergenceStats:
    weighted = [r.weighted_score for r in rounds]
    useful = [
        (r.items_referenced / r.items_served) if r.items_served else 0.0 for r in rounds
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    registry: StoreRegistry,
    *,
    seed: int = 0,
    rounds: int = DEFAULT_ROUNDS,
    feedback_batch_size: int = DEFAULT_FEEDBACK_BATCH_SIZE,
    traces_per_domain: int = DEFAULT_TRACES_PER_DOMAIN,
    entities_per_trace: int = DEFAULT_ENTITIES_PER_TRACE,
    success_coverage_threshold: float = DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    convergence_delta_regress_threshold: float = (CONVERGENCE_DELTA_REGRESS_THRESHOLD),
    regime_shift_round: int | None = None,
    regime_shift_replacement_count: int = DEFAULT_REGIME_SHIFT_REPLACEMENT_COUNT,
    advisory_min_sample_size: int = DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
) -> ScenarioReport:
    """Execute the agent-loop convergence scenario.

    The runner-supplied ``registry`` is used as-is. Tests construct an
    in-memory SQLite registry and pass it directly.

    Opt-in regime-shift demo: pass ``regime_shift_round=N`` to drop the
    first ``regime_shift_replacement_count`` required entities from the
    agent's grading after round ``N``. Combined with
    ``advisory_min_sample_size=2`` this exercises the suppression
    branch of ``run_advisory_fitness_loop`` end-to-end on a controlled
    corpus. Default kwargs leave both off so the convergence baseline
    is unchanged.
    """
    if rounds <= 0:
        msg = "rounds must be positive"
        raise ValueError(msg)
    if feedback_batch_size <= 0:
        msg = "feedback_batch_size must be positive"
        raise ValueError(msg)
    if regime_shift_round is not None and regime_shift_round < 0:
        msg = "regime_shift_round must be non-negative when set"
        raise ValueError(msg)

    corpus = generate_corpus(
        seed=seed,
        traces_per_domain=traces_per_domain,
        entities_per_trace=entities_per_trace,
    )

    findings: list[Finding] = []
    metrics: dict[str, float] = {
        "rounds": float(rounds),
        "feedback_batch_size": float(feedback_batch_size),
        "domain_count": float(len(DOMAIN_TEMPLATES)),
    }

    metrics["traces_ingested"] = float(_ingest_traces(registry, corpus))
    metrics["entities_upserted"] = float(_populate_entity_documents(registry, corpus))
    metrics["distractors_planted"] = float(_populate_distractor_documents(registry))

    # Advisory store is file-based; keep it under the runner-supplied
    # stores_dir when present, else fall back to a tmpdir created here.
    # The temp-dir fallback path covers the unit-test smoke (where the
    # registry uses an in-memory layout) without bleeding files.
    feedback_dir_holder = tempfile.TemporaryDirectory()
    feedback_dir = Path(feedback_dir_holder.name)
    advisory_dir_root = registry.stores_dir or feedback_dir
    advisory_store = AdvisoryStore(advisory_dir_root / "advisories.json")

    # PackBuilder reads ``advisory_store`` dynamically per-build, so
    # constructing it before the first generate() call is fine: the
    # store is empty at first, advisories appear after the first
    # periodic pass, then later rounds attach them and accumulate
    # presentations the fitness loop can score.
    builder = PackBuilder(
        strategies=[KeywordSearch(registry.knowledge.document_store)],
        event_log=registry.operational.event_log,
        advisory_store=advisory_store,
    )

    loop_stats = _LoopStats()
    round_results: list[_RoundResult] = []
    run_id = f"convergence_{seed:04d}"

    try:
        for round_index in range(rounds):
            query = _round_query(corpus, round_index)
            pack = _build_pack(builder, query)
            referenced, coverage, success = _grade_round(
                pack,
                query,
                coverage_threshold=success_coverage_threshold,
                round_index=round_index,
                regime_shift_round=regime_shift_round,
                regime_shift_replacement_count=regime_shift_replacement_count,
            )
            scores = _score_pack(pack, query)
            round_results.append(
                _RoundResult(
                    round_index=round_index,
                    domain=query.domain,
                    pack_id=pack.pack_id,
                    items_served=len(pack.items),
                    items_referenced=len(referenced),
                    coverage_fraction=coverage,
                    weighted_score=scores["weighted_score"],
                    success=success,
                )
            )
            _record_round_feedback(
                feedback_log_dir=feedback_dir,
                registry=registry,
                pack=pack,
                query=query,
                referenced=referenced,
                success=success,
                round_index=round_index,
                run_id=run_id,
            )
            if (round_index + 1) % feedback_batch_size == 0:
                _run_periodic_loops(
                    registry=registry,
                    advisory_store=advisory_store,
                    stats=loop_stats,
                    # Generate only on the first periodic pass so
                    # advisory_ids stay stable for the rest of the run.
                    generate_advisories=loop_stats.advisory_runs == 0,
                    advisory_min_sample_size=advisory_min_sample_size,
                )
    finally:
        feedback_dir_holder.cleanup()

    # Always run one final loop pass so the closing rounds' feedback is
    # reflected in the suppression / restoration counts even if rounds
    # is not a multiple of feedback_batch_size.
    if rounds % feedback_batch_size != 0:
        _run_periodic_loops(
            registry=registry,
            advisory_store=advisory_store,
            stats=loop_stats,
            generate_advisories=loop_stats.advisory_runs == 0,
            advisory_min_sample_size=advisory_min_sample_size,
        )

    convergence = _convergence_stats(round_results)
    metrics.update(_round_metrics(round_results))
    metrics.update(_loop_metrics(loop_stats))
    metrics.update(_convergence_metrics(convergence))
    findings.extend(_convergence_findings(convergence, loop_stats))

    # ``useful_delta`` is the primary convergence signal — it tracks the
    # fraction of pack items the agent actually referenced, which is
    # what the noise + advisory loops are supposed to improve.
    # ``weighted_delta`` (from ``evaluate_pack`` against the
    # domain_context profile) is reported as informational: that
    # profile weights breadth at 0.30, so a successful noise loop that
    # trims non-referenced items can correctly produce a negative
    # weighted_delta even while useful_delta climbs.
    status: ScenarioStatus
    if convergence.useful_delta < convergence_delta_regress_threshold:
        findings.append(
            Finding(
                severity="warn",
                message=(
                    f"useful-fraction delta {convergence.useful_delta:+.3f} "
                    f"below regression threshold "
                    f"{convergence_delta_regress_threshold:+.3f}"
                ),
            )
        )
        status = "regress"
    else:
        status = "pass"

    decision = (
        "Per-round weighted scores + first-vs-last-quarter delta are "
        "produced. Three downstream signals become actionable based on "
        "the suppression / restoration counts and the delta sign:\n"
        "  * advisory fitness loop validation — if "
        "advisories_suppressed_total > 0 with no regressions, the "
        "suppression / restoration semantics work on this controlled "
        "corpus.\n"
        "  * confidence-gate escalation — track failure rate over "
        "rounds; sustained low coverage signals confidence-gate failure "
        "patterns worth escalating.\n"
        "  * sustained-volume baseline — this scenario produces the "
        "workload pattern an enrichment loop would consume; pin a "
        "baseline of convergence.weighted_delta and watch for drift."
    )

    return ScenarioReport(
        name="agent_loop_convergence",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )


# ---------------------------------------------------------------------------
# Metric / finding aggregation
# ---------------------------------------------------------------------------


def _round_metrics(rounds: list[_RoundResult]) -> dict[str, float]:
    if not rounds:
        return {}
    weighted_scores = [r.weighted_score for r in rounds]
    coverage = [r.coverage_fraction for r in rounds]
    successes = sum(1 for r in rounds if r.success)
    served = sum(r.items_served for r in rounds)
    referenced = sum(r.items_referenced for r in rounds)
    metrics: dict[str, float] = {
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
    metrics.update(_per_domain_round_metrics(rounds))
    return metrics


def _per_domain_round_metrics(rounds: list[_RoundResult]) -> dict[str, float]:
    by_domain: dict[str, list[_RoundResult]] = {}
    for r in rounds:
        by_domain.setdefault(r.domain, []).append(r)
    out: dict[str, float] = {}
    for domain, items in by_domain.items():
        scores = [r.weighted_score for r in items]
        out[f"per_domain.{domain}.weighted_score_mean"] = round(
            statistics.fmean(scores), 4
        )
        out[f"per_domain.{domain}.success_rate"] = round(
            sum(1 for r in items if r.success) / len(items), 4
        )
    return out


def _convergence_metrics(convergence: _ConvergenceStats) -> dict[str, float]:
    return {
        "convergence.weighted_first_quarter_mean": round(
            convergence.weighted_first_quarter_mean, 4
        ),
        "convergence.weighted_last_quarter_mean": round(
            convergence.weighted_last_quarter_mean, 4
        ),
        "convergence.weighted_delta": round(convergence.weighted_delta, 4),
        "convergence.useful_first_quarter_mean": round(
            convergence.useful_first_quarter_mean, 4
        ),
        "convergence.useful_last_quarter_mean": round(
            convergence.useful_last_quarter_mean, 4
        ),
        "convergence.useful_delta": round(convergence.useful_delta, 4),
    }


def _loop_metrics(stats: _LoopStats) -> dict[str, float]:
    return {
        "loops.effectiveness_runs": float(stats.effectiveness_runs),
        "loops.noise_items_tagged_total": float(stats.noise_items_tagged_total),
        "loops.advisory_runs": float(stats.advisory_runs),
        "loops.advisories_generated_total": float(stats.advisories_generated_total),
        "loops.advisories_suppressed_total": float(stats.advisories_suppressed_total),
        "loops.advisories_restored_total": float(stats.advisories_restored_total),
        "loops.advisories_boosted_total": float(stats.advisories_boosted_total),
    }


def _convergence_findings(
    convergence: _ConvergenceStats, stats: _LoopStats
) -> Iterable[Finding]:
    yield Finding(
        severity="info",
        message=(
            f"weighted score: {convergence.weighted_first_quarter_mean:.3f} "
            f"→ {convergence.weighted_last_quarter_mean:.3f} "
            f"(Δ {convergence.weighted_delta:+.3f})"
        ),
        detail={
            "useful_fraction_first_quarter": round(
                convergence.useful_first_quarter_mean, 4
            ),
            "useful_fraction_last_quarter": round(
                convergence.useful_last_quarter_mean, 4
            ),
        },
    )
    yield Finding(
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
        detail={
            "suppressed_ids": stats.suppressed_ids[:20],
        },
    )
