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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from eval._real_llm import build_phase_a_clients
from eval.corpora.dbt_loader import (
    LoadResult,
    build_category_index,
    build_lineage_index,
    build_name_index,
    expand_seeds_with_lineage,
    extract_category_seeds,
    extract_seed_ids,
    load_jaffle_shop_corpus,
)
from eval.corpora.jaffle_shop.queries import (
    DBT_DOMAIN,
    GROUND_TRUTH_QUERIES,
    JaffleShopQuery,
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
    _loop_metrics,
    _loops_summary_finding,
    _LoopStats,
    _record_round_feedback,
    _run_periodic_loops,
    _validate_basic_kwargs,
)
from eval.scenarios._strategies import _SeededGraphSearch
from eval.scenarios._telemetry import _EmbedTelemetry, _make_embedding_fn
from trellis.llm.protocol import EmbedderClient
from trellis.retrieve.evaluate import (
    BUILTIN_PROFILES,
    EvaluationScenario,
    evaluate_pack,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import (
    KeywordSearch,
    SearchStrategy,
    SemanticSearch,
)
from trellis.schemas.pack import Pack, PackBudget
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

# Embeddings-only scenario — tighter than Phase A's $1 cap since there's
# no chat surface to inflate cost.
RUN_HARD_COST_CAP_USD = 0.50


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
        f"dbt test '{name}'. Validates {kind} constraint. Test entity_id={entity_id}."
    )


def _populate_test_descriptions(
    registry: StoreRegistry,
    *,
    embedder: EmbedderClient,
    telemetry: _EmbedTelemetry,
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
# Per-round helpers — query selection, pack build, grading
# ---------------------------------------------------------------------------


def _round_query(round_index: int) -> JaffleShopQuery:
    return GROUND_TRUTH_QUERIES[round_index % len(GROUND_TRUTH_QUERIES)]


def _build_pack(
    builder: PackBuilder,
    query: JaffleShopQuery,
    *,
    name_index: dict[str, str],
    category_index: dict[str, list[str]],
    lineage_index: dict[str, list[str]],
) -> tuple[Pack, list[str]]:
    """Build a pack with seed_ids extracted from the intent.

    Returns ``(pack, seed_ids)``. ``seed_ids`` is the union of:

    * :func:`extract_seed_ids` — short-name resolution
      (``customers`` → ``model.jaffle_shop.customers``)
    * :func:`expand_seeds_with_lineage` — when the intent contains
      a lineage keyword (``upstream``, ``lineage``, ``ancestors``,
      ``dependencies``), each name seed is expanded with its
      precomputed transitive ``dependsOn`` ancestors.
    * :func:`extract_category_seeds` — category-phrase resolution
      (``mart-layer models`` → set of ``schema='marts'`` entities,
      ``not null`` → set of ``not_null_*`` test entities)

    When category seeds OR lineage expansion contributed, GraphSearch
    runs with ``depth=0`` so the matched entities become first-class
    pack candidates without traversal pulling in their structural
    neighbors (which would otherwise displace the right answers
    under the 8-item budget).
    """
    name_seeds = extract_seed_ids(query.intent, name_index)
    expanded_seeds = expand_seeds_with_lineage(name_seeds, query.intent, lineage_index)
    lineage_expanded = len(expanded_seeds) > len(name_seeds)
    category_seeds = extract_category_seeds(query.intent, category_index)
    seed_ids = list(dict.fromkeys(expanded_seeds + category_seeds))
    filters: dict[str, Any] = {}
    if seed_ids:
        filters["seed_ids"] = seed_ids
        if category_seeds or lineage_expanded:
            # Category / lineage seeds are *the answer*, not a starting
            # point for traversal. depth=0 returns just the seeds.
            filters["depth"] = 0
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
    coverage = 1.0 if not required_count else len(referenced) / required_count
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
# Per-round bookkeeping — _RoundResult is local because the discriminator
# is ``skill`` + ``difficulty`` here (vs ``domain`` in agent_loop).
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


# ---------------------------------------------------------------------------
# Per-skill / per-difficulty metrics — discriminator is scenario-specific.
# ---------------------------------------------------------------------------


def _round_metrics(rounds: list[_RoundResult]) -> dict[str, float]:
    metrics = _base_round_metrics(rounds)
    if metrics:
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(  # noqa: PLR0915,PLR0912 — orchestrates many stages, single coherent run flow
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
    _validate_basic_kwargs(rounds=rounds, feedback_batch_size=feedback_batch_size)

    telemetry = _EmbedTelemetry()
    # Provider factory builds both clients; only the embedder is used here.
    _, embedder, llm_config = build_phase_a_clients()

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
    category_index = build_category_index(registry)
    lineage_index = build_lineage_index(registry)
    metrics["corpus.name_index_size"] = float(len(name_index))
    metrics["corpus.category_index_size"] = float(len(category_index))
    metrics["corpus.lineage_index_size"] = float(len(lineage_index))
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
                builder,
                query,
                name_index=name_index,
                category_index=category_index,
                lineage_index=lineage_index,
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
                intent=query.intent,
                intent_family=query.skill,
                referenced=referenced,
                success=success,
                round_index=round_index,
                run_id=run_id,
                agent_id="dbt_corpus_synthetic_agent",
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
        metrics["seed_extraction.hit_rate"] = round(seed_extraction_hits / rounds, 4)
        metrics["seed_extraction.seeds_total"] = float(seed_ids_grand_total)
        metrics["seed_extraction.seeds_per_round_mean"] = round(
            seed_ids_grand_total / rounds, 4
        )

    findings.append(_convergence_summary_finding(convergence))
    findings.append(_loops_summary_finding(loop_stats))
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
