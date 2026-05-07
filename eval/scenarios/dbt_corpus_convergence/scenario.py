"""dbt corpus convergence — Phase B-1.

Replaces the synthetic generator + round-robin domain queries with the
Jaffle Shop dbt corpus + 12 hand-authored ground-truth queries. Reuses
the agent-loop math, periodic dual-loop firing, telemetry, and cost
guards from :mod:`eval.scenarios.agent_loop_convergence_real_llm`.

Three deliberate differences from Phase A:

1. **Corpus.** :func:`eval.corpora.dbt_loader.load_jaffle_shop_corpus`
   ingests the manifest fixture through the governed mutation pipeline,
   producing 21 entities + 22 ``dependsOn`` edges + 8 description docs.
2. **Test descriptions.** Tests have no manifest descriptions; this
   scenario synthesizes a short templated string at setup time so they
   become findable by KeywordSearch and SemanticSearch. The 13 test
   docs are added to the document store with the same id scheme
   (``doc:<entity_id>``) the loader uses for models / sources.
3. **Embeddings only.** No LLM chat calls — manifest descriptions are
   already concise and accurate. Cost per run: ~$0.0001 (embeddings
   alone).

The grader accepts both ``f"doc:{entity_id}"`` (from KeywordSearch /
SemanticSearch PackItems) and ``entity_id`` (from GraphSearch
PackItems, for forward compatibility) as covering a required entity.
"""

from __future__ import annotations

import asyncio
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from eval._real_llm import build_phase_a_clients
from eval.corpora.dbt_loader import (
    LoadResult,
    build_name_index,
    extract_seed_ids,
    load_jaffle_shop_corpus,
)
from eval.corpora.jaffle_shop.queries import (
    DBT_DOMAIN,
    GROUND_TRUTH_QUERIES,
    JaffleShopQuery,
)
from eval.runner import Finding, ScenarioReport, ScenarioStatus
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import record_feedback
from trellis.llm.protocol import EmbedderClient
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
from trellis.retrieve.strategies import (
    GraphSearch,
    KeywordSearch,
    SearchStrategy,
    SemanticSearch,
)
from trellis.schemas.pack import Pack, PackBudget
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

DEFAULT_ROUNDS = 30
DEFAULT_FEEDBACK_BATCH_SIZE = 5
DEFAULT_PACK_MAX_ITEMS = 8
DEFAULT_PACK_MAX_TOKENS = 1_500
DEFAULT_SUCCESS_COVERAGE_THRESHOLD = 0.6
DEFAULT_PROFILE_NAME = "domain_context"
CONVERGENCE_DELTA_REGRESS_THRESHOLD = -0.05
ROUND_WINDOW_FRACTION = 4
DEFAULT_ADVISORY_MIN_SAMPLE_SIZE = 5

OPENAI_EMBED_3_SMALL_USD_PER_M = 0.02
RUN_HARD_COST_CAP_USD = 0.50  # tighter than Phase A — embeddings only

# ---------------------------------------------------------------------------
# Telemetry — embeddings only (no chat)
# ---------------------------------------------------------------------------


@dataclass
class _EmbedRecord:
    model: str
    input_tokens: int
    latency_ms: int


@dataclass
class _Telemetry:
    embed_calls: list[_EmbedRecord] = field(default_factory=list)

    def record_embed(self, model: str, in_tok: int, latency_ms: int) -> None:
        self.embed_calls.append(_EmbedRecord(model, in_tok, latency_ms))

    def total_cost_usd(self) -> float:
        in_tok = sum(c.input_tokens for c in self.embed_calls)
        return (in_tok / 1e6) * OPENAI_EMBED_3_SMALL_USD_PER_M

    def to_metrics(self) -> dict[str, float]:
        lat = [c.latency_ms for c in self.embed_calls]
        return {
            "embedder.calls_total": float(len(self.embed_calls)),
            "embedder.input_tokens_total": float(
                sum(c.input_tokens for c in self.embed_calls)
            ),
            "cost.embed_usd": round(self.total_cost_usd(), 6),
            "cost.total_usd": round(self.total_cost_usd(), 6),
            "latency.embed_ms_p50": (
                round(statistics.median(lat), 1) if lat else 0.0
            ),
            "latency.embed_ms_max": float(max(lat) if lat else 0),
        }


# ---------------------------------------------------------------------------
# Test description templating — fills the gap left by tests having no
# description in the manifest.
# ---------------------------------------------------------------------------


def _test_description(test_entity: dict[str, Any]) -> str:
    """Synthesize a short, factual description for a dbt test entity.

    Format: "dbt test '<name>' validates <kind> on <targets>."

    Test names follow dbt conventions (``unique_<table>_<col>``,
    ``not_null_<table>_<col>``, ``accepted_values_<table>_<col>``,
    ``relationships_<src>_<col>__<col>__ref_<target>_``). We surface the
    test type and target model(s) so KeywordSearch and SemanticSearch
    can disambiguate based on intent.
    """
    name = test_entity.get("properties", {}).get("name", "") or ""
    entity_id = test_entity.get("node_id", "")

    if name.startswith("unique_"):
        kind = "uniqueness"
    elif name.startswith("not_null_"):
        kind = "not-null"
    elif name.startswith("accepted_values_"):
        kind = "accepted-values"
    elif name.startswith("relationships_"):
        kind = "relationships (foreign-key style)"
    else:
        kind = "schema"

    return (
        f"dbt test '{name}'. Validates {kind} constraint. "
        f"Test entity_id={entity_id}."
    )


def _populate_test_descriptions(
    registry: StoreRegistry,
    *,
    embedder: EmbedderClient,
    telemetry: _Telemetry,
) -> tuple[int, int]:
    """Fill in docs for test entities, then embed all docs in one batch.

    Returns ``(test_docs_added, total_docs_embedded)``.

    The dbt loader already indexed descriptions for entities that have
    one (5 models + 3 sources). Here we:
    1. Find all test nodes in the graph.
    2. Synthesize a description for each + write to the document store
       under ``doc:<entity_id>`` (matching the loader's id scheme).
    3. Read all docs (model + source + test) from the document store
       and embed them in a single batch upsert into the vector store.
    """
    graph = registry.knowledge.graph_store
    document_store = registry.knowledge.document_store
    vector_store = registry.knowledge.vector_store

    # Synthesize test descriptions and write them to the doc store.
    test_nodes = [
        n for n in graph.query(limit=5000) if n.get("node_type") == "dbt_test"
    ]
    test_docs_added = 0
    for node in test_nodes:
        entity_id = node["node_id"]
        description = _test_description(node)
        document_store.put(
            doc_id=f"doc:{entity_id}",
            content=description,
            metadata={
                "source": "dbt",
                "entity_id": entity_id,
                "entity_type": "dbt_test",
                "name": node.get("properties", {}).get("name", ""),
                "content_type": "entity_summary",
                "content_tags": {"signal_quality": "standard"},
                "content": description,
            },
        )
        test_docs_added += 1

    # Now embed every doc in the store (models + sources + tests).
    # We build the batch by listing every node in the graph and looking
    # up its ``doc:<entity_id>`` document. Skips nodes without a doc.
    all_nodes = graph.query(limit=5000)
    doc_ids: list[str] = []
    contents: list[str] = []
    for node in all_nodes:
        entity_id = node["node_id"]
        doc_id = f"doc:{entity_id}"
        doc = document_store.get(doc_id)
        if not doc:
            continue
        doc_ids.append(doc_id)
        contents.append(doc.get("content", ""))

    if not contents:
        return test_docs_added, 0

    async def _embed_all() -> list[list[float]]:
        started = time.monotonic()
        responses = await embedder.embed_batch(contents)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        head_usage = responses[0].usage if responses else None
        telemetry.record_embed(
            model=(responses[0].model if responses else None) or "unknown",
            in_tok=head_usage.prompt_tokens if head_usage else 0,
            latency_ms=elapsed_ms,
        )
        return [r.embedding for r in responses]

    vectors = asyncio.run(_embed_all())
    for doc_id, vec in zip(doc_ids, vectors, strict=True):
        # Re-fetch the doc to get fresh metadata for vector_store upsert.
        doc = document_store.get(doc_id)
        if not doc:
            continue
        meta = dict(doc.get("metadata") or {})
        meta["content"] = doc.get("content", "")  # for SemanticSearch excerpt
        vector_store.upsert(item_id=doc_id, vector=vec, metadata=meta)

    return test_docs_added, len(doc_ids)


# ---------------------------------------------------------------------------
# SeededGraphSearch — wraps GraphSearch so it returns empty when no
# seed_ids are present in the filters. Without this wrapper,
# GraphSearch's no-seeds fallback dumps every non-structural node into
# the pack (21 items in the dbt corpus), crowding out
# Keyword/SemanticSearch hits. The scenario only wants GraphSearch to
# fire when the round's intent successfully maps to one or more known
# entity short-names via :func:`extract_seed_ids`.
# ---------------------------------------------------------------------------


class _SeededGraphSearch(SearchStrategy):
    """Seeded GraphSearch with doc-prefixed item_ids for cross-strategy dedup.

    Two responsibilities, both small:

    1. **Seed gating.** Returns empty when ``filters["seed_ids"]`` is
       absent or empty — avoids GraphSearch's no-seeds fallback of
       returning every non-structural node, which floods the pack.

    2. **item_id canonicalization for dedup.** GraphSearch emits
       ``PackItem.item_id == node_id`` (e.g.,
       ``"model.jaffle_shop.customers"``). KeywordSearch and
       SemanticSearch emit ``PackItem.item_id == "doc:" + node_id``
       (e.g., ``"doc:model.jaffle_shop.customers"``). Without
       canonicalization, the same entity appears in the pack twice
       and PackBuilder's exact-match dedup keeps both — wasting
       budget on cross-strategy duplicates.

       This wrapper rewrites each GraphSearch PackItem's ``item_id``
       to ``"doc:" + node_id`` so PackBuilder's existing dedup
       collapses cross-strategy hits. The original ``node_id`` is
       preserved in ``metadata["graph_node_id"]`` for any downstream
       consumer that wants the raw form.
    """

    def __init__(self, graph_store: Any) -> None:
        self._inner = GraphSearch(graph_store)

    @property
    def name(self) -> str:
        return "graph_seeded"

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        if not filters:
            return []
        seed_ids = filters.get("seed_ids")
        if not seed_ids:
            return []
        items = self._inner.search(query, limit=limit, filters=filters)
        rewritten = []
        for item in items:
            if item.item_id.startswith("doc:"):
                rewritten.append(item)
                continue
            new_metadata = {
                **(item.metadata or {}),
                "graph_node_id": item.item_id,
            }
            rewritten.append(
                item.model_copy(
                    update={
                        "item_id": f"doc:{item.item_id}",
                        "metadata": new_metadata,
                    }
                )
            )
        return rewritten


# ---------------------------------------------------------------------------
# Per-round helpers — query selection and grading
# ---------------------------------------------------------------------------


def _round_query(round_index: int) -> JaffleShopQuery:
    return GROUND_TRUTH_QUERIES[round_index % len(GROUND_TRUTH_QUERIES)]


def _build_pack(
    builder: PackBuilder,
    query: JaffleShopQuery,
    *,
    name_index: dict[str, str],
) -> tuple[Pack, list[str]]:
    """Build a pack with seed_ids extracted from the intent.

    Returns ``(pack, seed_ids)``. ``seed_ids`` is the result of
    :func:`extract_seed_ids` against the round's query — surfaced so
    the round logger can record per-round seed counts and the report
    can aggregate seed-extraction success.
    """
    seed_ids = extract_seed_ids(query.intent, name_index)
    filters: dict[str, Any] = {}
    if seed_ids:
        filters["seed_ids"] = seed_ids
    pack = builder.build(
        intent=query.intent,
        domain=DBT_DOMAIN,
        budget=PackBudget(
            max_items=DEFAULT_PACK_MAX_ITEMS,
            max_tokens=DEFAULT_PACK_MAX_TOKENS,
        ),
        filters=filters or None,
        tag_filters={},
    )
    return pack, seed_ids


def _grade_round(
    pack: Pack,
    query: JaffleShopQuery,
    *,
    coverage_threshold: float,
) -> tuple[list[str], float, bool]:
    """Compute referenced items + coverage. Accepts BOTH id forms.

    For each required entity X:
    - ``f"doc:{X}"`` matches PackItems from KeywordSearch / SemanticSearch
    - ``X`` matches PackItems from GraphSearch (forward-compat)

    Either form counts as covering X. If both appear, only one is
    counted toward coverage to avoid double-credit.
    """
    pack_item_ids = {item.item_id for item in pack.items}
    referenced: list[str] = []
    for entity_id in query.required_coverage:
        doc_form = f"doc:{entity_id}"
        if doc_form in pack_item_ids or entity_id in pack_item_ids:
            referenced.append(doc_form if doc_form in pack_item_ids else entity_id)
    required_count = len(query.required_coverage)
    coverage = (
        1.0 if not required_count else len(referenced) / required_count
    )
    return referenced, coverage, coverage >= coverage_threshold


