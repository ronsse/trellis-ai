"""Program-level convergence with a real LLM-backed embedder.

E3 (Wave 5) of [`docs/design/plan-next-swarm-wave.md`](
../../../docs/design/plan-next-swarm-wave.md) §8. Forks the synthetic
master scenario (:mod:`eval.scenarios.program_convergence.scenario`) by
replacing its keyword-only retrieval substrate with **real-embedding
SemanticSearch** layered on top of the same KeywordSearch. The per-round
loop body is unchanged — we delegate to
:func:`program_convergence.scenario._run_loop` via the ``extra_strategies``
and ``post_populate_hook`` slots C2 carved out so this fork carries zero
duplicated orchestration scaffold.

Three deltas vs. the synthetic master:

1. **Real embeddings.** ``OpenAIEmbedder`` (``text-embedding-3-small``,
   1536-dim) embeds every seed-entity document at setup time and every
   per-round query at retrieval time. The synthetic master uses
   keyword-only retrieval (no vector store interaction); this scenario
   populates the registry's vector store and adds a ``SemanticSearch``
   strategy to the builder so axes A and B (pack quality + useful-item
   fraction) move under semantic similarity, not just BM25.
2. **Budget telemetry.** A :class:`_Telemetry` instance accumulates
   embed-call token counts; at the end of the run we emit ONE
   :attr:`~trellis.stores.base.event_log.EventType.BUDGET_CONSUMED`
   event with the run's total cost. Per-call records stay in-memory
   (the event is summary-only) so downstream cost analyzers can join
   ``EventType=BUDGET_CONSUMED`` rows on ``source`` /
   ``entity_id=run_id``.
3. **Hard cost cap.** A configurable ``run_hard_cost_cap_usd`` (default
   ``$2.00``) trips :class:`RunBudgetError` if the accumulated cost
   exceeds the cap mid-run. The error propagates out of ``run()`` after
   the BUDGET_CONSUMED event has been emitted — operators always see
   the bill, even on abort. Setup-time exceedance is detected after the
   doc-embedding batch returns; mid-loop exceedance is detected after
   each per-round query embed.

Credential gating:
    Requires ``OPENAI_API_KEY`` to be set. ``ANTHROPIC_API_KEY``
    is honoured by :func:`_has_credentials` (matches the E3-prep
    contract — either env var counts as "real-LLM credentials
    present"), but the run path itself requires OpenAI today because
    :mod:`eval._real_llm` only ships an OpenAI embedder factory.
    When only ``ANTHROPIC_API_KEY`` is set the scenario returns
    ``status="skip"`` with an info finding pointing operators at the
    OpenAI requirement. Extending :mod:`eval._real_llm` with an
    Anthropic-only embedder path would change this branch to a fail-
    open run; that's deferred until an Anthropic embedder ships.

Cost calibration:
    Per-run cost is ``tokens_consumed * USD_PER_TOKEN`` where
    ``USD_PER_TOKEN = OPENAI_EMBED_3_SMALL_USD_PER_M / 1_000_000`` (the
    OpenAI ``text-embedding-3-small`` price as of 2026-04, $0.02 per
    million input tokens). The synthetic corpus produces ~22 seed-entity
    summaries averaging ~30 tokens each (~660 setup tokens), plus one
    query embed per round of ~10 tokens. At the default 50 rounds the
    expected token total is ``660 + 50*10 = 1160`` tokens, costing
    ``1160 / 1e6 * $0.02 ~= $0.0000232`` — well under the $2 hard cap.
    The E3-prep doc's ~$0.50 figure assumed 1K tokens per
    summary + 30 distractors + per-round query at the same rate; this
    scenario reuses the synthetic corpus's smaller doc shape so the
    realised cost is two orders of magnitude lower. The hard cap stays
    at $2.00 to absorb pricing regressions or operator-supplied larger
    corpora without surprising the operator. See
    ``docs/design/plan-program-level-eval.md`` §4.5 for the calibration
    formula write-up.

Mock-API smoke test:
    Setting ``TRELLIS_EVAL_REAL_LLM_MOCK=1`` swaps the
    :class:`OpenAIEmbedder` for an in-memory
    :class:`_DeterministicMockEmbedder`. Tests use this to exercise the
    full nine-axis loop + BUDGET_CONSUMED emit path without billing
    tokens against a real provider. The mock embedder records the same
    telemetry shape (per-call token counts derived from
    ``len(text.split())``) so the cost arithmetic is exercised even
    under mock.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from eval._real_llm import (
    DEFAULT_OPENAI_EMBEDDING_MODEL,
    OPENAI_EMBED_3_SMALL_USD_PER_M,
    OPENAI_EMBEDDING_3_SMALL_DIM,
    RealLLMConfigError,
    build_openai_embedder,
    resolve_config,
)
from eval.runner import Finding, ScenarioReport, ScenarioStatus
from eval.scenarios._convergence_common import (
    DEFAULT_ADVISORY_MIN_SAMPLE_SIZE,
    DEFAULT_FEEDBACK_BATCH_SIZE,
    DEFAULT_ROUNDS,
    DEFAULT_SUCCESS_COVERAGE_THRESHOLD,
    NINE_AXIS_LABELS,
    _build_multi_axis_stats,
    _convergence_metrics,
    _loop_metrics,
    _loops_summary_finding,
    _multi_axis_metrics,
)
from eval.scenarios.program_convergence.scenario import (
    DEFAULT_ADVISORY_HIT_LOOKBACK_ROUNDS,
    DEFAULT_ANALYZER_CADENCE,
    DEFAULT_ENTITIES_PER_TRACE,
    DEFAULT_TRACES_PER_DOMAIN,
    _composite_convergence_finding,
    _per_axis_findings,
    _run_loop,
    _validate_run_kwargs,
)
from trellis.llm.protocol import EmbedderClient
from trellis.llm.types import EmbeddingResponse, TokenUsage
from trellis.retrieve.strategies import SemanticSearch
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

SCENARIO_NAME = "program_convergence_real_llm"

#: Environment variables the scenario looks for. Either is sufficient
#: for the credential-gated entry path. The actual implementation
#: requires ``OPENAI_API_KEY`` (see module docstring); ``ANTHROPIC``
#: stays in the tuple so the E3-prep test contract holds.
_CREDENTIAL_ENV_VARS: tuple[str, ...] = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")

#: Hatch env var that swaps the real embedder for a deterministic mock.
#: Used by the smoke test to exercise the full nine-axis loop +
#: BUDGET_CONSUMED emit path without calling real APIs. Setting to any
#: non-empty value activates the mock path.
MOCK_HATCH_ENV_VAR: str = "TRELLIS_EVAL_REAL_LLM_MOCK"

#: Default hard cost cap per run. The expected realised cost is two
#: orders of magnitude below this; the cap exists to bound operator
#: surprise on pricing regressions or larger corpora.
DEFAULT_RUN_HARD_COST_CAP_USD: float = 2.00

#: Per-token cost for ``text-embedding-3-small`` (USD). Derived from
#: ``OPENAI_EMBED_3_SMALL_USD_PER_M`` so the eval scenarios share a
#: single source of truth on pricing.
_USD_PER_TOKEN: float = OPENAI_EMBED_3_SMALL_USD_PER_M / 1_000_000

#: Provider slug recorded on the BUDGET_CONSUMED event payload. The
#: real-OpenAI and mock paths both emit the same provider — operators
#: distinguish via the event's ``source`` (the mock path stamps
#: ``source="eval.program_convergence_real_llm.mock"``).
_PROVIDER_OPENAI: str = "openai"


class RunBudgetError(RuntimeError):
    """Raised when accumulated cost exceeds the per-run hard cap.

    Propagates out of :func:`run` after the
    :attr:`~trellis.stores.base.event_log.EventType.BUDGET_CONSUMED`
    event has been emitted so the operator always sees the bill, even
    on abort. The error message carries the cap, the realised cost,
    and a pointer at the configurable kwarg.
    """


def _has_credentials() -> bool:
    """Return ``True`` iff at least one credential env var is non-empty."""
    return any(os.environ.get(name) for name in _CREDENTIAL_ENV_VARS)


def _mock_enabled() -> bool:
    """Return ``True`` iff the mock-embedder hatch is set to a non-empty value."""
    return bool(os.environ.get(MOCK_HATCH_ENV_VAR))


# ---------------------------------------------------------------------------
# Telemetry — accumulates per-call token totals for the BUDGET_CONSUMED event
# ---------------------------------------------------------------------------


@dataclass
class _EmbedCallRecord:
    """One embedder call (single text or batch)."""

    model: str
    input_tokens: int
    latency_ms: int


@dataclass
class _Telemetry:
    """Accumulator for embedder telemetry across a single scenario run.

    Lighter than the agent-loop fork's :class:`_Telemetry` because the
    program scenario does not invoke a chat model — only the embedder
    is on the hot path. Subset of the same shape so future unification
    is a no-op.
    """

    calls: list[_EmbedCallRecord] = field(default_factory=list)

    def record_embed(self, model: str, in_tok: int, latency_ms: int) -> None:
        self.calls.append(_EmbedCallRecord(model, in_tok, latency_ms))

    def total_input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    def total_cost_usd(self) -> float:
        return self.total_input_tokens() * _USD_PER_TOKEN

    def call_count(self) -> int:
        return len(self.calls)

    def to_metrics(self) -> dict[str, float]:
        return {
            "embedder.calls_total": float(self.call_count()),
            "embedder.input_tokens_total": float(self.total_input_tokens()),
            "cost.total_usd": round(self.total_cost_usd(), 6),
        }


# ---------------------------------------------------------------------------
# Mock embedder — exercises the loop without real-API spend
# ---------------------------------------------------------------------------


class _DeterministicMockEmbedder:
    """In-memory embedder that produces a stable vector from text content.

    The vector dimension matches
    :data:`eval._real_llm.OPENAI_EMBEDDING_3_SMALL_DIM` (1536) so the
    mock path exercises the same shape contract real OpenAI does.
    Vectors derive from a SHA-256 hash of the text — same text always
    embeds to the same vector, different texts produce different
    vectors with high probability. Token counts are approximated as
    ``len(text.split())`` so the cost arithmetic is exercised.

    Used only when :data:`MOCK_HATCH_ENV_VAR` is set; production /
    operator paths instantiate :class:`OpenAIEmbedder` instead.
    """

    def __init__(self, *, model: str = DEFAULT_OPENAI_EMBEDDING_MODEL) -> None:
        self._model = model
        self._dim = OPENAI_EMBEDDING_3_SMALL_DIM

    def _vectorize(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Cycle the 32-byte digest across the 1536-dim vector.
        # Normalize bytes to [-1.0, 1.0] so cosine similarity is well-behaved.
        return [
            (digest[i % len(digest)] - 128) / 128.0
            for i in range(self._dim)
        ]

    @staticmethod
    def _approx_tokens(text: str) -> int:
        # Word-count proxy. Matches OpenAI's roughly-one-token-per-word
        # behaviour closely enough for cost arithmetic exercise.
        return max(1, len(text.split()))

    async def embed(
        self, text: str, *, model: str | None = None
    ) -> EmbeddingResponse:
        return EmbeddingResponse(
            embedding=self._vectorize(text),
            model=model or self._model,
            usage=TokenUsage(
                prompt_tokens=self._approx_tokens(text),
                completion_tokens=0,
                total_tokens=self._approx_tokens(text),
            ),
        )

    async def embed_batch(
        self, texts: list[str], *, model: str | None = None
    ) -> list[EmbeddingResponse]:
        if not texts:
            return []
        total = sum(self._approx_tokens(t) for t in texts)
        chosen_model = model or self._model
        results: list[EmbeddingResponse] = []
        for i, text in enumerate(texts):
            results.append(
                EmbeddingResponse(
                    embedding=self._vectorize(text),
                    model=chosen_model,
                    # Aggregate usage on response[0]; None on the rest,
                    # matching OpenAI's batch contract.
                    usage=(
                        TokenUsage(
                            prompt_tokens=total,
                            completion_tokens=0,
                            total_tokens=total,
                        )
                        if i == 0
                        else None
                    ),
                )
            )
        return results


# ---------------------------------------------------------------------------
# Embedder helpers — wrap async client + record telemetry
# ---------------------------------------------------------------------------


def _embed_batch_with_telemetry(
    embedder: EmbedderClient,
    telemetry: _Telemetry,
    texts: list[str],
) -> list[list[float]]:
    """Embed a batch synchronously; record one telemetry call against [0] usage."""
    if not texts:
        return []

    async def _run() -> list[list[float]]:
        started = time.monotonic()
        responses = await embedder.embed_batch(texts)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        head_usage = responses[0].usage if responses else None
        telemetry.record_embed(
            model=(responses[0].model if responses else None) or "unknown",
            in_tok=head_usage.prompt_tokens if head_usage else 0,
            latency_ms=elapsed_ms,
        )
        return [r.embedding for r in responses]

    return asyncio.run(_run())


def _make_embedding_fn(
    embedder: EmbedderClient,
    telemetry: _Telemetry,
    *,
    on_call: Any = None,
) -> Any:
    """Build the sync ``callable(str) -> list[float]`` SemanticSearch expects.

    Each per-round invocation embeds a single query string. ``on_call``
    is an optional callback fired *after* the telemetry record lands —
    used by the mid-loop cost-cap watcher to bail out as soon as the
    cap trips. The watcher raises :class:`RunBudgetError`; we re-raise
    out of ``embed_query`` so PackBuilder surfaces the abort cleanly.
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

        vector = asyncio.run(_one())
        if on_call is not None:
            on_call()
        return vector

    return embed_query


# ---------------------------------------------------------------------------
# Post-populate hook — embed every seed doc into the vector store
# ---------------------------------------------------------------------------


def _make_post_populate_hook(
    embedder: EmbedderClient,
    telemetry: _Telemetry,
) -> Any:
    """Return a ``(registry, seed_entities)`` callable that primes the vector store.

    The synthetic master writes ``doc:<entity>`` rows to the document
    store but never touches the vector store. We re-read each document
    via :meth:`DocumentStore.get`, batch-embed, and upsert into the
    vector store using the same ``doc:<entity>`` id so SemanticSearch
    returns rows whose ``item_id`` matches keyword-search's items —
    pack-builder dedup works without item_id translation.
    """

    def hook(registry: StoreRegistry, seed_entities: list[str]) -> None:
        if not seed_entities:
            return
        document_store = registry.knowledge.document_store
        vector_store = registry.knowledge.vector_store

        doc_ids: list[str] = []
        contents: list[str] = []
        metadatas: list[dict[str, Any]] = []
        for entity in seed_entities:
            doc_id = f"doc:{entity}"
            doc = document_store.get(doc_id)
            if doc is None:
                # Master populates every seed; defensive guard.
                continue
            content = doc.get("content", "")
            metadata = dict(doc.get("metadata", {}))
            # SemanticSearch reads ``metadata.content`` for the excerpt;
            # the synthetic master writes content separately from
            # metadata, so we splice it in here.
            metadata.setdefault("content", content)
            doc_ids.append(doc_id)
            contents.append(content)
            metadatas.append(metadata)

        if not contents:
            return

        logger.info(
            "program_convergence_real_llm.setup_embed_start",
            doc_count=len(contents),
        )
        vectors = _embed_batch_with_telemetry(embedder, telemetry, contents)
        for doc_id, vec, meta in zip(doc_ids, vectors, metadatas, strict=True):
            vector_store.upsert(item_id=doc_id, vector=vec, metadata=meta)
        logger.info(
            "program_convergence_real_llm.setup_embed_done",
            cost_usd=round(telemetry.total_cost_usd(), 6),
        )

    return hook


# ---------------------------------------------------------------------------
# Budget audit — emit one BUDGET_CONSUMED event at end of run
# ---------------------------------------------------------------------------


def _emit_budget_consumed(
    *,
    registry: StoreRegistry,
    telemetry: _Telemetry,
    run_id: str,
    model: str,
    source: str,
) -> None:
    """Emit one BUDGET_CONSUMED event capturing this run's totals.

    Payload contract (per EventType.BUDGET_CONSUMED docstring): a dict
    with required keys ``tokens_consumed``, ``dollars_estimated``,
    ``provider``, ``model``. Cost computed from
    :data:`_USD_PER_TOKEN` so it survives provider repricings via a
    single source of truth in :mod:`eval._real_llm`.
    """
    event_log = registry.operational.event_log
    event_log.emit(
        EventType.BUDGET_CONSUMED,
        source=source,
        entity_id=run_id,
        entity_type="ProgramConvergenceRealLLMRun",
        payload={
            "tokens_consumed": telemetry.total_input_tokens(),
            "dollars_estimated": round(telemetry.total_cost_usd(), 6),
            "provider": _PROVIDER_OPENAI,
            "model": model,
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _skip_report(*, message: str, decision: str) -> ScenarioReport:
    """Return a ``status="skip"`` report with the given info finding."""
    return ScenarioReport(
        name=SCENARIO_NAME,
        status="skip",
        findings=[Finding(severity="info", message=message)],
        decision=decision,
    )


def run(  # noqa: PLR0915 — top-level orchestrator; one coherent run flow, parity with the synthetic master + the agent_loop real-LLM fork
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
    advisory_hit_lookback_rounds: int = DEFAULT_ADVISORY_HIT_LOOKBACK_ROUNDS,
    run_hard_cost_cap_usd: float = DEFAULT_RUN_HARD_COST_CAP_USD,
) -> ScenarioReport:
    """Execute the program-level master scenario with real OpenAI embeddings.

    Skip semantics (no work done, no registry touched):

    - Neither ``OPENAI_API_KEY`` nor ``ANTHROPIC_API_KEY`` set →
      ``status="skip"`` with an info finding pointing operators at both
      env vars (matches the E3-prep contract).
    - Only ``ANTHROPIC_API_KEY`` set (no ``OPENAI_API_KEY``) →
      ``status="skip"`` with an info finding noting that embeddings
      require OpenAI today.

    Run semantics:

    - ``OPENAI_API_KEY`` set, mock hatch off → constructs
      :class:`OpenAIEmbedder`, embeds the synthetic corpus at setup,
      runs the nine-axis loop with SemanticSearch + KeywordSearch,
      embeds each per-round query, emits ONE BUDGET_CONSUMED event
      with the run's totals at end of run.
    - ``OPENAI_API_KEY`` set, mock hatch on
      (``TRELLIS_EVAL_REAL_LLM_MOCK=1``) → same flow, but the
      embedder is :class:`_DeterministicMockEmbedder` so no API calls
      are made. The BUDGET_CONSUMED event still fires; cost
      arithmetic exercises the same path.

    Per-run hard cost cap (``run_hard_cost_cap_usd``, default ``$2.00``):
    if accumulated cost crosses the cap mid-run, the BUDGET_CONSUMED
    event is emitted first (operators always see the bill) then
    :class:`RunBudgetError` is raised. The runner catches it as a
    standard scenario failure.
    """
    _validate_run_kwargs(
        rounds=rounds,
        feedback_batch_size=feedback_batch_size,
        advisory_hit_lookback_rounds=advisory_hit_lookback_rounds,
    )

    if not _has_credentials():
        return _skip_report(
            message=(
                "set OPENAI_API_KEY or ANTHROPIC_API_KEY to run "
                "program_convergence_real_llm"
            ),
            decision=(
                "Scenario skipped — real-LLM credentials not configured. "
                "See eval/scenarios/program_convergence_real_llm/scenario.py "
                "module docstring for the cost calibration formula."
            ),
        )

    if not os.environ.get("OPENAI_API_KEY"):
        # ANTHROPIC_API_KEY is set but OPENAI_API_KEY isn't — the
        # embedder factory in eval/_real_llm.py is OpenAI-only today,
        # so we can't run. Loud-skip with a precise pointer.
        return _skip_report(
            message=(
                "OPENAI_API_KEY required for embeddings; ANTHROPIC_API_KEY "
                "alone is insufficient. eval/_real_llm.py only ships an "
                "OpenAI embedder factory today — extend it with an "
                "Anthropic path to enable this branch."
            ),
            decision=(
                "Scenario skipped — embedder factory does not support an "
                "Anthropic-only path today. Operator must set "
                "OPENAI_API_KEY or extend eval/_real_llm.py."
            ),
        )

    # Resolve config + build the embedder (real or mock).
    mock_active = _mock_enabled()
    config = resolve_config()
    embedder: EmbedderClient
    source = "eval.program_convergence_real_llm"
    if mock_active:
        embedder = _DeterministicMockEmbedder(model=config.openai_embedding_model)
        source = "eval.program_convergence_real_llm.mock"
        logger.info("program_convergence_real_llm.mock_enabled")
    else:
        try:
            embedder = build_openai_embedder(config)
        except RealLLMConfigError as exc:
            # Defensive: _has_credentials() + OPENAI_API_KEY check
            # already passed, so this branch only fires on a race
            # (env var unset between check and factory) or a
            # configuration error inside the factory. Surface as a
            # fail-fast finding rather than a skip.
            return ScenarioReport(
                name=SCENARIO_NAME,
                status="fail",
                findings=[
                    Finding(
                        severity="fail",
                        message=f"OpenAI embedder construction failed: {exc}",
                    )
                ],
                decision=(
                    "Scenario aborted before any work — embedder factory "
                    "raised RealLLMConfigError. Check env-var resolution "
                    "(typically `op run --env-file=.env -- ...`)."
                ),
            )

    telemetry = _Telemetry()
    run_id = f"program_convergence_real_llm_{seed:04d}"

    def _check_budget_cap() -> None:
        cost = telemetry.total_cost_usd()
        if cost > run_hard_cost_cap_usd:
            msg = (
                f"per-run hard cost cap tripped: ${cost:.6f} > "
                f"${run_hard_cost_cap_usd:.2f}. Adjust "
                f"`run_hard_cost_cap_usd` kwarg or reduce rounds/corpus."
            )
            raise RunBudgetError(msg)

    # Wrap the embedding callable so the mid-loop cap watcher fires
    # immediately after each per-round query embed. Setup-time
    # embedding is checked once after the batch returns.
    embedding_fn = _make_embedding_fn(
        embedder, telemetry, on_call=_check_budget_cap
    )
    post_populate_hook = _make_post_populate_hook(embedder, telemetry)
    semantic_strategy = SemanticSearch(
        registry.knowledge.vector_store, embedding_fn
    )

    budget_error: RunBudgetError | None = None
    loop_result = None
    try:
        loop_result = _run_loop(
            registry,
            seed=seed,
            rounds=rounds,
            feedback_batch_size=feedback_batch_size,
            traces_per_domain=traces_per_domain,
            entities_per_trace=entities_per_trace,
            success_coverage_threshold=success_coverage_threshold,
            advisory_min_sample_size=advisory_min_sample_size,
            analyzer_cadence=analyzer_cadence,
            advisory_hit_lookback_rounds=advisory_hit_lookback_rounds,
            run_id=run_id,
            extra_strategies=[semantic_strategy],
            post_populate_hook=post_populate_hook,
        )
        # Defensive setup-cap check: if the setup-time embed alone
        # blew the cap, we already accumulated cost. Run-loop returned
        # whatever rounds completed before the cap tripped via
        # ``embed_query``'s raise path; a clean return means we did
        # not trip mid-loop. Recheck so a setup-only blowup still
        # surfaces as a RunBudgetError.
        _check_budget_cap()
    except RunBudgetError as exc:
        budget_error = exc
    finally:
        # ALWAYS emit BUDGET_CONSUMED — operators see the bill on the
        # happy path and on the abort path.
        _emit_budget_consumed(
            registry=registry,
            telemetry=telemetry,
            run_id=run_id,
            model=config.openai_embedding_model,
            source=source,
        )

    if budget_error is not None:
        # Re-raise after the audit event has landed.
        raise budget_error

    assert loop_result is not None  # type-narrowing for mypy
    findings: list[Finding] = []
    metrics: dict[str, float | str] = {
        "rounds": float(rounds),
        "feedback_batch_size": float(feedback_batch_size),
        "analyzer_cadence": float(analyzer_cadence),
        "traces_ingested": float(loop_result.traces_ingested),
        "seed_entities": float(loop_result.seed_entity_count),
        "mock_enabled": float(1.0 if mock_active else 0.0),
    }

    nine_axis_rounds = [r.to_nine_axis() for r in loop_result.round_results]
    stats = _build_multi_axis_stats(nine_axis_rounds)

    metrics.update(_loop_metrics(loop_result.loop_stats))
    metrics.update(_convergence_metrics(stats.convergence))
    metrics.update(_multi_axis_metrics(stats))
    metrics.update(telemetry.to_metrics())

    findings.append(_loops_summary_finding(loop_result.loop_stats))
    findings.extend(_per_axis_findings(stats))
    findings.append(_composite_convergence_finding(stats))
    findings.append(
        Finding(
            severity="info",
            message=(
                f"provider: {_PROVIDER_OPENAI} ({config.openai_embedding_model}); "
                f"embed calls: {telemetry.call_count()}; "
                f"tokens: {telemetry.total_input_tokens()}; "
                f"cost: ${telemetry.total_cost_usd():.6f} "
                f"(cap ${run_hard_cost_cap_usd:.2f})"
            ),
            detail={
                "provider": _PROVIDER_OPENAI,
                "model": config.openai_embedding_model,
                "embedding_dim": config.openai_embedding_dim,
                "mock_enabled": mock_active,
                "embed_calls": telemetry.call_count(),
                "tokens_consumed": telemetry.total_input_tokens(),
                "dollars_estimated": round(telemetry.total_cost_usd(), 6),
                "run_hard_cost_cap_usd": run_hard_cost_cap_usd,
            },
        )
    )

    decision = (
        "Program-level convergence with real OpenAI embeddings completed. "
        f"Cost: ${telemetry.total_cost_usd():.6f} across "
        f"{telemetry.call_count()} embed calls "
        f"({telemetry.total_input_tokens()} tokens). One BUDGET_CONSUMED "
        "event emitted to the operational EventLog with the run's totals; "
        "join on entity_id=run_id to correlate with downstream cost analysis. "
        f"Mock mode: {'on' if mock_active else 'off'}."
    )

    status: ScenarioStatus = "pass"
    return ScenarioReport(
        name=SCENARIO_NAME,
        status=status,
        metrics=metrics,
        findings=findings,
        decision=decision,
        convergence_stats=stats,
    )


# Re-exports for axis-label parity with the synthetic master's test surface.
__all__ = [
    "DEFAULT_RUN_HARD_COST_CAP_USD",
    "MOCK_HATCH_ENV_VAR",
    "NINE_AXIS_LABELS",
    "SCENARIO_NAME",
    "RunBudgetError",
    "run",
]
