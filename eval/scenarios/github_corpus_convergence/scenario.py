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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from eval._real_llm import build_phase_a_clients
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
from trellis.retrieve.semantic_seeds import SemanticSeedExtractor
from trellis.retrieve.strategies import (
    KeywordSearch,
    SearchStrategy,
    SemanticSearch,
)
from trellis.schemas.pack import Pack, PackBudget
from trellis.schemas.well_known import WAS_ATTRIBUTED_TO
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

RUN_HARD_COST_CAP_USD = 1.00


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


#: Hard cap on total seed count after literal + semantic union (SEM-1).
#:
#: Each seed anchors a depth=2 GraphSearch subgraph, and on the github
#: corpus's cross-reference density (~250 ``wasInformedBy`` edges
#: across 88 PRs) more than ~4-5 seeds expand a combined subgraph that
#: exceeds the 8-item pack budget and displaces the right answers
#: with structurally-adjacent neighbors. Tuned at 4 to leave room
#: for a 1-3 PR literal hit plus 1-3 semantic completions for
#: paraphrased intents — small enough to stay under the budget after
#: depth-2 fan-out, large enough to cover the multi_pr_series Q1
#: shape (4 PRs in a series, of which the literal extractor finds 0-1).
#:
#: This cap is the no-regression posture for SEM-1: it bounds the
#: subgraph size so the literal path's high-precision answers (e.g.,
#: cross_pr_lineage's ``"PR #66"``) keep their cited PRs in scope
#: even when 1-2 semantic top-K hits are off-target. See the proxy
#: test's regression cases for the boundary measurements.
SEM1_MAX_TOTAL_SEEDS = 4


def _build_pack(
    builder: PackBuilder,
    query: GitHubPRQuery,
    *,
    name_index: dict[str, str],
    semantic_seed_extractor: SemanticSeedExtractor | None = None,
) -> tuple[Pack, list[str]]:
    """Build a pack with literal + (optional) semantic seed extraction.

    Seed-source composition (SEM-1):

    * :func:`extract_seed_ids` — literal short-name / unique-phrase
      matches from the loader's PR-name index. Catches intents that
      reference a PR by ``#NNN``, login, or unique title phrase.
      Always runs first.
    * :meth:`SemanticSeedExtractor.extract` (when an extractor is
      supplied) — embedding-based top-K against entity-summary docs.
      Catches paraphrased intents that no literal index entry matches
      (e.g., "Phase 1 through Phase 4 PRs that shipped scenarios 5.1,
      5.2, 5.3" — no PR title literally contains "Phase 1 through
      Phase 4"). Always runs, but bounded — see
      :data:`SEM1_MAX_TOTAL_SEEDS`.

    Composition rule:

    * Literal seeds keep priority — they fill the seed list first.
    * Semantic seeds fill any remaining slots up to the
      :data:`SEM1_MAX_TOTAL_SEEDS` cap, in similarity-rank order.

    A query like cross_pr_lineage (literal returns 1 high-confidence
    ``"#66"``) admits up to ``SEM1_MAX_TOTAL_SEEDS - 1`` semantic
    additions; multi_pr_series Q1 (literal returns 0 or 1 noise hit)
    gets the remaining ``SEM1_MAX_TOTAL_SEEDS`` from semantic. The
    cap keeps the depth=2 expanded subgraph compact enough to fit
    the pack budget after dedup and ranking — without the cap, the
    semantic path's 5-10 seeds inflate the subgraph past the 8-item
    pack budget and the literal path's right answers get displaced
    by structurally-adjacent neighbors.

    Negation handling (:func:`_drop_negated_user_seeds`) runs over
    the final seed set so a semantic hit on an excluded user is also
    dropped.

    The user-attribution edge-type narrowing only triggers when
    *every* surviving seed is a user entity — semantic-seed
    contributions are PR entities and so naturally widen out of that
    code path (which is the right behavior; that branch is for the
    pure "what did user X author" intent shape).
    """
    seed_ids = extract_seed_ids(query.intent, name_index)
    if semantic_seed_extractor is not None and len(seed_ids) < SEM1_MAX_TOTAL_SEEDS:
        # Bounded composition: semantic adds enough to bring the union
        # to the cap, no more. See SEM1_MAX_TOTAL_SEEDS for the cap
        # rationale.
        remaining = SEM1_MAX_TOTAL_SEEDS - len(seed_ids)
        semantic_seeds = semantic_seed_extractor.extract(query.intent)
        seen = set(seed_ids)
        for sid in semantic_seeds:
            if remaining <= 0:
                break
            if sid not in seen:
                seed_ids.append(sid)
                seen.add(sid)
                remaining -= 1
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
    coverage = 1.0 if not required_count else len(referenced) / required_count
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
# Setup — embed all PR documents in a single batch
# ---------------------------------------------------------------------------


def _embed_corpus_documents(
    registry: StoreRegistry,
    *,
    embedder: EmbedderClient,
    telemetry: _EmbedTelemetry,
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
# Round bookkeeping — _RoundResult is local because the discriminator is
# ``skill`` + ``difficulty`` (same as dbt, but defined here to avoid
# scenario-to-scenario coupling).
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
# Per-skill / per-difficulty metrics
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


def run(  # noqa: PLR0912, PLR0915 — orchestrates many stages, single coherent flow
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
    _validate_basic_kwargs(rounds=rounds, feedback_batch_size=feedback_batch_size)
    del seed  # unused — corpus is deterministic

    telemetry = _EmbedTelemetry()
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
    # SEM-1: semantic-seed extraction for paraphrased intents that the
    # literal extract_seed_ids cannot reach (e.g., multi_pr_series Q1).
    # Only enabled when GraphSearch is enabled — without it the seeds
    # have nowhere to go (KeywordSearch + SemanticSearch already run on
    # the raw intent). The cache is sized for one round per query.
    semantic_seed_extractor: SemanticSeedExtractor | None = None
    if enable_graph_search:
        semantic_seed_extractor = SemanticSeedExtractor(
            registry.knowledge.vector_store,
            embed_fn,
            cache_size=max(len(GROUND_TRUTH_QUERIES) * 2, 32),
        )
    metrics["config.semantic_seed_extraction"] = (
        1.0 if semantic_seed_extractor is not None else 0.0
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
                builder,
                query,
                name_index=name_index,
                semantic_seed_extractor=semantic_seed_extractor,
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
                agent_id="github_corpus_synthetic_agent",
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
