"""Master ``program_convergence`` scenario — nine-axis multi-loop signal.

Composes the dual-loop convergence curve (axes A + B, the same curve
agent_loop_convergence produces) with the seven program-level signals
(axes C-I) introduced by the self-improvement program. See
``docs/design/plan-program-level-eval.md`` §4.1 for the axis table and
``docs/agent-guide/program-convergence-eval.md`` for the operator-facing
reference.

The scenario runs the same per-round loop as agent_loop_convergence:
build pack → grade coverage → record feedback → periodic
effectiveness + advisory loops. On top of that, at every round it
captures the nine axis values into a single :class:`_NineAxisRound`
snapshot. See ``docs/agent-guide/program-convergence-eval.md`` for the
full axis table. Briefly:

- A. Pack quality — ``evaluate_pack`` weighted score.
- B. Useful-item fraction — ``items_referenced / items_served``.
- C. Advisory hit rate — advisories whose recommendation was honoured
  and whose outcome was success.
- D. Observation enrichment — count of Observation/Measurement nodes
  attached to seed entities this round.
- E. Provenance queryability — fraction of seed entities for which the
  ``confidence < 0.5`` edge probe resolves cleanly.
- F. Extraction-failure cluster decay — open
  ``(source_hint, failure_kind)`` clusters not yet drafted.
- G. Schema-evolution candidate emergence — new
  ``WELL_KNOWN_CANDIDATE`` events per cadence round.
- H. Meta-trace density — ``Activity`` nodes added per cadence round.
- I. Self-authored proposals — ``PROPOSAL_DRAFTED`` events emitted per
  cadence round.

POC directives applied:
- No silent fallback. Every axis source must be reachable; if any is
  missing the scenario raises :class:`ProgramConvergenceError` and the
  runner surfaces the error as a ``fail`` status. The strict-mode
  guarantee is documented in the user-facing doc.
- Deterministic. Seeds derived from the runner-supplied ``seed`` /
  ``invocation_id``. No live LLM calls — the per-round work uses the
  same KeywordSearch substrate as the existing convergence scenarios.

Out of scope (deferred to plan §4.2 and §4.3):
- Regression assertions (``--strict`` thresholds).
- Matplotlib PNG renderer.
"""

from __future__ import annotations

import random
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    DEFAULT_FEEDBACK_BATCH_SIZE,
    DEFAULT_PACK_MAX_ITEMS,
    DEFAULT_PACK_MAX_TOKENS,
    DEFAULT_PROFILE_NAME,
    DEFAULT_ROUNDS,
    DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    NINE_AXIS_LABELS,
    _build_multi_axis_stats,
    _convergence_metrics,
    _loop_metrics,
    _loops_summary_finding,
    _LoopStats,
    _multi_axis_metrics,
    _NineAxisRound,
    _record_round_feedback,
    _run_periodic_loops,
    _validate_basic_kwargs,
)
from trellis.learning import (
    RECOMMENDED_SEED_VALUES,
    analyze_well_known_candidates,
)
from trellis.learning.schema_evolution import PARAM_COMPONENT_ID
from trellis.meta import DEFAULT_META_AGENT_ID, record_meta_analysis
from trellis.ops import ParameterRegistry
from trellis.retrieve.evaluate import (
    BUILTIN_PROFILES,
    EvaluationScenario,
    evaluate_pack,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch
from trellis.schemas import well_known as wk
from trellis.schemas.pack import Pack, PackBudget
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.base.graph_query import EdgeQuery, FilterClause
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.parameter import SQLiteParameterStore

logger = structlog.get_logger(__name__)


SCENARIO_NAME = "program_convergence"

# Synthetic-corpus knobs — match agent_loop_convergence's defaults so
# the master's per-round work has the same shape as the legacy curve.
DEFAULT_TRACES_PER_DOMAIN = 6
DEFAULT_ENTITIES_PER_TRACE = 3

# Master-scenario specific knobs.
DEFAULT_ANALYZER_CADENCE = 5  # rounds between Item-5 / Item-6 / Item-7 passes
# Why: synthetic profile — Phase 2's regression suite asserts >=10 observations
# per seed by round 25 (~22 seeds * 10 / 25 ~= 8.8/round); real corpora seed
# observations from query logs at a lower rate.
DEFAULT_OBSERVATION_BATCH = 10  # observations seeded per round per seed entity
DEFAULT_FAILURE_BATCH = 3  # synthetic EXTRACTION_FAILED events per round
DEFAULT_PROPOSAL_WINDOW_HOURS = 24

# Provenance edge probe — axis E asks "does the EdgeQuery DSL accept
# confidence < 0.5 against the live edges produced by this run?". The
# probe is read-only; it never asserts non-empty results (an empty
# result is still a sane outcome, the failure mode is the backend
# erroring on the predicate compile).
_PROVENANCE_PROBE_CONFIDENCE: float = 0.5

# Advisory hit rate observation window — fraction of *recently
# referenced* item_ids the active advisory recommends that ALSO map to
# a successful round in the current batch. Looser than the prose
# definition in the plan; tight enough that a misfiring advisory drops
# the rate. See ``_compute_advisory_hit_rate``.
_ADVISORY_HIT_LOOKBACK_ROUNDS = 5

#: Seed for the per-round RNG. Combined with the ``seed`` kwarg so
#: re-running with the same seed produces byte-identical numbers (POC
#: determinism directive).
_RNG_SEED_OFFSET = 0xC0FFEE


class ProgramConvergenceError(RuntimeError):
    """Raised when an axis source is unreachable.

    The master scenario refuses to silently emit zero for an axis when
    its machinery is missing — operators who run the suite against an
    incomplete tree must see *which* item's wiring is broken, not a
    flat green line that hides the gap. See plan §3.
    """


@dataclass
class _RoundResult:
    """Per-round bookkeeping — extends agent_loop's shape with axis values.

    The five legacy fields (``weighted_score``, ``items_served``,
    ``items_referenced``, ``coverage_fraction``, ``success``) feed into
    :func:`_convergence_stats` exactly like agent_loop_convergence's
    ``_RoundResult``. The nine ``axis_*`` fields carry the additive
    program-level signal.
    """

    round_index: int
    domain: str
    pack_id: str
    items_served: int
    items_referenced: int
    coverage_fraction: float
    weighted_score: float
    success: bool
    axis_pack_quality: float
    axis_useful_item_fraction: float
    axis_advisory_hit_rate: float
    axis_observation_enrichment: float
    axis_provenance_queryability: float
    axis_extraction_failure_clusters: float
    axis_schema_evolution_candidates: float
    axis_meta_trace_density: float
    axis_self_authored_proposals: float

    def to_nine_axis(self) -> _NineAxisRound:
        return _NineAxisRound(
            round_index=self.round_index,
            weighted_score=self.weighted_score,
            items_served=self.items_served,
            items_referenced=self.items_referenced,
            coverage_fraction=self.coverage_fraction,
            success=self.success,
            axis_pack_quality=self.axis_pack_quality,
            axis_useful_item_fraction=self.axis_useful_item_fraction,
            axis_advisory_hit_rate=self.axis_advisory_hit_rate,
            axis_observation_enrichment=self.axis_observation_enrichment,
            axis_provenance_queryability=self.axis_provenance_queryability,
            axis_extraction_failure_clusters=self.axis_extraction_failure_clusters,
            axis_schema_evolution_candidates=self.axis_schema_evolution_candidates,
            axis_meta_trace_density=self.axis_meta_trace_density,
            axis_self_authored_proposals=self.axis_self_authored_proposals,
        )


# ---------------------------------------------------------------------------
# Setup helpers — mirror agent_loop_convergence; minor changes documented
# inline where they exist.
# ---------------------------------------------------------------------------


def _ingest_traces(registry: StoreRegistry, corpus: GeneratedCorpus) -> int:
    trace_store = registry.operational.trace_store
    for gt in corpus.traces:
        trace_store.append(gt.trace)
    return len(corpus.traces)


def _populate_entity_documents(
    registry: StoreRegistry, corpus: GeneratedCorpus
) -> list[str]:
    """Seed graph + document store; return the list of seed entity ids."""
    knowledge = registry.knowledge
    graph_store = knowledge.graph_store
    document_store = knowledge.document_store

    by_entity: dict[str, list[GeneratedTrace]] = {}
    for gt in corpus.traces:
        for entity in gt.entities:
            by_entity.setdefault(entity, []).append(gt)

    seed_entities: list[str] = []
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
                "content_tags": {"signal_quality": "standard"},
            },
        )
        seed_entities.append(entity)
    return seed_entities


def _verify_axis_machinery(registry: StoreRegistry) -> None:
    """Raise loudly if any required axis source is missing.

    Plan §3 directive: a master run against an incomplete tree must
    surface *which* item's machinery is broken, not silently emit zero
    for the affected axis. We probe each substrate eagerly — better to
    fail before round 0 than after 50 rounds of partial signal.
    """
    operational = registry.operational
    knowledge = registry.knowledge

    if operational.event_log is None:
        msg = (
            "EventLog is not wired on the runner-supplied registry; axes "
            "C, F, G, H, I depend on it. Refusing to silently emit zero."
        )
        raise ProgramConvergenceError(msg)

    graph_store = knowledge.graph_store
    if graph_store is None:
        msg = (
            "GraphStore is not wired; axes D + E + H depend on it. "
            "Check the registry's knowledge plane configuration."
        )
        raise ProgramConvergenceError(msg)

    # The EdgeQuery DSL is the substrate for axis E. If the backend
    # cannot compile a ``confidence < 0.5`` predicate, the scenario is
    # measuring an axis that doesn't exist for this backend.
    try:
        graph_store.execute_edge_query(
            EdgeQuery(
                filters=(
                    FilterClause(
                        field="confidence",
                        op="lt",
                        value=_PROVENANCE_PROBE_CONFIDENCE,
                    ),
                ),
                limit=1,
            )
        )
    except Exception as exc:
        msg = (
            "GraphStore.execute_edge_query failed on the provenance-column "
            f"probe (axis E): {type(exc).__name__}: {exc}. Item 2's machinery "
            "is missing or the backend has not been upgraded — refusing to "
            "silently emit 0 for axis E."
        )
        raise ProgramConvergenceError(msg) from exc


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
        tag_filters={},
    )


def _grade_round(
    pack: Pack,
    query: EvalQuery,
    *,
    coverage_threshold: float,
) -> tuple[list[str], float, bool]:
    """Same shape as agent_loop_convergence._grade_round, no regime-shift knobs."""
    required = list(query.required_coverage)
    pack_doc_ids = {item.item_id for item in pack.items}
    required_doc_ids = {f"doc:{entity}" for entity in required}
    referenced = sorted(pack_doc_ids & required_doc_ids)
    coverage = 1.0 if not required else len(referenced) / len(required)
    return referenced, coverage, coverage >= coverage_threshold


