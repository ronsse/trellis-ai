"""Degraded-retrieval convergence scenario.

What this is for
----------------

The baseline ``agent_loop_convergence`` scenario starts retrieval
already-precise: 2 distractors per domain, an 8-item pack budget that
fits every relevant entity. ``useful_delta`` lands near zero because
the loop has nothing to clean up — round 1 already serves the right
items.

This scenario starts from *deliberately degraded* retrieval — many
distractors that hit the query tokens, a tight 4-item budget that
forces real entities to compete — and runs long enough for the
effectiveness loop to tag distractors as ``signal_quality="noise"`` so
they get filtered out of subsequent packs. The expected trajectory:

* Q1 (rounds 0-49): useful_fraction ≈ 0.3-0.5 because distractors
  crowd half the pack on every round.
* Q4 (rounds 150-199): useful_fraction climbs as accumulated noise
  tags suppress the distractors and real entities take their slots.
* ``convergence.useful_delta`` sits comfortably positive — that's
  the chart the "improves with use" claim depends on.

Distractor design
-----------------

Distractors are doc-id-prefixed ``doc:distractor:<domain>:<n>`` so the
grader (which keys on ``doc:<entity_id>``) will never count them as
covering required entities. Their *content* mentions query tokens
freely — including entity names — so KeywordSearch BM25 ranks them
competitively against real entity docs. The whole point is for them
to win pack slots they don't deserve, then get demoted by the loop.

This scenario does not exercise the advisory loop's suppression
branch — that's exercised by ``agent_loop_convergence``'s opt-in
regime-shift mode. The dual loop's two halves are complementary:
effectiveness demotes noise, advisories boost lift. Here we lean on
the noise-tagging half because it's the half that closes the gap on a
degraded corpus.
"""

from __future__ import annotations

import statistics
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog

from eval.generators.trace_generator import (
    DOMAIN_TEMPLATES,
    EvalQuery,
    GeneratedCorpus,
    generate_corpus,
)
from eval.runner import Finding, ScenarioReport, ScenarioStatus
from eval.scenarios._convergence_common import (
    DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    DEFAULT_PROFILE_NAME,
    DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    _base_round_metrics,
    _convergence_metrics,
    _convergence_stats,
    _convergence_summary_finding,
    _ConvergenceStats,
    _loop_metrics,
    _loops_summary_finding,
    _LoopStats,
    _record_round_feedback,
    _run_periodic_loops,
    _validate_basic_kwargs,
)
from eval.scenarios.agent_loop_convergence.scenario import (
    _ingest_traces,
    _populate_entity_documents,
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

# Defaults — chosen so the loop has enough rounds * periodic passes to
# demonstrably tag noise. With batch_size=10 and 200 rounds, the loop
# fires 20 times — first batch produces ~no signal, last batch operates
# on ~200 packs of accumulated evidence.
DEFAULT_ROUNDS = 200
DEFAULT_FEEDBACK_BATCH_SIZE = 10
DEFAULT_PACK_MAX_ITEMS = 4  # tight enough that distractors crowd real entities
DEFAULT_PACK_MAX_TOKENS = 800
DEFAULT_TRACES_PER_DOMAIN = 6
DEFAULT_ENTITIES_PER_TRACE = 3
DEFAULT_DISTRACTORS_PER_DOMAIN = 15

# Useful-delta climb threshold the run must clear to count as a
# convergence demonstration, not just noise. The baseline scenario
# treats ``useful_delta < -0.05`` as a regression — here we're more
# demanding because the *whole point* is the climb, so we expect
# meaningful positive movement. Set to 0.10 — modest enough to absorb
# run-to-run variance, strong enough to mean the loop did real work.
USEFUL_DELTA_CLIMB_THRESHOLD = 0.10
ROUND_QUARTER_FRACTION = 4


# ---------------------------------------------------------------------------
# Per-domain distractor pool. Each doc:
#
# * Has id ``doc:distractor:<domain>:<idx>`` so the grader (which keys
#   on ``doc:<entity_id>``) cannot count it toward coverage.
# * Mentions one or more query tokens (entity names included) so
#   KeywordSearch BM25 ranks it competitively against real entity docs
#   and it actually wins pack slots.
# * Is unambiguously off-topic from the *real* required-coverage —
#   it'd be wrong for the agent to reference it.
#
# The dual loop's job is to learn that distinction over time.
# ---------------------------------------------------------------------------


_DISTRACTOR_DOCS: dict[str, list[tuple[str, str]]] = {
    "software_engineering": [
        (
            "doc:distractor:se:01",
            "session_token deprecation timeline. Migration guide for "
            "client libraries; not specific to the current validation "
            "flow.",
        ),
        (
            "doc:distractor:se:02",
            "validation queue redesign retrospective. Mentions "
            "auth_module in passing; off-topic for current integration "
            "work.",
        ),
        (
            "doc:distractor:se:03",
            "rate_limiter capacity planning notes. Historical thresholds "
            "across services; no integration guidance.",
        ),
        (
            "doc:distractor:se:04",
            "auth_module weekly newsletter archive. Mostly meeting "
            "summaries; no actionable rate_limiter detail.",
        ),
        (
            "doc:distractor:se:05",
            "Generic integration glossary. Defines validation, behavior, "
            "and structure as terms; no entity-specific advice.",
        ),
        (
            "doc:distractor:se:06",
            "session_token roadmap document. Lists future-quarter work; "
            "nothing on the present validation flow.",
        ),
        (
            "doc:distractor:se:07",
            "auth_module access reviews schedule. Audit calendar; not "
            "implementation-relevant for integration.",
        ),
        (
            "doc:distractor:se:08",
            "rate_limiter architecture decision review. Old proposals; "
            "no current behavior details.",
        ),
        (
            "doc:distractor:se:09",
            "validation retrospective compiled by QA. Process notes "
            "only; no auth_module integration guidance.",
        ),
        (
            "doc:distractor:se:10",
            "behavior testing coverage spreadsheet. Numbers across "
            "services; no structure guidance for session_token.",
        ),
        (
            "doc:distractor:se:11",
            "Org-wide integration budget memo. Mentions session_token "
            "infrastructure costs; no validation specifics.",
        ),
        (
            "doc:distractor:se:12",
            "Compliance tracker for auth_module audits. Status entries "
            "only; no implementation steps.",
        ),
        (
            "doc:distractor:se:13",
            "Archived chat logs on rate_limiter incidents. Truncated "
            "transcripts; no useful structure to extract.",
        ),
        (
            "doc:distractor:se:14",
            "Customer changelog mentioning session_token v3. Marketing "
            "copy; no validation flow implementation.",
        ),
        (
            "doc:distractor:se:15",
            "Defunct prototype for auth_module v0. Abandoned design; "
            "no rate_limiter integration story.",
        ),
    ],
    "data_pipeline": [
        (
            "doc:distractor:dp:01",
            "fact_table archive cleanup runbook. References fact_table "
            "by name but covers archival, not backfills.",
        ),
        (
            "doc:distractor:dp:02",
            "Generic upstream dependencies glossary. Mentions "
            "dependencies in passing; no concrete backfill steps.",
        ),
        (
            "doc:distractor:dp:03",
            "etl_job historical incident postmortem. Outage analysis; "
            "no fact_table backfill guidance.",
        ),
        (
            "doc:distractor:dp:04",
            "staging_table partitioning strategy memo. Storage layout "
            "discussion; no backfill specifics.",
        ),
        (
            "doc:distractor:dp:05",
            "Pipeline monitoring dashboard guide. Alerting setup; no "
            "fact_table or etl_job operational detail.",
        ),
        (
            "doc:distractor:dp:06",
            "warehouse_role provisioning policy. IAM document; no "
            "etl_job orchestration content.",
        ),
        (
            "doc:distractor:dp:07",
            "Generic pipeline glossary. Defines upstream, dependencies, "
            "backfills as terms; no concrete steps.",
        ),
        (
            "doc:distractor:dp:08",
            "fact_table billing forecast spreadsheet. Cost projections "
            "by quarter; no etl_job pipeline detail.",
        ),
        (
            "doc:distractor:dp:09",
            "staging_table schema evolution log. Historical column "
            "changes; no current backfill flow.",
        ),
        (
            "doc:distractor:dp:10",
            "etl_job retrospective archive. Quarterly review notes; no "
            "fact_table operational guidance.",
        ),
        (
            "doc:distractor:dp:11",
            "Pipeline retrospectives index page. Links to old reports; "
            "no upstream dependencies content.",
        ),
        (
            "doc:distractor:dp:12",
            "warehouse_role audit trail summary. Permission changes; "
            "no fact_table backfill workflow.",
        ),
        (
            "doc:distractor:dp:13",
            "etl_job tooling deprecation notice. Migration timeline; "
            "no staging_table operational steps.",
        ),
        (
            "doc:distractor:dp:14",
            "fact_table cost-allocation memo. Finance breakdown; no "
            "upstream pipeline guidance.",
        ),
        (
            "doc:distractor:dp:15",
            "Pipeline orientation slides for new hires. Overview deck; "
            "no etl_job backfill detail.",
        ),
    ],
    "customer_support": [
        (
            "doc:distractor:cs:01",
            "Marketing brief for refund campaigns. Mentions refund and "
            "billing tone but not policy.",
        ),
        (
            "doc:distractor:cs:02",
            "Quarterly summary of billing disputes volume. Numbers, "
            "no policy guidance.",
        ),
        (
            "doc:distractor:cs:03",
            "ticket_queue triage staffing plan. Headcount discussion; "
            "no refund_policy detail.",
        ),
        (
            "doc:distractor:cs:04",
            "billing_record archive retention schedule. Storage policy "
            "only; no dispute workflow content.",
        ),
        (
            "doc:distractor:cs:05",
            "refund_policy historical changelog. Past versions; not "
            "current dispute handling.",
        ),
        (
            "doc:distractor:cs:06",
            "ticket_queue tooling roadmap. Future features; no current "
            "billing_record dispute steps.",
        ),
        (
            "doc:distractor:cs:07",
            "billing_record format specification. Schema documentation; "
            "no refund_policy procedural content.",
        ),
        (
            "doc:distractor:cs:08",
            "refund_policy training slide deck. Onboarding material; "
            "not the active dispute runbook.",
        ),
        (
            "doc:distractor:cs:09",
            "ticket_queue volume forecast. Capacity model; no concrete "
            "billing dispute resolution.",
        ),
        (
            "doc:distractor:cs:10",
            "Generic dispute glossary. Defines billing, refund, and "
            "policy as terms; no concrete steps.",
        ),
        (
            "doc:distractor:cs:11",
            "billing_record retention compliance review. Audit notes; "
            "no refund_policy guidance.",
        ),
        (
            "doc:distractor:cs:12",
            "ticket_queue archived sprint reports. Velocity metrics; "
            "no billing dispute workflow.",
        ),
        (
            "doc:distractor:cs:13",
            "refund_policy edge case discussion archive. Hypotheticals; "
            "not the current ticket_queue procedure.",
        ),
        (
            "doc:distractor:cs:14",
            "billing dispute tone-of-voice guide. Communication style; "
            "no refund_policy substance.",
        ),
        (
            "doc:distractor:cs:15",
            "ticket_queue retirement migration plan. Decommission "
            "schedule; no refund dispute content.",
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


# ---------------------------------------------------------------------------
# Setup — distractor population (the differentiator from the baseline)
# ---------------------------------------------------------------------------


def _populate_distractor_documents(
    registry: StoreRegistry,
    *,
    distractors_per_domain: int,
) -> int:
    """Plant a heavy distractor pool — many docs that match query tokens.

    Each doc starts at ``signal_quality="standard"`` so PackBuilder's
    default tag filter passes them through. The effectiveness loop is
    what should learn to flip them to ``"noise"`` over time, after
    which the same filter excludes them from packs.
    """
    document_store = registry.knowledge.document_store
    planted = 0
    for domain, docs in _DISTRACTOR_DOCS.items():
        cap = min(distractors_per_domain, len(docs))
        for doc_id, content in docs[:cap]:
            document_store.put(
                doc_id=doc_id,
                content=content,
                metadata={
                    "domain": domain,
                    "content_type": "entity_summary",
                    "domains": [domain],
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


def _build_pack(
    builder: PackBuilder,
    query: EvalQuery,
    *,
    pack_max_items: int,
    pack_max_tokens: int,
) -> Pack:
    return builder.build(
        intent=query.intent,
        domain=query.domain,
        budget=PackBudget(
            max_items=pack_max_items,
            max_tokens=pack_max_tokens,
        ),
        # Empty dict triggers the default ``signal_quality`` filter that
        # excludes ``noise`` items — without this engagement, noise tags
        # applied by the effectiveness loop would have no read-time
        # effect and convergence couldn't be measured.
        tag_filters={},
    )


def _grade_round(
    pack: Pack,
    query: EvalQuery,
    *,
    coverage_threshold: float,
) -> tuple[list[str], float, bool]:
    """Determine which served items map to ground-truth entities.

    Returns ``(items_referenced, coverage_fraction, success)``.

    Distractor docs (``doc:distractor:<domain>:<n>``) cannot match the
    ``doc:<entity>`` form the grader keys on, so their content can
    freely mention entity names without being credited as coverage.
    """
    pack_doc_ids = {item.item_id for item in pack.items}
    required_doc_ids = {f"doc:{entity}" for entity in query.required_coverage}
    referenced = sorted(pack_doc_ids & required_doc_ids)
    coverage = (
        1.0 if not query.required_coverage
        else len(referenced) / len(query.required_coverage)
    )
    return referenced, coverage, coverage >= coverage_threshold


def _score_pack(pack: Pack, query: EvalQuery) -> dict[str, float]:
    eval_scenario = EvaluationScenario(
        name=f"degraded_{query.domain}",
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
    return {**report.dimensions, "weighted_score": report.weighted_score}


# ---------------------------------------------------------------------------
# Quarter-by-quarter trajectory — the chart this scenario produces
# ---------------------------------------------------------------------------


def _quarter_trajectory(rounds: list[_RoundResult]) -> list[float]:
    """Per-quarter useful-fraction means, oldest first.

    Returns a list of 4 floats; index 0 is the first quarter. Used to
    surface the convergence climb shape — a single delta hides whether
    progress was monotonic or noisy.

    Defensive against tiny round counts: when fewer than 4 samples are
    available, the full-sample mean is returned in every position so
    the resulting climb (q4-q1) is zero rather than noise.
    """
    if not rounds:
        return [0.0, 0.0, 0.0, 0.0]
    useful = [
        (r.items_referenced / r.items_served) if r.items_served else 0.0
        for r in rounds
    ]
    if len(useful) < ROUND_QUARTER_FRACTION:
        full = statistics.fmean(useful)
        return [full, full, full, full]
    n = len(useful)
    quarter = n // ROUND_QUARTER_FRACTION
    quarters = [
        useful[0:quarter],
        useful[quarter : 2 * quarter],
        useful[2 * quarter : 3 * quarter],
        # The last quarter takes any remainder so trajectory still
        # uses every round when ``rounds`` isn't divisible by 4.
        useful[3 * quarter : n],
    ]
    return [statistics.fmean(q) if q else 0.0 for q in quarters]


# ---------------------------------------------------------------------------
# Per-domain metric aggregation — discriminator is scenario-specific.
# ---------------------------------------------------------------------------


def _round_metrics(rounds: list[_RoundResult]) -> dict[str, float]:
    metrics = _base_round_metrics(rounds)
    if metrics:
        metrics.update(_per_domain_round_metrics(rounds))
    return metrics


def _per_domain_round_metrics(rounds: list[_RoundResult]) -> dict[str, float]:
    by_domain: dict[str, list[_RoundResult]] = {}
    for r in rounds:
        by_domain.setdefault(r.domain, []).append(r)
    out: dict[str, float] = {}
    for domain, items in by_domain.items():
        useful = [
            (r.items_referenced / r.items_served) if r.items_served else 0.0
            for r in items
        ]
        out[f"per_domain.{domain}.useful_fraction_mean"] = round(
            statistics.fmean(useful), 4
        )
        out[f"per_domain.{domain}.success_rate"] = round(
            sum(1 for r in items if r.success) / len(items), 4
        )
    return out


def _trajectory_metrics(trajectory: list[float]) -> dict[str, float]:
    return {
        f"convergence.quarters.useful_q{i + 1}_mean": round(v, 4)
        for i, v in enumerate(trajectory)
    }


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _findings(
    convergence: _ConvergenceStats,
    loop_stats: _LoopStats,
    trajectory: list[float],
) -> Iterable[Finding]:
    yield _convergence_summary_finding(convergence)
    yield _loops_summary_finding(loop_stats)
    yield Finding(
        severity="info",
        message=(
            "useful-fraction trajectory by quarter: "
            f"{trajectory[0]:.3f} → {trajectory[1]:.3f} → "
            f"{trajectory[2]:.3f} → {trajectory[3]:.3f}"
        ),
        detail={
            "quarter_means": [round(v, 4) for v in trajectory],
            "climb_q4_minus_q1": round(trajectory[3] - trajectory[0], 4),
        },
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
    pack_max_items: int = DEFAULT_PACK_MAX_ITEMS,
    pack_max_tokens: int = DEFAULT_PACK_MAX_TOKENS,
    traces_per_domain: int = DEFAULT_TRACES_PER_DOMAIN,
    entities_per_trace: int = DEFAULT_ENTITIES_PER_TRACE,
    distractors_per_domain: int = DEFAULT_DISTRACTORS_PER_DOMAIN,
    success_coverage_threshold: float = DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    useful_delta_climb_threshold: float = USEFUL_DELTA_CLIMB_THRESHOLD,
    advisory_min_sample_size: int = DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
) -> ScenarioReport:
    """Run the degraded-retrieval convergence scenario.

    The runner-supplied ``registry`` is used as-is. Tests construct an
    in-memory SQLite registry and pass it directly.

    Status semantics differ from the baseline scenario: ``pass``
    requires ``useful_delta >= useful_delta_climb_threshold`` (default
    +0.10), since a flat or barely-positive climb means the loop
    didn't earn its keep on a degraded corpus.
    """
    _validate_basic_kwargs(rounds=rounds, feedback_batch_size=feedback_batch_size)
    if pack_max_items <= 0:
        msg = "pack_max_items must be positive"
        raise ValueError(msg)
    if distractors_per_domain <= 0:
        msg = "distractors_per_domain must be positive"
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
        "pack_max_items": float(pack_max_items),
        "domain_count": float(len(DOMAIN_TEMPLATES)),
        "distractors_per_domain": float(distractors_per_domain),
    }

    metrics["traces_ingested"] = float(_ingest_traces(registry, corpus))
    metrics["entities_upserted"] = float(_populate_entity_documents(registry, corpus))
    metrics["distractors_planted"] = float(
        _populate_distractor_documents(
            registry, distractors_per_domain=distractors_per_domain
        )
    )

    feedback_dir_holder = tempfile.TemporaryDirectory()
    feedback_dir = Path(feedback_dir_holder.name)
    advisory_dir_root = registry.stores_dir or feedback_dir
    advisory_store = AdvisoryStore(advisory_dir_root / "advisories.json")

    builder = PackBuilder(
        strategies=[KeywordSearch(registry.knowledge.document_store)],
        event_log=registry.operational.event_log,
        advisory_store=advisory_store,
    )

    loop_stats = _LoopStats()
    round_results: list[_RoundResult] = []
    run_id = f"degraded_{seed:04d}"

    try:
        for round_index in range(rounds):
            query = _round_query(corpus, round_index)
            pack = _build_pack(
                builder,
                query,
                pack_max_items=pack_max_items,
                pack_max_tokens=pack_max_tokens,
            )
            referenced, coverage, success = _grade_round(
                pack,
                query,
                coverage_threshold=success_coverage_threshold,
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
                intent=query.intent,
                intent_family=query.domain,
                referenced=referenced,
                success=success,
                round_index=round_index,
                run_id=run_id,
                agent_id="degraded_convergence_agent",
            )
            if (round_index + 1) % feedback_batch_size == 0:
                _run_periodic_loops(
                    registry=registry,
                    advisory_store=advisory_store,
                    stats=loop_stats,
                    # Generate advisories only on the first periodic
                    # pass so advisory_ids stay stable for the rest of
                    # the run — same rationale as the baseline scenario.
                    generate_advisories=loop_stats.advisory_runs == 0,
                    advisory_min_sample_size=advisory_min_sample_size,
                )
    finally:
        feedback_dir_holder.cleanup()

    if rounds % feedback_batch_size != 0:
        _run_periodic_loops(
            registry=registry,
            advisory_store=advisory_store,
            stats=loop_stats,
            generate_advisories=loop_stats.advisory_runs == 0,
            advisory_min_sample_size=advisory_min_sample_size,
        )

    convergence = _convergence_stats(round_results)
    trajectory = _quarter_trajectory(round_results)
    metrics.update(_round_metrics(round_results))
    metrics.update(_loop_metrics(loop_stats))
    metrics.update(_convergence_metrics(convergence))
    metrics.update(_trajectory_metrics(trajectory))
    findings.extend(_findings(convergence, loop_stats, trajectory))

    # ``useful_delta`` is the load-bearing convergence signal here —
    # the whole scenario exists to demonstrate it climbing on a
    # degraded corpus. A flat or negative delta means either the loop
    # didn't tag enough noise or distractors weren't actually
    # competing — in either case the scenario didn't prove what it
    # claims.
    status: ScenarioStatus
    if convergence.useful_delta < useful_delta_climb_threshold:
        findings.append(
            Finding(
                severity="warn",
                message=(
                    f"useful-fraction climb {convergence.useful_delta:+.3f} "
                    f"below expected threshold "
                    f"{useful_delta_climb_threshold:+.3f} — the dual loop "
                    f"did not demonstrably improve retrieval on this "
                    f"degraded corpus."
                ),
            )
        )
        status = "regress"
    else:
        status = "pass"

    decision = (
        "Degraded-retrieval convergence run complete. "
        f"useful_delta={convergence.useful_delta:+.3f}; quarter "
        f"trajectory: {trajectory[0]:.2f} → {trajectory[3]:.2f}. "
        "Read the trajectory before the delta — a clean monotonic "
        "climb across all four quarters is what proves the loop is "
        "doing the work; a single jump might be a corpus artifact. "
        "If status is ``regress``, either distractors weren't winning "
        "pack slots (lower pack_max_items or raise "
        "distractors_per_domain) or the loop didn't get enough "
        "periodic passes (raise rounds)."
    )

    return ScenarioReport(
        name="agent_loop_convergence_degraded",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )
