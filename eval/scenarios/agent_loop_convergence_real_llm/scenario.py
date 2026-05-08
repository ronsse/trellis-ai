"""Agent-loop convergence with a real LLM + real embedder (Phase A).

Fork of :mod:`eval.scenarios.agent_loop_convergence.scenario` that
swaps three things while keeping the agent-loop math identical:

1. Entity-summary docs are LLM-generated (Moonshot/Kimi) instead of
   hand-formatted. One call per entity at setup time.
2. Every doc (entity summary + distractor) is embedded via the OpenAI
   embedder and upserted into the registry's vector store.
3. ``PackBuilder`` strategies become ``[KeywordSearch, SemanticSearch]``
   instead of keyword-only — the vector path is now exercised.

Telemetry: a thin wrapper class records every chat / embed call and
totals tokens. Costs are computed at run-end using known per-M-token
rates for ``kimi-k2-0905-preview`` and ``text-embedding-3-small``.

The synthetic baseline scenario stays unchanged so per-seed diffs
against it remain meaningful.

See README.md for what this exercises and what it deliberately defers.
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

from eval._real_llm import (
    OPENAI_EMBED_3_SMALL_USD_PER_M,
    build_phase_a_clients,
)
from eval.generators.trace_generator import (
    EvalQuery,
    GeneratedCorpus,
    GeneratedTrace,
    generate_corpus,
)
from eval.runner import Finding, ScenarioReport, ScenarioStatus

# Reuse the original scenario's helpers — these are stable, well-tested,
# and the fork shouldn't redefine them. Underscore-leading is fine
# inside the project; this is a deliberate intra-project import not a
# public API consumption.
from eval.scenarios.agent_loop_convergence.scenario import (
    _DISTRACTOR_DOCS,
    CONVERGENCE_DELTA_REGRESS_THRESHOLD,
    DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    DEFAULT_ENTITIES_PER_TRACE,
    DEFAULT_FEEDBACK_BATCH_SIZE,
    DEFAULT_PACK_MAX_ITEMS,
    DEFAULT_PACK_MAX_TOKENS,
    DEFAULT_REGIME_SHIFT_REPLACEMENT_COUNT,
    DEFAULT_ROUNDS,
    DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    DEFAULT_TRACES_PER_DOMAIN,
    _convergence_findings,
    _convergence_metrics,
    _convergence_stats,
    _grade_round,
    _ingest_traces,
    _loop_metrics,
    _LoopStats,
    _record_round_feedback,
    _round_metrics,
    _round_query,
    _RoundResult,
    _run_periodic_loops,
    _score_pack,
    _validate_run_kwargs,
)
from trellis.llm.protocol import EmbedderClient, LLMClient
from trellis.llm.types import Message
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import KeywordSearch, SemanticSearch
from trellis.schemas.pack import Pack, PackBudget
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pricing — per million tokens, USD. Updated 2026-05-06.
# ---------------------------------------------------------------------------
# kimi-k2-0905-preview: published rate at moonshot.ai
# text-embedding-3-small: published rate at openai.com/pricing
# These are hardcoded for transparency. Update with explicit commits when
# pricing changes; the report metric `cost.total_usd` will silently drift
# otherwise.

KIMI_K2_INPUT_USD_PER_M = 0.60
KIMI_K2_OUTPUT_USD_PER_M = 2.50
# OPENAI_EMBED_3_SMALL_USD_PER_M re-exported from eval._real_llm; imported
# above so cost telemetry tracks one source of truth across scenarios.

# Hard safety cap — abort if a single run blows past this. Phase A's expected
# cost per run is ~$0.01; a 100x ceiling buys safety against pricing
# regressions or runaway loops without requiring tight budget control.
RUN_HARD_COST_CAP_USD = 1.00


# ---------------------------------------------------------------------------
# Telemetry — wraps LLM/embedder calls and aggregates usage
# ---------------------------------------------------------------------------


@dataclass
class _CallRecord:
    """Single call to an LLM or embedder surface."""

    surface: str  # "chat" | "embed"
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


@dataclass
class _Telemetry:
    """Accumulator for chat + embedder telemetry across a scenario run."""

    calls: list[_CallRecord] = field(default_factory=list)

    def record_chat(self, model: str, in_tok: int, out_tok: int, latency_ms: int) -> None:
        self.calls.append(_CallRecord("chat", model, in_tok, out_tok, latency_ms))

    def record_embed(self, model: str, in_tok: int, latency_ms: int) -> None:
        self.calls.append(_CallRecord("embed", model, in_tok, 0, latency_ms))

    def chat_calls(self) -> list[_CallRecord]:
        return [c for c in self.calls if c.surface == "chat"]

    def embed_calls(self) -> list[_CallRecord]:
        return [c for c in self.calls if c.surface == "embed"]

    def chat_cost_usd(self) -> float:
        in_tok = sum(c.input_tokens for c in self.chat_calls())
        out_tok = sum(c.output_tokens for c in self.chat_calls())
        return (in_tok / 1e6) * KIMI_K2_INPUT_USD_PER_M + (
            out_tok / 1e6
        ) * KIMI_K2_OUTPUT_USD_PER_M

    def embed_cost_usd(self) -> float:
        in_tok = sum(c.input_tokens for c in self.embed_calls())
        return (in_tok / 1e6) * OPENAI_EMBED_3_SMALL_USD_PER_M

    def total_cost_usd(self) -> float:
        return self.chat_cost_usd() + self.embed_cost_usd()

    def to_metrics(self) -> dict[str, float]:
        chat_calls = self.chat_calls()
        embed_calls = self.embed_calls()
        chat_lat = [c.latency_ms for c in chat_calls]
        embed_lat = [c.latency_ms for c in embed_calls]
        return {
            "llm.calls_total": float(len(chat_calls)),
            "llm.input_tokens_total": float(
                sum(c.input_tokens for c in chat_calls)
            ),
            "llm.output_tokens_total": float(
                sum(c.output_tokens for c in chat_calls)
            ),
            "embedder.calls_total": float(len(embed_calls)),
            "embedder.input_tokens_total": float(
                sum(c.input_tokens for c in embed_calls)
            ),
            "cost.chat_usd": round(self.chat_cost_usd(), 6),
            "cost.embed_usd": round(self.embed_cost_usd(), 6),
            "cost.total_usd": round(self.total_cost_usd(), 6),
            "latency.chat_ms_p50": (
                round(statistics.median(chat_lat), 1) if chat_lat else 0.0
            ),
            "latency.chat_ms_max": float(max(chat_lat) if chat_lat else 0),
            "latency.embed_ms_p50": (
                round(statistics.median(embed_lat), 1) if embed_lat else 0.0
            ),
            "latency.embed_ms_max": float(max(embed_lat) if embed_lat else 0),
        }


# ---------------------------------------------------------------------------
# LLM-driven summary generation
# ---------------------------------------------------------------------------


_SUMMARY_SYSTEM_PROMPT = (
    "You are a documentation assistant generating concise entity "
    "summaries for a software-engineering knowledge graph. Each summary "
    "must be one paragraph, 2-3 sentences, factual, and mention the "
    "entity name explicitly. No bullet points, no headers, no markdown."
)


def _build_summary_prompt(entity: str, domain: str, intents: list[str]) -> list[Message]:
    """Build the messages list for one entity-summary generation."""
    sample_intents = "; ".join(intents[:5]) if intents else "(no sampled intents)"
    user = (
        f"Entity name: {entity}\n"
        f"Domain: {domain}\n"
        f"Sample intents that touched this entity: {sample_intents}\n\n"
        f"Write a 2-3 sentence summary describing what {entity} is and how "
        f"it's used in the {domain} domain."
    )
    return [
        Message(role="system", content=_SUMMARY_SYSTEM_PROMPT),
        Message(role="user", content=user),
    ]


async def _generate_one_summary(
    llm: LLMClient,
    telemetry: _Telemetry,
    entity: str,
    domain: str,
    intents: list[str],
) -> str:
    """One LLM call → one summary string. Telemetry recorded on the way out."""
    messages = _build_summary_prompt(entity, domain, intents)
    started = time.monotonic()
    resp = await llm.generate(messages=messages, max_tokens=200, temperature=0.2)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    usage = resp.usage
    telemetry.record_chat(
        model=resp.model or "unknown",
        in_tok=usage.prompt_tokens if usage else 0,
        out_tok=usage.completion_tokens if usage else 0,
        latency_ms=elapsed_ms,
    )
    return resp.content.strip()


def _generate_summaries_parallel(
    llm: LLMClient,
    telemetry: _Telemetry,
    entity_specs: list[tuple[str, str, list[str]]],
) -> list[str]:
    """Fire all summary requests concurrently; return summaries in input order."""

    async def _all() -> list[str]:
        return await asyncio.gather(
            *(
                _generate_one_summary(llm, telemetry, ent, dom, intents)
                for ent, dom, intents in entity_specs
            )
        )

    return asyncio.run(_all())


def _embed_batch_with_telemetry(
    embedder: EmbedderClient, telemetry: _Telemetry, texts: list[str]
) -> list[list[float]]:
    """Embed a batch; record one telemetry call against the batch's [0] usage."""
    if not texts:
        return []

    async def _run() -> list[list[float]]:
        started = time.monotonic()
        responses = await embedder.embed_batch(texts)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # OpenAI returns aggregate usage on response[0]; remaining are None.
        head_usage = responses[0].usage if responses else None
        telemetry.record_embed(
            model=(responses[0].model if responses else None) or "unknown",
            in_tok=head_usage.prompt_tokens if head_usage else 0,
            latency_ms=elapsed_ms,
        )
        return [r.embedding for r in responses]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Setup helpers — overridden from the synthetic baseline
# ---------------------------------------------------------------------------


def _populate_entity_documents_real_llm(
    registry: StoreRegistry,
    corpus: GeneratedCorpus,
    *,
    llm: LLMClient,
    embedder: EmbedderClient,
    telemetry: _Telemetry,
) -> int:
    """Populate entity docs with LLM summaries + embeddings.

    Replaces the synthetic baseline's hand-formatted strings with one
    LLM call per entity; every resulting doc is embedded and upserted
    into the registry's vector store using the same item_id scheme
    (``doc:<entity>``) so KeywordSearch and SemanticSearch return
    matching ids.
    """
    knowledge = registry.knowledge
    graph_store = knowledge.graph_store
    document_store = knowledge.document_store
    vector_store = knowledge.vector_store

    by_entity: dict[str, list[GeneratedTrace]] = {}
    for gt in corpus.traces:
        for entity in gt.entities:
            by_entity.setdefault(entity, []).append(gt)

    # Order is stable — we depend on it for matching summaries to entities.
    entries = sorted(by_entity.items())
    specs: list[tuple[str, str, list[str]]] = []
    for entity, traces in entries:
        domain = traces[0].domain
        intents = sorted({t.trace.intent for t in traces})
        specs.append((entity, domain, intents))

    logger.info("phase_a.summary_generation_start", entity_count=len(specs))
    summaries = _generate_summaries_parallel(llm, telemetry, specs)
    logger.info(
        "phase_a.summary_generation_done",
        chat_cost_usd=round(telemetry.chat_cost_usd(), 6),
    )

    # Upsert nodes + docs in one pass. Zip ``specs`` (which carries
    # domain) not ``entries`` (which only carries entity + traces).
    doc_ids: list[str] = []
    contents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for (entity, domain, _intents), summary in zip(specs, summaries, strict=True):
        graph_store.upsert_node(
            node_id=entity,
            node_type="entity",
            properties={"name": entity, "domain": domain},
        )
        doc_id = f"doc:{entity}"
        metadata = {
            "entity_id": entity,
            "domain": domain,
            "content_type": "entity_summary",
            "domains": [domain],
            "content_tags": {"signal_quality": "standard"},
            "content": summary,  # vector store reads metadata.content for excerpt
        }
        document_store.put(doc_id=doc_id, content=summary, metadata=metadata)
        doc_ids.append(doc_id)
        contents.append(summary)
        metadatas.append(metadata)

    # Now embed all docs in a single batch and upsert into the vector store.
    logger.info("phase_a.embedding_start", doc_count=len(contents))
    vectors = _embed_batch_with_telemetry(embedder, telemetry, contents)
    logger.info(
        "phase_a.embedding_done",
        embed_cost_usd=round(telemetry.embed_cost_usd(), 6),
    )
    for doc_id, vec, meta in zip(doc_ids, vectors, metadatas, strict=True):
        vector_store.upsert(item_id=doc_id, vector=vec, metadata=meta)
    return len(entries)


def _populate_distractor_documents_real_llm(
    registry: StoreRegistry,
    *,
    embedder: EmbedderClient,
    telemetry: _Telemetry,
) -> int:
    """Populate distractor docs with embeddings.

    Distractor *content* stays hand-written (per the synthetic baseline —
    distractor design is what the dual-loop has to learn to suppress).
    The fork only adds embedding so SemanticSearch sees the distractors
    too.
    """
    document_store = registry.knowledge.document_store
    vector_store = registry.knowledge.vector_store

    doc_ids: list[str] = []
    contents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for domain, docs in _DISTRACTOR_DOCS.items():
        for doc_id, content in docs:
            metadata = {
                "domain": domain,
                "content_type": "entity_summary",
                "domains": [domain],
                "content_tags": {"signal_quality": "standard"},
                "content": content,
            }
            document_store.put(doc_id=doc_id, content=content, metadata=metadata)
            doc_ids.append(doc_id)
            contents.append(content)
            metadatas.append(metadata)

    if contents:
        vectors = _embed_batch_with_telemetry(embedder, telemetry, contents)
        for doc_id, vec, meta in zip(doc_ids, vectors, metadatas, strict=True):
            vector_store.upsert(item_id=doc_id, vector=vec, metadata=meta)
    return len(doc_ids)


# ---------------------------------------------------------------------------
# Per-round helpers — adds SemanticSearch
# ---------------------------------------------------------------------------


def _make_embedding_fn(
    embedder: EmbedderClient, telemetry: _Telemetry
) -> object:
    """Return a sync ``callable(str) -> list[float]`` for SemanticSearch.

    SemanticSearch invokes ``embedding_fn(query_text)`` synchronously
    once per round to vectorize the query. We wrap our async embedder
    via ``asyncio.run`` so each per-round call is a clean entry/exit.
    Telemetry captures these query-time embed calls separately from
    the setup-time batch.
    """

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


def _build_pack_with_semantic(builder: PackBuilder, query: EvalQuery) -> Pack:
    """Same pack budgets as the baseline, just delegated through the builder.

    The builder itself is constructed in :func:`run` with the augmented
    strategy list ``[KeywordSearch, SemanticSearch]`` — no per-call
    branching needed here.
    """
    return builder.build(
        intent=query.intent,
        domain=query.domain,
        budget=PackBudget(
            max_items=DEFAULT_PACK_MAX_ITEMS,
            max_tokens=DEFAULT_PACK_MAX_TOKENS,
        ),
        tag_filters={},
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
    """Phase A scenario: real LLM + real embeddings against the synthetic corpus.

    Constructs Moonshot chat + OpenAI embedder via
    :func:`eval._real_llm.build_phase_a_clients`, runs setup with both
    in the path, then exercises the same agent-loop machinery as the
    synthetic baseline. Reports the same convergence metrics plus
    ``cost.*``, ``llm.*``, ``embedder.*``, and ``latency.*`` series.
    """
    _validate_run_kwargs(
        rounds=rounds,
        feedback_batch_size=feedback_batch_size,
        regime_shift_round=regime_shift_round,
        regime_shift_replacement_count=regime_shift_replacement_count,
    )

    telemetry = _Telemetry()
    chat_client, embedder, llm_config = build_phase_a_clients()

    findings: list[Finding] = []
    metrics: dict[str, float] = {
        "rounds": float(rounds),
        "feedback_batch_size": float(feedback_batch_size),
    }

    corpus = generate_corpus(
        seed=seed,
        traces_per_domain=traces_per_domain,
        entities_per_trace=entities_per_trace,
    )

    metrics["traces_ingested"] = float(_ingest_traces(registry, corpus))
    metrics["entities_upserted"] = float(
        _populate_entity_documents_real_llm(
            registry, corpus, llm=chat_client, embedder=embedder, telemetry=telemetry
        )
    )
    metrics["distractors_planted"] = float(
        _populate_distractor_documents_real_llm(
            registry, embedder=embedder, telemetry=telemetry
        )
    )

    # Cost guard — if setup already blew the cap, bail before the round loop.
    setup_cost = telemetry.total_cost_usd()
    if setup_cost > RUN_HARD_COST_CAP_USD:
        findings.append(
            Finding(
                severity="fail",
                message=(
                    f"Setup cost ${setup_cost:.4f} exceeded "
                    f"hard cap ${RUN_HARD_COST_CAP_USD:.2f}. Aborting."
                ),
            )
        )
        metrics.update(telemetry.to_metrics())
        return ScenarioReport(
            name="agent_loop_convergence_real_llm",
            status="fail",
            metrics=metrics,
            findings=findings,
            decision=(
                "Hard cost cap tripped during setup. Investigate pricing or "
                "input sizes before raising the cap."
            ),
        )

    feedback_dir_holder = tempfile.TemporaryDirectory()
    feedback_dir = Path(feedback_dir_holder.name)
    advisory_dir_root = registry.stores_dir or feedback_dir
    advisory_store = AdvisoryStore(advisory_dir_root / "advisories.json")

    # Strategy list: keyword + semantic. Same vector_store used by setup
    # so item_ids align.
    embed_fn = _make_embedding_fn(embedder, telemetry)
    builder = PackBuilder(
        strategies=[
            KeywordSearch(registry.knowledge.document_store),
            SemanticSearch(registry.knowledge.vector_store, embed_fn),
        ],
        event_log=registry.operational.event_log,
        advisory_store=advisory_store,
    )

    loop_stats = _LoopStats()
    round_results: list[_RoundResult] = []
    run_id = f"convergence_real_llm_{seed:04d}"

    try:
        for round_index in range(rounds):
            query = _round_query(corpus, round_index)
            pack = _build_pack_with_semantic(builder, query)
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
                    generate_advisories=loop_stats.advisory_runs == 0,
                    advisory_min_sample_size=advisory_min_sample_size,
                )

            # Cost guard inside the loop. Cheap to check, expensive to forget.
            if telemetry.total_cost_usd() > RUN_HARD_COST_CAP_USD:
                findings.append(
                    Finding(
                        severity="fail",
                        message=(
                            f"Round {round_index} tripped hard cost cap "
                            f"${RUN_HARD_COST_CAP_USD:.2f}. Aborting."
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
    # Model identifiers are strings — surfaced via findings rather than
    # metrics (which are floats). See the providers finding below.
    findings.extend(_convergence_findings(convergence, loop_stats))

    findings.append(
        Finding(
            severity="info",
            message=(
                f"providers — chat: {llm_config.moonshot_chat_model} "
                f"({llm_config.moonshot_base_url}); "
                f"embed: {llm_config.openai_embedding_model} "
                f"({llm_config.openai_embedding_dim}-dim)"
            ),
            detail={
                "chat_calls": len(telemetry.chat_calls()),
                "embed_calls": len(telemetry.embed_calls()),
                "cost_total_usd": round(telemetry.total_cost_usd(), 6),
            },
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
        "Phase A real-LLM run completed against the synthetic corpus. "
        f"Cost: ${telemetry.total_cost_usd():.4f} "
        f"({len(telemetry.chat_calls())} chat calls, "
        f"{len(telemetry.embed_calls())} embedder calls). "
        "Useful-delta and convergence-delta carry the same meaning as the "
        "synthetic baseline; the cost/latency series are net-new and the "
        "primary deliverable for Phase A. See "
        "docs/design/plan-real-corpus-eval.md §5.1."
    )

    return ScenarioReport(
        name="agent_loop_convergence_real_llm",
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
    )