def _score_pack(pack: Pack, query: EvalQuery) -> float:
    eval_scenario = EvaluationScenario(
        name=f"program_convergence_{query.domain}",
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
    return report.weighted_score


# ---------------------------------------------------------------------------
# Axis measurement
# ---------------------------------------------------------------------------


def _seed_observations(
    registry: StoreRegistry,
    *,
    seed_entities: list[str],
    round_index: int,
    rng: random.Random,
    observed_at: datetime,
) -> int:
    """Item 1 — seed Observation/Measurement nodes for axis D.

    Returns the count attached to seed entities **this round**. The
    nodes are written through the GraphStore directly so we exercise
    the storage path without dragging the SDK into the scenario.
    """
    graph_store = registry.knowledge.graph_store
    if not seed_entities:
        return 0
    attached = 0
    # Round-robin across the seeds so axis D rises monotonically until
    # every seed has at least DEFAULT_OBSERVATION_BATCH observations.
    pick = seed_entities[round_index % len(seed_entities)]
    for i in range(DEFAULT_OBSERVATION_BATCH):
        obs_id = f"obs:program_convergence:{round_index:04d}:{i}:{pick}"
        # Synthetic confidence drawn from a deterministic RNG so re-runs
        # produce byte-identical confidence histograms.
        confidence = round(rng.uniform(0.3, 0.95), 3)
        graph_store.upsert_node(
            node_id=obs_id,
            node_type=wk.OBSERVATION,
            properties={
                "subject_entity_id": pick,
                "subject_entity_type": "entity",
                "observer_agent_id": "program_convergence_eval",
                "content": f"round {round_index} observation {i} on {pick}",
                "confidence": confidence,
                "observed_at": observed_at.isoformat(),
            },
        )
        # ``confidence`` lives on the edge's provenance column on every
        # built-in backend (Item 2). The ABC signature does not yet
        # declare the five provenance kwargs, so call sites carry a
        # type-ignore — same pattern as ``trellis.meta.recorder`` and
        # ``trellis_cli.admin_migrate_provenance``.
        graph_store.upsert_edge(  # type: ignore[call-arg]
            source_id=pick,
            target_id=obs_id,
            edge_type=wk.HAS_OBSERVATION,
            confidence=confidence,
        )
        attached += 1
    return attached


def _probe_provenance_queryability(
    registry: StoreRegistry,
    *,
    seed_entities: list[str],
) -> float:
    """Axis E — fraction of seed entities for which the predicate is sane.

    Axis E is the "did Item 2 land?" signal. Before Item 2 the column
    didn't exist; the backend would error on the predicate. After
    Item 2 a clean query against ``confidence < 0.5`` returns sane
    results (even if empty) for every seed entity. The probe runs
    one EdgeQuery per seed and counts the successes. We deliberately
    do *not* require non-empty results — an empty result for a seed
    with no low-confidence edges is still sane.
    """
    if not seed_entities:
        return 0.0
    graph_store = registry.knowledge.graph_store
    successes = 0
    for entity in seed_entities:
        try:
            graph_store.execute_edge_query(
                EdgeQuery(
                    filters=(
                        FilterClause(field="source_id", op="eq", value=entity),
                        FilterClause(
                            field="confidence",
                            op="lt",
                            value=_PROVENANCE_PROBE_CONFIDENCE,
                        ),
                    ),
                    limit=50,
                )
            )
            successes += 1
        except Exception:
            logger.debug(
                "axis_E_provenance_probe_failed",
                entity=entity,
            )
    return successes / len(seed_entities)


def _seed_extraction_failures(
    registry: StoreRegistry,
    *,
    round_index: int,
    rng: random.Random,
) -> None:
    """Item 4 — seed EXTRACTION_FAILED events that cluster predictably.

    The number of distinct ``(source_hint, failure_kind)`` clusters is
    axis F's signal. We rotate across a small pool so clusters stop
    growing (and proposal_generation can decay them in axis F).
    """
    event_log = registry.operational.event_log
    pool = [
        ("src/trellis/extract/llm.py", "parse_error"),
        ("src/trellis/extract/json_rules.py", "validation_error"),
        ("src/trellis_workers/learning/miner.py", "model_error"),
    ]
    for _ in range(DEFAULT_FAILURE_BATCH):
        source_hint, failure_kind = rng.choice(pool)
        event_log.emit(
            EventType.EXTRACTION_FAILED,
            source="eval.program_convergence",
            payload={
                "extractor_id": "eval.synthetic_extractor",
                "extractor_tier": "deterministic",
                "failure_kind": failure_kind,
                "source_hint": source_hint,
                "error_class": "ValueError",
                "error_excerpt": f"synthetic failure round={round_index}",
            },
        )


def _count_open_failure_clusters(registry: StoreRegistry) -> int:
    """Axis F — distinct unresolved ``(source_hint, failure_kind)`` clusters.

    "Open" means: at least one EXTRACTION_FAILED event has been emitted
    for this signature, and no PROPOSAL_DRAFTED event has *cleared* it
    yet (we treat a PROPOSAL_DRAFTED whose payload carries the same
    cluster_signature as closure). The signature derives from the same
    string :class:`compute_cluster_signature` builds. Item 4's
    machinery is the analyzer that surfaces these clusters; we read
    the EventLog directly so the axis works even when an operator
    hasn't run the analyzer this round.
    """
    from trellis_workers.code_authoring.clustering import (  # noqa: PLC0415
        compute_cluster_signature,
    )

    event_log = registry.operational.event_log
    failures = event_log.get_events(
        event_type=EventType.EXTRACTION_FAILED,
        limit=10_000,
    )
    open_signatures: set[str] = set()
    for event in failures:
        payload = event.payload or {}
        source_hint = payload.get("source_hint")
        failure_kind = payload.get("failure_kind")
        if not source_hint or not failure_kind:
            continue
        open_signatures.add(
            compute_cluster_signature(str(source_hint), str(failure_kind))
        )

    drafted = event_log.get_events(
        event_type=EventType.PROPOSAL_DRAFTED,
        limit=10_000,
    )
    for event in drafted:
        sig = (event.payload or {}).get("cluster_signature")
        if isinstance(sig, str):
            open_signatures.discard(sig)

    return len(open_signatures)


def _run_well_known_analyzer(
    registry: StoreRegistry,
    *,
    param_store: SQLiteParameterStore,
) -> int:
    """Item 5 — run the well-known analyzer once, return new candidates.

    Returns the **count of WELL_KNOWN_CANDIDATE events emitted** by
    this pass (the analyzer respects its cooldown gate, so re-runs in
    quick succession naturally return zero — that's the correct shape
    for axis G).
    """
    operational = registry.operational
    knowledge = registry.knowledge
    param_registry = ParameterRegistry(param_store)

    before_count = len(
        operational.event_log.get_events(
            event_type=EventType.WELL_KNOWN_CANDIDATE,
            limit=10_000,
        )
    )
    analyze_well_known_candidates(
        graph_store=knowledge.graph_store,
        event_log=operational.event_log,
        registry=param_registry,
    )
    after_count = len(
        operational.event_log.get_events(
            event_type=EventType.WELL_KNOWN_CANDIDATE,
            limit=10_000,
        )
    )
    return max(0, after_count - before_count)


def _run_meta_analysis(
    registry: StoreRegistry,
    *,
    round_index: int,
) -> int:
    """Item 6 — fire one meta-Activity, return Activity nodes added.

    Axis H should be **flat** under a sane sampling cap. The scenario
    deliberately fires a single Activity per analyzer-cadence round so
    a regression that breaks the sampling cap (one Activity per round
    becomes 50 per round) blows axis H upward and the chart catches
    it. We count the Activity-typed nodes before and after; the
    difference is the per-round density signal.
    """
    graph_store = registry.knowledge.graph_store
    before = graph_store.query(node_type=wk.ACTIVITY, limit=100_000)
    with record_meta_analysis(
        analyzer_name=f"program_convergence_round_{round_index:04d}",
        agent_id=DEFAULT_META_AGENT_ID,
        registry=registry,
    ) as record:
        if record.activity_id is None:
            msg = (
                "record_meta_analysis returned no activity_id — Item 6's "
                "machinery is disabled (check TRELLIS_META_TRACES env var). "
                "Refusing to silently emit zero for axis H."
            )
            raise ProgramConvergenceError(msg)
    after = graph_store.query(node_type=wk.ACTIVITY, limit=100_000)
    return max(0, len(after) - len(before))


def _run_proposal_generator(registry: StoreRegistry) -> int:
    """Item 7 — run the proposal generator once, return new proposals.

    Axis I should **rise in lockstep with axis F's decay**: every
    cluster the generator surfaces produces one PROPOSAL_DRAFTED
    event the first time it's seen, and the cluster signature is then
    counted against axis F's "open" set on the next round.
    """
    from trellis_workers.code_authoring import (  # noqa: PLC0415
        ProposalGenerator,
    )

    event_log = registry.operational.event_log
    before = len(
        event_log.get_events(event_type=EventType.PROPOSAL_DRAFTED, limit=10_000)
    )
    ProposalGenerator(
        registry,
        window=timedelta(hours=DEFAULT_PROPOSAL_WINDOW_HOURS),
    ).run()
    after = len(
        event_log.get_events(event_type=EventType.PROPOSAL_DRAFTED, limit=10_000)
    )
    return max(0, after - before)


def _compute_advisory_hit_rate(
    *,
    advisory_store: AdvisoryStore,
    recent_rounds: list[_RoundResult],
) -> float:
    """Axis C — fraction of active advisories whose entity recommendation
    co-occurred with at least one successful recent round.

    The plan defines axis C as "advisories whose recommendation was
    followed AND outcome=success". We operationalise it as: walk every
    active advisory, look up its ``entity_id`` (the canonical
    recommendation target), and count it as a hit if at least one
    recent round both **referenced** the entity (or the doc derived
    from it) and **succeeded**. Suppressed advisories don't count
    against the rate — they're correctly retired and shouldn't drag
    the fitness metric.

    Returns 0.0 when there are no active advisories yet (pre-first-pass).
    """
    # advisory_store.list() defaults to active-only — suppressed
    # advisories are correctly retired and shouldn't drag axis C.
    actives = advisory_store.list()
    if not actives:
        return 0.0
    if not recent_rounds:
        return 0.0
    success_refs: set[str] = set()
    for r in recent_rounds:
        if r.success:
            # ``items_referenced`` is a count on _RoundResult; the
            # underlying item_ids are baked into the pack but we don't
            # carry them on the dataclass to keep it slim. As a proxy,
            # we count an advisory as "hit" when at least one recent
            # round succeeded for the same domain. This is the same
            # semantic the dual-loop scenarios use to grade per-domain
            # success; the master scenario doesn't need finer grain.
            success_refs.add(r.domain)
    hits = 0
    for adv in actives:
        # ``scope`` carries the domain on entity-scoped advisories;
        # "global" advisories count as a hit when any recent round
        # succeeded (they're not domain-restricted).
        scope = adv.scope or "global"
        if scope == "global":
            hits += 1 if success_refs else 0
        elif scope in success_refs:
            hits += 1
    return hits / len(actives)


# ---------------------------------------------------------------------------
# Per-round driver — kept separate from ``run`` so the entry point stays
# under the PLR0915 statement budget and the round body has its own
# scope for debugging / breakpoint placement.
# ---------------------------------------------------------------------------


def _execute_round(
    *,
    round_index: int,
    registry: StoreRegistry,
    corpus: GeneratedCorpus,
    builder: PackBuilder,
    advisory_store: AdvisoryStore,
    param_store: SQLiteParameterStore,
    seed_entities: list[str],
    round_results: list[_RoundResult],
    loop_stats: _LoopStats,
    rng: random.Random,
    feedback_dir: Path,
    run_id: str,
    observed_at_base: datetime,
    success_coverage_threshold: float,
    analyzer_cadence: int,
    feedback_batch_size: int,
    advisory_min_sample_size: int,
) -> None:
    """One round of context build → grade → feedback → axis capture.

    Mutates ``round_results`` and ``loop_stats`` in place. The
    long-lived state — feedback_dir, run_id, observed_at_base — is
    passed through so the helper has no module-level state.
    """
    query = _round_query(corpus, round_index)
    pack = _build_pack(builder, query)
    referenced, coverage, success = _grade_round(
        pack,
        query,
        coverage_threshold=success_coverage_threshold,
    )
    weighted = _score_pack(pack, query)

    axis_d_round = _seed_observations(
        registry,
        seed_entities=seed_entities,
        round_index=round_index,
        rng=rng,
        observed_at=observed_at_base + timedelta(minutes=round_index),
    )
    _seed_extraction_failures(registry, round_index=round_index, rng=rng)

    # Axis G + H + I fire only on cadence rounds — running the analyzers
    # every round would inflate axis H (meta-trace density) and gum up
    # axis G's cooldown gate, masking the signal we're trying to surface.
    axis_g_round = 0
    axis_h_round = 0
    axis_i_round = 0
    if (round_index + 1) % analyzer_cadence == 0:
        axis_g_round = _run_well_known_analyzer(registry, param_store=param_store)
        axis_h_round = _run_meta_analysis(registry, round_index=round_index)
        axis_i_round = _run_proposal_generator(registry)

    axis_e = _probe_provenance_queryability(registry, seed_entities=seed_entities)
    axis_f = _count_open_failure_clusters(registry)
    lookback = round_results[-_ADVISORY_HIT_LOOKBACK_ROUNDS:]
    axis_c = _compute_advisory_hit_rate(
        advisory_store=advisory_store,
        recent_rounds=lookback,
    )

    useful_fraction = len(referenced) / len(pack.items) if pack.items else 0.0

    round_results.append(
        _RoundResult(
            round_index=round_index,
            domain=query.domain,
            pack_id=pack.pack_id,
            items_served=len(pack.items),
            items_referenced=len(referenced),
            coverage_fraction=coverage,
            weighted_score=weighted,
            success=success,
            axis_pack_quality=weighted,
            axis_useful_item_fraction=useful_fraction,
            axis_advisory_hit_rate=axis_c,
            axis_observation_enrichment=float(axis_d_round),
            axis_provenance_queryability=axis_e,
            axis_extraction_failure_clusters=float(axis_f),
            axis_schema_evolution_candidates=float(axis_g_round),
            axis_meta_trace_density=float(axis_h_round),
            axis_self_authored_proposals=float(axis_i_round),
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
        agent_id="program_convergence_agent",
    )

    if (round_index + 1) % feedback_batch_size == 0:
        _run_periodic_loops(
            registry=registry,
            advisory_store=advisory_store,
            stats=loop_stats,
            # Same shape as agent_loop_convergence — generate once on
            # the first periodic pass so advisory IDs remain stable for
            # the fitness loop's accumulation.
            generate_advisories=loop_stats.advisory_runs == 0,
            advisory_min_sample_size=advisory_min_sample_size,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _validate_run_kwargs(*, rounds: int, feedback_batch_size: int) -> None:
    _validate_basic_kwargs(rounds=rounds, feedback_batch_size=feedback_batch_size)


def _render_chart(stats: object, *, invocation_id: str) -> Path:
    """Render the 9-axis PNG via the eval-reports renderer.

    Lazy-imported so a registry that runs the scenario without
    ``render_chart=True`` never pulls matplotlib at import time. Any
    error from the renderer (missing matplotlib, invalid args) is left
    to propagate — the scenario's success/failure status does not
    depend on whether the chart rendered.
    """
    from eval.reports.program_convergence_chart import (  # noqa: PLC0415
        render_program_convergence_chart,
    )

    chart_path = render_program_convergence_chart(
        stats,  # type: ignore[arg-type]
        output_dir=Path("eval/reports"),
        invocation_id=invocation_id,
    )
    logger.info(
        "program_convergence_chart_written",
        chart_path=str(chart_path),
        invocation_id=invocation_id,
    )
    return chart_path


def run(
    registry: StoreRegistry,
    *,
    seed: int = 0,
    rounds: int = DEFAULT_ROUNDS,
    feedback_batch_size: int = DEFAULT_FEEDBACK_BATCH_SIZE,
    traces_per_domain: int = DEFAULT_TRACES_PER_DOMAIN,
    entities_per_trace: int = DEFAULT_ENTITIES_PER_TRACE,
    success_coverage_threshold: float = DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    advisory_min_sample_size: int = DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    analyzer_cadence: int = DEFAULT_ANALYZER_CADENCE,
    render_chart: bool = False,
    invocation_id: str | None = None,
) -> ScenarioReport:
    """Execute the program-level master scenario.

    See the module docstring for the axis table and POC directives.
    The runner-supplied ``registry`` is used as-is; tests pass a fresh
    in-memory SQLite registry.

    When ``render_chart=True`` the in-memory ``_MultiAxisStats`` payload
    is passed to ``render_program_convergence_chart`` from
    ``eval.reports.program_convergence_chart`` and the resulting PNG
    path is logged at info level and surfaced in
    ``ScenarioReport.metrics['chart_path']``. The rendered ``stats`` is
    also attached to ``ScenarioReport.convergence_stats`` so a post-hoc
    caller can re-render without re-running the loop.

    ``invocation_id`` defaults to ``program_convergence_{seed:04d}`` —
    the same identifier used as the per-run ``run_id`` for feedback
    file partitioning. Operators driving the scenario from a parent
    harness may override to align with their own ID space.
    """
    _validate_run_kwargs(rounds=rounds, feedback_batch_size=feedback_batch_size)

    # Verify every axis substrate is reachable BEFORE round 0 — better
    # to fail loud at setup than to emit partial signal for 30 rounds.
    _verify_axis_machinery(registry)

    rng = random.Random(seed ^ _RNG_SEED_OFFSET)  # noqa: S311 — synthetic, not crypto
    observed_at_base = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)

    corpus = generate_corpus(
        seed=seed,
        traces_per_domain=traces_per_domain,
        entities_per_trace=entities_per_trace,
    )

    findings: list[Finding] = []
    # Widened to ``float | str`` because this scenario sets a string
    # ``chart_path`` metric on the resulting report when
    # ``render_chart=True``; every other key stays float.
    metrics: dict[str, float | str] = {
        "rounds": float(rounds),
        "feedback_batch_size": float(feedback_batch_size),
        "domain_count": float(len(DOMAIN_TEMPLATES)),
        "analyzer_cadence": float(analyzer_cadence),
    }

    metrics["traces_ingested"] = float(_ingest_traces(registry, corpus))
    seed_entities = _populate_entity_documents(registry, corpus)
    metrics["seed_entities"] = float(len(seed_entities))

    feedback_dir_holder = tempfile.TemporaryDirectory()
    feedback_dir = Path(feedback_dir_holder.name)
    advisory_dir_root = registry.stores_dir or feedback_dir
    advisory_store = AdvisoryStore(advisory_dir_root / "advisories.json")

    # Parameter store for the well-known analyzer. We seed it inline
    # rather than reaching into the registry's parameter plane because
    # the analyzer requires every required key to be present, and the
    # registry's default seed leaves them unset.
    param_store_holder = tempfile.TemporaryDirectory()
    param_store = SQLiteParameterStore(Path(param_store_holder.name) / "params.db")
    _seed_well_known_parameters(param_store)

    builder = PackBuilder(
        strategies=[KeywordSearch(registry.knowledge.document_store)],
        event_log=registry.operational.event_log,
        advisory_store=advisory_store,
    )

    loop_stats = _LoopStats()
    round_results: list[_RoundResult] = []
    run_id = f"program_convergence_{seed:04d}"

    try:
        for round_index in range(rounds):
            _execute_round(
                round_index=round_index,
                registry=registry,
                corpus=corpus,
                builder=builder,
                advisory_store=advisory_store,
                param_store=param_store,
                seed_entities=seed_entities,
                round_results=round_results,
                loop_stats=loop_stats,
                rng=rng,
                feedback_dir=feedback_dir,
                run_id=run_id,
                observed_at_base=observed_at_base,
                success_coverage_threshold=success_coverage_threshold,
                analyzer_cadence=analyzer_cadence,
                feedback_batch_size=feedback_batch_size,
                advisory_min_sample_size=advisory_min_sample_size,
            )
    finally:
        feedback_dir_holder.cleanup()
        param_store.close()
        param_store_holder.cleanup()

    if rounds % feedback_batch_size != 0:
        _run_periodic_loops(
            registry=registry,
            advisory_store=advisory_store,
            stats=loop_stats,
            generate_advisories=loop_stats.advisory_runs == 0,
            advisory_min_sample_size=advisory_min_sample_size,
        )

    nine_axis_rounds = [r.to_nine_axis() for r in round_results]
    stats = _build_multi_axis_stats(nine_axis_rounds)

    metrics.update(_loop_metrics(loop_stats))
    metrics.update(_convergence_metrics(stats.convergence))
    metrics.update(_multi_axis_metrics(stats))

    findings.append(_loops_summary_finding(loop_stats))
    findings.extend(_per_axis_findings(stats))
    findings.append(_composite_convergence_finding(stats))

    resolved_invocation_id = invocation_id or run_id
    if render_chart:
        chart_path = _render_chart(stats, invocation_id=resolved_invocation_id)
        metrics["chart_path"] = str(chart_path)

    decision = (
        "Master program_convergence scenario ran nine axes end-to-end on "
        f"{rounds} synthetic rounds. Inspect per-axis deltas in the "
        "metrics block ('axis.<label>.delta'); plan §2.1 specifies which "
        "direction each axis should move. Phase 2 (regression suite, plan "
        "§4.2) and Phase 3 (PNG chart renderer, §4.3) consume the same "
        "axis metrics."
    )

    # Phase 0 deliberately does not gate status on per-axis thresholds —
    # those land with Phase 2. A clean run is "pass"; a setup-time
    # ProgramConvergenceError propagates up to the runner as "fail".
    status: ScenarioStatus = "pass"

    return ScenarioReport(
        name=SCENARIO_NAME,
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
        convergence_stats=stats,
    )


# ---------------------------------------------------------------------------
# Findings — one per axis plus a composite summary
# ---------------------------------------------------------------------------


def _per_axis_findings(stats: object) -> list[Finding]:
    """Emit one info finding per axis with first/last quarter means + delta.

    ``stats`` is a ``_MultiAxisStats`` (annotated as ``object`` to avoid
    a circular reference back into the helpers module — the type is
    structural, not nominal).
    """
    findings: list[Finding] = []
    axes = getattr(stats, "axes", {})
    for label in NINE_AXIS_LABELS:
        track = axes.get(label)
        if track is None:
            continue
        first = track.first_quarter_mean()
        last = track.last_quarter_mean()
        delta = track.delta()
        findings.append(
            Finding(
                severity="info",
                message=(f"{label}: {first:.3f} → {last:.3f} (Δ {delta:+.3f})"),
                detail={
                    "axis": label,
                    "first_quarter_mean": round(first, 4),
                    "last_quarter_mean": round(last, 4),
                    "delta": round(delta, 4),
                },
            )
        )
    return findings


def _composite_convergence_finding(stats: object) -> Finding:
    """Emit a single composite finding summarising every axis's trajectory."""
    axes = getattr(stats, "axes", {})
    axis_deltas: dict[str, float] = {}
    for label in NINE_AXIS_LABELS:
        track = axes.get(label)
        if track is None:
            continue
        axis_deltas[label] = round(track.delta(), 4)
    return Finding(
        severity="info",
        message=(
            "program_convergence multi-axis summary — see detail for "
            "the nine per-axis deltas"
        ),
        detail={"axis_deltas": axis_deltas},
    )


# ---------------------------------------------------------------------------
# Setup helpers — parameter seeding
# ---------------------------------------------------------------------------


def _seed_well_known_parameters(param_store: SQLiteParameterStore) -> None:
    """Persist the well-known thresholds the analyzer requires.

    Forces ``well_known_window_days=0`` so synthetic same-instant data
    isn't gated by the evidence-span filter (matches the satellite
    scenario's seed). Forces ``well_known_count_threshold=3`` so the
    cap fires on the small synthetic graph this scenario produces.
    """
    values: dict[str, float | int | str | bool] = dict(RECOMMENDED_SEED_VALUES)
    values["well_known_window_days"] = 0
    # Why: synthetic profile — the master scenario produces a small graph;
    # real corpora keep the production threshold of 10.
    values["well_known_count_threshold"] = 3
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id=PARAM_COMPONENT_ID),
            values=values,
            source="eval:program_convergence",
        )
    )
