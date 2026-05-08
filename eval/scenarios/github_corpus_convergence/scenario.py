"""GitHub PR corpus convergence — Phase B-2.

Mirrors ``dbt_corpus_convergence`` in shape and architecture; only the
corpus, queries, and seed-name source differ:

- Corpus: trellis-ai PR snapshot (88 merged PRs, 2 users, 250 edges).
- Queries: 12 hand-authored against the actual PR history.
- Seed-name index: ``#NNN`` and bare ``NNN`` forms for PRs, login for
  users — produced by :func:`eval.corpora.github_trellis.loader.build_pr_name_index`.
- Author-attribution query has its required_coverage filled in
  dynamically at startup based on the dependabot-authored PRs in the
  loaded snapshot.

Same retrieval strategies (KeywordSearch + SemanticSearch +
SeededGraphSearch), same canonical-id dedup wrapper, same dual-loop
firing schedule. The github corpus is larger (~90 entities + 250
edges vs. dbt's 21 + 22) so per-round retrieval has more candidates
to rank.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from eval._real_llm import (
    OPENAI_EMBED_3_SMALL_USD_PER_M,
    build_phase_a_clients,
)
from eval.corpora.dbt_loader import extract_seed_ids
from eval.corpora.github_trellis.loader import (
    GitHubLoadResult,
    build_pr_name_index,
    load_github_corpus,
)
from eval.corpora.github_trellis.queries import (
    GITHUB_DOMAIN,
    GROUND_TRUTH_QUERIES,
    GitHubPRQuery,
    materialize_dependabot_query_coverage,
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
from trellis.schemas.well_known import WAS_ATTRIBUTED_TO
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

# OPENAI_EMBED_3_SMALL_USD_PER_M re-exported from eval._real_llm.
RUN_HARD_COST_CAP_USD = 1.00


# ---------------------------------------------------------------------------
# SeededGraphSearch — copy of the dbt scenario's wrapper. Same two
# responsibilities: seed gating + canonical doc-prefixed item_ids.
# Kept here rather than imported from the dbt scenario to avoid a
# scenario-to-scenario coupling.
# ---------------------------------------------------------------------------


class _SeededGraphSearch(SearchStrategy):
    """Seeded GraphSearch with doc-prefixed item_ids for cross-strategy dedup."""

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
# Telemetry — embeddings only (no chat), same shape as Phase B-1.
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
# Setup — embed all PR documents in a single batch
# ---------------------------------------------------------------------------


def _embed_corpus_documents(
    registry: StoreRegistry,
    *,
    embedder: EmbedderClient,
    telemetry: _Telemetry,
) -> int:
    """Embed every indexed document and upsert to the vector store.

    Reads doc_ids from the graph (each PR has a doc:<entity_id> entry
    indexed by the loader). Skips users (they have no doc).

    Returns the count of documents embedded.
    """
    graph = registry.knowledge.graph_store
    document_store = registry.knowledge.document_store
    vector_store = registry.knowledge.vector_store

    doc_ids: list[str] = []
    contents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for node in graph.query(limit=5000):
        entity_id = node["node_id"]
        doc_id = f"doc:{entity_id}"
        doc = document_store.get(doc_id)
        if not doc:
            continue
        doc_ids.append(doc_id)
        contents.append(doc.get("content", ""))
        meta = dict(doc.get("metadata") or {})
        meta["content"] = doc.get("content", "")
        metadatas.append(meta)

    if not contents:
        return 0

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
    for doc_id, vec, meta in zip(doc_ids, vectors, metadatas, strict=True):
        vector_store.upsert(item_id=doc_id, vector=vec, metadata=meta)
    return len(doc_ids)


# ---------------------------------------------------------------------------
# Per-round helpers — query selection, pack build, grading
# ---------------------------------------------------------------------------


def _round_query(round_index: int) -> GitHubPRQuery:
    return GROUND_TRUTH_QUERIES[round_index % len(GROUND_TRUTH_QUERIES)]


_USER_ENTITY_PREFIX = "github.user."

# Phrases that precede a user mention to negate it ("not by ronsse",
# "except dependabot", "without app/foo"). Used to drop user seeds the
# intent is asking to *exclude* rather than retrieve.
_NEGATION_BEFORE_USER = re.compile(
    r"\b(?:not\s+by|except\s+(?:by\s+)?|excluding|without|other\s+than)\s+"
    r"([A-Za-z0-9_\-/.]+)",
    flags=re.IGNORECASE,
)


def _drop_negated_user_seeds(
    seed_ids: list[str], intent: str, name_index: dict[str, str]
) -> list[str]:
    """Remove user seeds the intent is asking to exclude.

    For an intent like ``"PRs authored by app/dependabot, not by ronsse"``
    the seed extractor pulls both user entity_ids. Without this filter,
    a depth-1 ``wasAttributedTo`` traversal returns *every* PR (since
    every PR is attributed to one of the two users). Dropping the
    negated user collapses the subgraph to just the requested set.
    """
    negated_ids: set[str] = set()
    for match in _NEGATION_BEFORE_USER.finditer(intent):
        # The capture-group character class permits ``.`` and ``/`` so logins
        # like ``app/dependabot`` survive intact; that means trailing
        # punctuation (``"not by ronsse."``) also gets captured. Strip
        # sentence-ending punctuation before the index lookup.
        login = match.group(1).lower().rstrip(".,;!?")
        if login in name_index:
            negated_ids.add(name_index[login])
    return [s for s in seed_ids if s not in negated_ids]


def _build_pack(
    builder: PackBuilder,
    query: GitHubPRQuery,
    *,
    name_index: dict[str, str],
) -> tuple[Pack, list[str]]:
    seed_ids = extract_seed_ids(query.intent, name_index)
    seed_ids = _drop_negated_user_seeds(seed_ids, query.intent, name_index)
    filters: dict[str, Any] = {}
    if seed_ids:
        filters["seed_ids"] = seed_ids
        # Attribute-filter shape: when ALL seeds resolve to user entities,
        # the intent is asking "what PRs did this user author" rather than
        # "find related content near these seeds". Constrain GraphSearch to
        # depth=1 along ``wasAttributedTo`` so the subgraph contains only
        # the user(s) plus the PRs attributed to them, not 2-hop neighbors
        # via cross-references.
        if all(s.startswith(_USER_ENTITY_PREFIX) for s in seed_ids):
            filters["edge_types"] = [WAS_ATTRIBUTED_TO]
            filters["depth"] = 1
    pack = builder.build(
        intent=query.intent,
        domain=GITHUB_DOMAIN,
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
    query: GitHubPRQuery,
    *,
    coverage_threshold: float,
) -> tuple[list[str], float, bool]:
    """Coverage check. Accepts both ``doc:X`` and ``X`` item_id forms.

    Empty ``required_coverage`` is treated as a degenerate-but-passing
    case (coverage=1.0, success=True) — matches the dbt scenario's
    ``not required`` short-circuit.
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


def _score_pack(pack: Pack, query: GitHubPRQuery) -> dict[str, float]:
    eval_scenario = EvaluationScenario(
        name=f"github_corpus_{query.skill}",
        intent=query.intent,
        domain=GITHUB_DOMAIN,
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
# Round bookkeeping (mirrors Phase B-1)
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
    query: GitHubPRQuery,
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
        intent_family=query.skill,
        agent_id="github_corpus_synthetic_agent",
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
# Convergence math
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
# Metric aggregation (per-skill + per-difficulty)
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


def _read_snapshot_authors(snapshot_path: Path) -> dict[int, str]:
    """Parse PR-number → author-login map from the raw snapshot file."""
    raw = snapshot_path.read_text(encoding="utf-8")
    snapshot = json.loads(raw)
    result: dict[int, str] = {}
    for pr in snapshot:
        author = pr.get("author") or {}
        login = author.get("login", "")
        if login:
            result[pr["number"]] = login
    return result


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


def run(  # noqa: PLR0915 — orchestrates many stages, single coherent flow
    registry: StoreRegistry,
    *,
    seed: int = 0,
    rounds: int = DEFAULT_ROUNDS,
    feedback_batch_size: int = DEFAULT_FEEDBACK_BATCH_SIZE,
    success_coverage_threshold: float = DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    convergence_delta_regress_threshold: float = (CONVERGENCE_DELTA_REGRESS_THRESHOLD),
    advisory_min_sample_size: int = DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    snapshot_path: Path | None = None,
    enable_graph_search: bool = True,
) -> ScenarioReport:
    _validate(rounds, feedback_batch_size)
    del seed  # unused — corpus is deterministic

    telemetry = _Telemetry()
    _, embedder, llm_config = build_phase_a_clients()

    findings: list[Finding] = []
    metrics: dict[str, float] = {
        "rounds": float(rounds),
        "feedback_batch_size": float(feedback_batch_size),
    }

    # Load corpus.
    if snapshot_path is not None:
        load_result: GitHubLoadResult = load_github_corpus(
            registry, snapshot_path=snapshot_path
        )
    else:
        load_result = load_github_corpus(registry)
    metrics.update(load_result.as_metrics(prefix="corpus"))

    # Materialize the dependabot author-attribution query coverage.
    snap_path = snapshot_path or Path(__file__).parent.parent.parent / (
        "corpora/github_trellis/snapshot_raw.json"
    )
    if snap_path.exists():
        materialize_dependabot_query_coverage(_read_snapshot_authors(snap_path))

    metrics["queries_in_corpus"] = float(len(GROUND_TRUTH_QUERIES))

    # Embed all PR docs in one batch.
    docs_embedded = _embed_corpus_documents(
        registry, embedder=embedder, telemetry=telemetry
    )
    metrics["corpus.docs_embedded"] = float(docs_embedded)

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
            name="github_corpus_convergence",
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
    name_index = build_pr_name_index(registry)
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
    seed_extraction_hits = 0
    seed_ids_grand_total = 0
    run_id = "github_corpus"

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
                f"providers — chat: unused; embed: "
                f"{llm_config.openai_embedding_model} "
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
        "Phase B-2 GitHub PR corpus convergence run completed. "
        f"Cost: ${telemetry.total_cost_usd():.4f} "
        f"({len(telemetry.embed_calls)} embedder calls). "
        "Per-skill / per-difficulty breakdowns are in the metrics block. "
        "See docs/design/plan-real-corpus-eval.md §5.3."
    )

    return ScenarioReport(
        name="github_corpus_convergence",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )
