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
from eval.scenarios._convergence_common import (
    CONVERGENCE_DELTA_REGRESS_THRESHOLD,
    DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    DEFAULT_FEEDBACK_BATCH_SIZE,
    DEFAULT_PACK_MAX_ITEMS,
    DEFAULT_PACK_MAX_TOKENS,
    DEFAULT_PROFILE_NAME,
    DEFAULT_ROUNDS,
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
    compute_advisory_hit_rate,
)
from trellis.retrieve.evaluate import (
    BUILTIN_PROFILES,
    EvaluationScenario,
    evaluate_pack,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.schemas.pack import Pack, PackBudget
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

# Scenario-local overrides — agent_loop ships an extra synthetic-corpus
# knob (traces/entities per trace) that other convergence scenarios
# don't expose because they load real corpora.
DEFAULT_TRACES_PER_DOMAIN = 6
DEFAULT_ENTITIES_PER_TRACE = 3

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
# Per-round bookkeeping — _RoundResult stays scenario-local because the
# discriminator is ``domain`` here vs ``skill``/``difficulty`` elsewhere.
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
    #: Per-item advisory provenance from ``PackItem.injected_advisory_ids``
    #: for this round's pack (one inner list per served item). Feeds the
    #: shared :func:`compute_advisory_hit_rate` — the same axis-C formula
    #: ``program_convergence`` uses.
    injected_advisory_ids_per_item: list[list[str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _populate_corpus_stores(
    registry: StoreRegistry,
    corpus: GeneratedCorpus,
    metrics: dict[str, float],
) -> None:
    """Seed traces + entity docs + distractors; stamp the setup metrics."""
    metrics["traces_ingested"] = float(_ingest_traces(registry, corpus))
    metrics["entities_upserted"] = float(_populate_entity_documents(registry, corpus))
    metrics["distractors_planted"] = float(_populate_distractor_documents(registry))


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


def _seed_reference_advisories(
    advisory_store: AdvisoryStore, corpus: GeneratedCorpus
) -> int:
    """Seed one high-confidence ENTITY advisory per required-coverage doc.

    Opt-in demo support (``seed_reference_advisory=True``). The default
    corpus never organically forms an item-scoped advisory that survives
    into packs — the lone entity advisory it produces targets a distractor
    the effectiveness loop then noise-filters out — so
    ``loops.advisory_hit_rate`` is structurally ``0.0``. Seeding a
    recommendation for each *ground-truth* required entity
    (``entity_id == "doc:{entity}" == PackItem.item_id``,
    ``scope="global"`` so it is surfaced for every domain's pack) lets
    those advisories stamp the required docs when they land in packs, so
    successful rounds register as advisory hits and the metric exercises a
    positive value end-to-end.

    This demonstrates the *measurement path* (advisory → PackBuilder stamp
    → per-round capture → ``compute_advisory_hit_rate``), not organic
    advisory generation: the seeded advisories stand in for ones the
    generator would form on a corpus with the right presence differential.
    Provenance stamping is pure annotation, so seeding does not change pack
    composition, coverage, or the convergence deltas.
    """
    seen: set[str] = set()
    advisories: list[Advisory] = []
    for query in corpus.queries:
        for entity in query.required_coverage:
            doc_id = f"doc:{entity}"
            if doc_id in seen:
                continue
            seen.add(doc_id)
            advisories.append(
                Advisory(
                    category=AdvisoryCategory.ENTITY,
                    confidence=0.95,
                    message=(
                        f"Entity {doc_id} is ground-truth context for "
                        f"{query.domain}; include it."
                    ),
                    evidence=AdvisoryEvidence(
                        sample_size=DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
                        success_rate_with=1.0,
                        success_rate_without=0.0,
                        effect_size=1.0,
                    ),
                    scope="global",
                    entity_id=doc_id,
                )
            )
    # One batched write — ``put`` rewrites the whole JSON file per call.
    return advisory_store.put_many(advisories)


# ---------------------------------------------------------------------------
# Organic-advisory staging mode (opt-in) — issue #248
# ---------------------------------------------------------------------------

#: The synthetic entity whose presence differential the staged mode
#: manufactures. Its name appears nowhere else in the corpus, so its doc
#: is retrieved exactly when the round intent mentions it.
_ORGANIC_PROBE_ENTITY = "organic_probe_entity"

#: The probe doc is present on every Nth visit to the staged domain
#: (visit % N == 0). 3 keeps absent-round failures frequent enough that
#: the with/without success differential clears the generator's
#: ``_MIN_EFFECT_SIZE`` on a short run.
_ORGANIC_PRESENCE_CADENCE = 3


def _plant_organic_probe_document(registry: StoreRegistry, domain: str) -> None:
    """Plant the staged mode's probe doc (absent from default runs).

    Content mentions only the probe's own name plus neutral words — none
    of the domain query's keywords — so KeywordSearch retrieves it *only*
    on rounds whose intent explicitly names it. That on/off switch is
    what manufactures the presence differential ``AdvisoryGenerator``
    needs (see :func:`_stage_organic_query`).
    """
    registry.knowledge.document_store.put(
        doc_id=f"doc:{_ORGANIC_PROBE_ENTITY}",
        content=(
            f"{_ORGANIC_PROBE_ENTITY} reference sheet. Curated background "
            f"for the {domain} probe rounds."
        ),
        metadata={
            "domain": domain,
            "content_type": "entity_summary",
            "domains": [domain],
            "content_tags": {"signal_quality": "standard"},
        },
    )


def _setup_organic_staging(
    registry: StoreRegistry,
    corpus: GeneratedCorpus,
    metrics: dict[str, float],
) -> str:
    """Plant the probe doc + stamp the setup metric; return the staged domain."""
    staged_domain = corpus.queries[0].domain
    _plant_organic_probe_document(registry, staged_domain)
    metrics["organic_probe_planted"] = 1.0
    return staged_domain


def _count_organic_advisories(advisory_store: AdvisoryStore) -> float:
    """How many ENTITY advisories the generator formed for the probe doc.

    Counts active + suppressed alike — formation is the claim under test
    (issue #248); fitness is scored separately.
    """
    organic_doc_id = f"doc:{_ORGANIC_PROBE_ENTITY}"
    return float(
        sum(
            1
            for advisory in advisory_store.list(include_suppressed=True)
            if advisory.category is AdvisoryCategory.ENTITY
            and advisory.entity_id == organic_doc_id
        )
    )


def _stage_organic_query(query: EvalQuery, visit_index: int) -> EvalQuery:
    """Return the staged-domain round's query for organic-advisory mode.

    Two edits relative to the canonical query:

    - ``required_coverage`` always gains the probe entity, so with a
      ``success_coverage_threshold`` above ``3/4`` the round succeeds
      **iff** the probe doc landed in the pack.
    - The intent mentions the probe's name only on every
      ``_ORGANIC_PRESENCE_CADENCE``-th visit, so the doc lands in a
      deterministic minority of packs.

    Net effect on the joined pack/feedback dataset: the probe appears in
    a handful of successful packs and is absent from failing ones — the
    exact ``success_rate_with - success_rate_without`` differential the
    ``AdvisoryGenerator`` requires to *organically* form an ENTITY
    advisory (issue #248). Nothing is pre-seeded into the advisory
    store; the generator does its own statistics over real events.
    """
    present = visit_index % _ORGANIC_PRESENCE_CADENCE == 0
    return EvalQuery(
        domain=query.domain,
        intent=(
            f"{query.intent} {_ORGANIC_PROBE_ENTITY}" if present else query.intent
        ),
        required_coverage=[*query.required_coverage, _ORGANIC_PROBE_ENTITY],
    )


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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _validate_run_kwargs(
    *,
    rounds: int,
    feedback_batch_size: int,
    regime_shift_round: int | None,
    regime_shift_replacement_count: int,
) -> None:
    """Reject contradictory kwarg combinations at the boundary."""
    _validate_basic_kwargs(rounds=rounds, feedback_batch_size=feedback_batch_size)
    if regime_shift_round is not None and regime_shift_round < 0:
        msg = "regime_shift_round must be non-negative when set"
        raise ValueError(msg)
    if regime_shift_round is not None and regime_shift_replacement_count <= 0:
        # 0 silently no-ops the swap (post-shift required_coverage equals
        # pre-shift) so a caller setting regime_shift_round=N expects
        # the demo behaviour and gets baseline. Reject explicitly.
        msg = (
            "regime_shift_replacement_count must be positive when "
            "regime_shift_round is set"
        )
        raise ValueError(msg)


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
    seed_reference_advisory: bool = False,
    stage_organic_advisory: bool = False,
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

    Opt-in advisory-hit demo: pass ``seed_reference_advisory=True`` to
    pre-seed a high-confidence ENTITY advisory per required-coverage doc,
    so ``loops.advisory_hit_rate`` exercises a positive value end-to-end
    (the default corpus leaves it at ``0.0`` — see
    :func:`_seed_reference_advisories`). Provenance stamping is pure
    annotation, so this does not change pack composition or the
    convergence deltas.

    Opt-in organic-advisory staging (issue #248): pass
    ``stage_organic_advisory=True`` to plant one probe doc and stage a
    deterministic presence differential in the first domain (see
    :func:`_stage_organic_query`), so the ``AdvisoryGenerator`` forms an
    ENTITY advisory **organically** — from its own statistics over real
    pack/feedback events, with nothing pre-seeded. Pair with
    ``success_coverage_threshold`` in ``(0.75, 1.0]`` and a small
    ``advisory_min_sample_size`` so the differential is resolvable on a
    short run; the staged domain's absent-probe rounds *fail by design*,
    so convergence deltas are meaningful only within this mode (defaults
    leave the mode off and the baseline untouched). Not designed to
    combine with ``regime_shift_round``.
    """
    _validate_run_kwargs(
        rounds=rounds,
        feedback_batch_size=feedback_batch_size,
        regime_shift_round=regime_shift_round,
        regime_shift_replacement_count=regime_shift_replacement_count,
    )

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

    _populate_corpus_stores(registry, corpus, metrics)

    # Organic-advisory staging (issue #248): one probe doc + a visit
    # counter; everything else in the loop is the production path.
    staged_domain = (
        _setup_organic_staging(registry, corpus, metrics)
        if stage_organic_advisory
        else None
    )
    staged_visits = 0

    # Advisory store is file-based; keep it under the runner-supplied
    # stores_dir when present, else fall back to a tmpdir created here.
    # The temp-dir fallback path covers the unit-test smoke (where the
    # registry uses an in-memory layout) without bleeding files.
    feedback_dir_holder = tempfile.TemporaryDirectory()
    feedback_dir = Path(feedback_dir_holder.name)
    advisory_dir_root = registry.stores_dir or feedback_dir
    advisory_store = AdvisoryStore(advisory_dir_root / "advisories.json")
    if seed_reference_advisory:
        metrics["reference_advisories_seeded"] = float(
            _seed_reference_advisories(advisory_store, corpus)
        )

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
            if staged_domain is not None and query.domain == staged_domain:
                query = _stage_organic_query(query, staged_visits)
                staged_visits += 1
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
                    injected_advisory_ids_per_item=[
                        list(item.injected_advisory_ids) for item in pack.items
                    ],
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
                agent_id="synthetic_convergence_agent",
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

    # When ``rounds`` is not a multiple of ``feedback_batch_size``, the
    # main-loop's modulo gate misses the closing rounds' feedback. Fire
    # one extra pass so those rounds still flow through the
    # effectiveness + advisory loops. When rounds IS a multiple, the
    # in-loop pass already covered them — no extra work needed.
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
    # Second deterministic source for the C3 advisory-hit-rate signal
    # (the first is program_convergence axis C). Same shared formula,
    # computed corpus-wide over every round: of all advisory presentations
    # across the run, the fraction that landed in a successful round.
    #
    # NOTE: on the default corpus this is legitimately 0.0 — and so is
    # program_convergence axis C. Item-scoped advisories only stamp an
    # item when ``advisory.entity_id == item.item_id`` (ENTITY /
    # ANTI_PATTERN categories); here the lone entity advisory targets a
    # distractor doc that the effectiveness loop tags as noise and the
    # PackBuilder then excludes, so no stamped item ever reaches a pack
    # (total_presented == 0). The metric lights up only once an
    # item-scoped advisory targets a doc that survives into packs — do
    # not read 0.0 as "advisories don't help" (the boost/suppress counts
    # carry the live C3 signal). Pass ``seed_reference_advisory=True`` to
    # exercise a positive value end-to-end (see
    # :func:`_seed_reference_advisories`). See
    # docs/plans/2026-06-17-step3-assessment.md §6.5.
    metrics["loops.advisory_hit_rate"] = round(
        compute_advisory_hit_rate(round_results), 4
    )
    if stage_organic_advisory:
        # Issue #248: did the AdvisoryGenerator form the probe's ENTITY
        # advisory from its own statistics (nothing pre-seeded)?
        metrics["organic_advisories_formed"] = _count_organic_advisories(
            advisory_store
        )
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
# Per-domain metric aggregation — the discriminator is scenario-specific,
# so this stays here rather than moving to the common module.
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
        scores = [r.weighted_score for r in items]
        out[f"per_domain.{domain}.weighted_score_mean"] = round(
            statistics.fmean(scores), 4
        )
        out[f"per_domain.{domain}.success_rate"] = round(
            sum(1 for r in items if r.success) / len(items), 4
        )
    return out


def _convergence_findings(
    convergence: _ConvergenceStats, stats: _LoopStats
) -> Iterable[Finding]:
    yield _convergence_summary_finding(convergence)
    yield _loops_summary_finding(stats)