def _score_pack(pack: Pack, query: JaffleShopQuery) -> dict[str, float]:
    eval_scenario = EvaluationScenario(
        name=f"dbt_corpus_{query.skill}",
        intent=query.intent,
        domain=DBT_DOMAIN,
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
# Per-round bookkeeping (mirrors Phase A's shape)
# ---------------------------------------------------------------------------


@dataclass
class _RoundResult:
    round_index: int
    skill: str
    difficulty: str
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
    effectiveness_runs: int = 0
    noise_items_tagged_total: int = 0
    advisory_runs: int = 0
    advisories_generated_total: int = 0
    advisories_suppressed_total: int = 0
    advisories_restored_total: int = 0
    advisories_boosted_total: int = 0


def _record_round_feedback(
    *,
    feedback_log_dir: Path,
    registry: StoreRegistry,
    pack: Pack,
    query: JaffleShopQuery,
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
        intent_family=query.skill,  # use skill as intent_family for B-1
        agent_id="dbt_corpus_synthetic_agent",
    )
    record_feedback(
        feedback,
        log_dir=feedback_log_dir,
        event_log=registry.operational.event_log,
        pack_id=pack.pack_id,
    )


def _run_periodic_loops(
    *,
    registry: StoreRegistry,
    advisory_store: AdvisoryStore,
    stats: _LoopStats,
    generate_advisories: bool,
    advisory_min_sample_size: int = DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
) -> None:
    knowledge = registry.knowledge
    operational = registry.operational

    effectiveness = run_effectiveness_feedback(
        operational.event_log,
        knowledge.document_store,
    )
    stats.effectiveness_runs += 1
    stats.noise_items_tagged_total += len(effectiveness.noise_candidates)

    if generate_advisories:
        report = AdvisoryGenerator(
            operational.event_log,
            advisory_store,
            min_sample_size=advisory_min_sample_size,
        ).generate()
        stats.advisories_generated_total += report.advisories_generated
    stats.advisory_runs += 1

    fitness = run_advisory_fitness_loop(
        operational.event_log,
        advisory_store,
        min_presentations=2,
    )
    stats.advisories_boosted_total += len(fitness.advisories_boosted)
    stats.advisories_suppressed_total += len(fitness.advisories_suppressed)
    stats.advisories_restored_total += len(fitness.advisories_restored)


# ---------------------------------------------------------------------------
# Convergence math (same shape as Phase A)
# ---------------------------------------------------------------------------


def _quarter_means(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) < ROUND_WINDOW_FRACTION:
        full = statistics.fmean(values)
        return full, full
    window = max(1, len(values) // ROUND_WINDOW_FRACTION)
    return statistics.fmean(values[:window]), statistics.fmean(values[-window:])


def _convergence_stats(rounds: list[_RoundResult]) -> _ConvergenceStats:
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


# ---------------------------------------------------------------------------
# Metric aggregation
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
    metrics.update(_per_skill_round_metrics(rounds))
    metrics.update(_per_difficulty_round_metrics(rounds))
    return metrics


def _per_skill_round_metrics(rounds: list[_RoundResult]) -> dict[str, float]:
    by_skill: dict[str, list[_RoundResult]] = {}
    for r in rounds:
        by_skill.setdefault(r.skill, []).append(r)
    out: dict[str, float] = {}
    for skill, items in by_skill.items():
        out[f"per_skill.{skill}.success_rate"] = round(
            sum(1 for r in items if r.success) / len(items), 4
        )
        out[f"per_skill.{skill}.coverage_mean"] = round(
            statistics.fmean(r.coverage_fraction for r in items), 4
        )
    return out


def _per_difficulty_round_metrics(
    rounds: list[_RoundResult],
) -> dict[str, float]:
    by_diff: dict[str, list[_RoundResult]] = {}
    for r in rounds:
        by_diff.setdefault(r.difficulty, []).append(r)
    out: dict[str, float] = {}
    for diff, items in by_diff.items():
        out[f"per_difficulty.{diff}.success_rate"] = round(
            sum(1 for r in items if r.success) / len(items), 4
        )
        out[f"per_difficulty.{diff}.coverage_mean"] = round(
            statistics.fmean(r.coverage_fraction for r in items), 4
        )
    return out


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
        "convergence.useful_last_quarter_mean": round(c.useful_last_quarter_mean, 4),
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


def _make_embedding_fn(embedder: EmbedderClient, telemetry: _Telemetry) -> Any:
    """Sync ``callable(str) -> list[float]`` for SemanticSearch query embeds."""

    def embed_query(text: str) -> list[float]:
        async def _one() -> list[float]:
            started = time.monotonic()
            resp = await embedder.embed(text)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            usage = resp.usage
            telemetry.record_embed(
                model=resp.model or "unknown",
                in_tok=usage.prompt_tokens if usage else 0,
                latency_ms=elapsed_ms,
            )
            return list(resp.embedding)

        return asyncio.run(_one())

    return embed_query


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _validate(rounds: int, feedback_batch_size: int) -> None:
    if rounds <= 0:
        msg = "rounds must be positive"
        raise ValueError(msg)
    if feedback_batch_size <= 0:
        msg = "feedback_batch_size must be positive"
        raise ValueError(msg)


def run(  # noqa: PLR0915 — orchestrates many stages, single coherent run flow
    registry: StoreRegistry,
    *,
    seed: int = 0,
    rounds: int = DEFAULT_ROUNDS,
    feedback_batch_size: int = DEFAULT_FEEDBACK_BATCH_SIZE,
    success_coverage_threshold: float = DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    convergence_delta_regress_threshold: float = (CONVERGENCE_DELTA_REGRESS_THRESHOLD),
    advisory_min_sample_size: int = DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    manifest_path: Path | None = None,
    enable_graph_search: bool = True,
) -> ScenarioReport:
    _validate(rounds, feedback_batch_size)

    telemetry = _Telemetry()
    # Provider factory builds both clients. We only use the embedder.
    _, embedder, llm_config = build_phase_a_clients()
    del _  # explicit: chat client unused for B-1

    findings: list[Finding] = []
    metrics: dict[str, float] = {
        "rounds": float(rounds),
        "feedback_batch_size": float(feedback_batch_size),
        "queries_in_corpus": float(len(GROUND_TRUTH_QUERIES)),
    }

    # Load corpus (entities + edges + per-entity descriptions for
    # models/sources). Returns counts surfaced as `corpus.*` metrics.
    if manifest_path is not None:
        load_result: LoadResult = load_jaffle_shop_corpus(
            registry, manifest_path=manifest_path
        )
    else:
        load_result = load_jaffle_shop_corpus(registry)
    metrics.update(load_result.as_metrics(prefix="corpus"))

    # Synthesize test descriptions + embed every doc.
    test_docs_added, total_docs_embedded = _populate_test_descriptions(
        registry, embedder=embedder, telemetry=telemetry
    )
    metrics["corpus.test_docs_synthesized"] = float(test_docs_added)
    metrics["corpus.total_docs_embedded"] = float(total_docs_embedded)

    # Cost guard before round loop.
    if telemetry.total_cost_usd() > RUN_HARD_COST_CAP_USD:
        findings.append(
            Finding(
                severity="fail",
                message=(
                    f"Setup cost ${telemetry.total_cost_usd():.4f} exceeded "
                    f"hard cap ${RUN_HARD_COST_CAP_USD:.2f}"
                ),
            )
        )
        metrics.update(telemetry.to_metrics())
        return ScenarioReport(
            name="dbt_corpus_convergence",
            status="fail",
            metrics=metrics,
            findings=findings,
            decision="Hard cost cap tripped during setup.",
        )

    feedback_dir_holder = tempfile.TemporaryDirectory()
    feedback_dir = Path(feedback_dir_holder.name)
    advisory_dir_root = registry.stores_dir or feedback_dir
    advisory_store = AdvisoryStore(advisory_dir_root / "advisories.json")

    embed_fn = _make_embedding_fn(embedder, telemetry)
    name_index = build_name_index(registry)
    metrics["corpus.name_index_size"] = float(len(name_index))
    metrics["config.enable_graph_search"] = 1.0 if enable_graph_search else 0.0
    strategies: list[SearchStrategy] = [
        KeywordSearch(registry.knowledge.document_store),
        SemanticSearch(registry.knowledge.vector_store, embed_fn),
    ]
    if enable_graph_search:
        strategies.append(_SeededGraphSearch(registry.knowledge.graph_store))
    builder = PackBuilder(
        strategies=strategies,
        event_log=registry.operational.event_log,
        advisory_store=advisory_store,
    )

    loop_stats = _LoopStats()
    round_results: list[_RoundResult] = []
    seed_extraction_hits = 0  # rounds where seed_ids was non-empty
    seed_ids_grand_total = 0  # cumulative seed_id count across all rounds
    run_id = f"dbt_corpus_{seed:04d}"

    try:
        for round_index in range(rounds):
            query = _round_query(round_index)
            pack, round_seed_ids = _build_pack(
                builder, query, name_index=name_index
            )
            if round_seed_ids:
                seed_extraction_hits += 1
                seed_ids_grand_total += len(round_seed_ids)
            referenced, coverage, success = _grade_round(
                pack, query, coverage_threshold=success_coverage_threshold
            )
            scores = _score_pack(pack, query)
            round_results.append(
                _RoundResult(
                    round_index=round_index,
                    skill=query.skill,
                    difficulty=query.difficulty,
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
                    generate_advisories=loop_stats.advisory_runs == 0,
                    advisory_min_sample_size=advisory_min_sample_size,
                )

            if telemetry.total_cost_usd() > RUN_HARD_COST_CAP_USD:
                findings.append(
                    Finding(
                        severity="fail",
                        message=(
                            f"Round {round_index} tripped hard cost cap "
                            f"${RUN_HARD_COST_CAP_USD:.2f}"
                        ),
                    )
                )
                break
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
    metrics.update(_round_metrics(round_results))
    metrics.update(_loop_metrics(loop_stats))
    metrics.update(_convergence_metrics(convergence))
    metrics.update(telemetry.to_metrics())
    if rounds > 0:
        metrics["seed_extraction.rounds_with_seeds"] = float(seed_extraction_hits)
        metrics["seed_extraction.hit_rate"] = round(
            seed_extraction_hits / rounds, 4
        )
        metrics["seed_extraction.seeds_total"] = float(seed_ids_grand_total)
        metrics["seed_extraction.seeds_per_round_mean"] = round(
            seed_ids_grand_total / rounds, 4
        )

    findings.append(
        Finding(
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
                "useful_delta": round(convergence.useful_delta, 4),
            },
        )
    )
    findings.append(
        Finding(
            severity="info",
            message=(
                f"loops fired: {loop_stats.effectiveness_runs} effectiveness, "
                f"{loop_stats.advisory_runs} advisory; "
                f"noise tags applied: {loop_stats.noise_items_tagged_total}; "
                f"advisories generated {loop_stats.advisories_generated_total}, "
                f"suppressed {loop_stats.advisories_suppressed_total}, "
                f"restored {loop_stats.advisories_restored_total}, "
                f"boosted {loop_stats.advisories_boosted_total}"
            ),
        )
    )
    findings.append(
        Finding(
            severity="info",
            message=(
                f"providers — chat: unused; "
                f"embed: {llm_config.openai_embedding_model} "
                f"({llm_config.openai_embedding_dim}-dim, "
                f"{len(telemetry.embed_calls)} calls)"
            ),
            detail={"cost_total_usd": round(telemetry.total_cost_usd(), 6)},
        )
    )

    status: ScenarioStatus
    if any(f.severity == "fail" for f in findings):
        status = "fail"
    elif convergence.useful_delta < convergence_delta_regress_threshold:
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
        "Phase B-1 dbt corpus convergence run completed. "
        f"Cost: ${telemetry.total_cost_usd():.4f} "
        f"({len(telemetry.embed_calls)} embedder calls). "
        "Per-skill and per-difficulty breakdowns surface in the "
        "metrics block; check per_skill.* and per_difficulty.* for "
        "where the loop is or isn't improving. See "
        "docs/design/plan-real-corpus-eval.md §5.2."
    )

    return ScenarioReport(
        name="dbt_corpus_convergence",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )
