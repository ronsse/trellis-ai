"""Embedding-call telemetry shared across embedding-using scenarios.

``dbt_corpus_convergence`` and ``github_corpus_convergence`` exercise
embeddings only (no chat). The Phase-A real-LLM scenario also tracks
chat tokens — that telemetry is more complex and stays in its own
module since costs and per-model pricing differ.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from eval._real_llm import OPENAI_EMBED_3_SMALL_USD_PER_M
from trellis.llm.protocol import EmbedderClient


@dataclass
class _EmbedRecord:
    model: str
    input_tokens: int
    latency_ms: int


@dataclass
class _EmbedTelemetry:
    """Aggregate embedder calls, total cost, and latency for a run."""

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


def _make_embedding_fn(
    embedder: EmbedderClient,
    telemetry: _EmbedTelemetry,
) -> Callable[[str], list[float]]:
    """Sync ``callable(str) -> list[float]`` for SemanticSearch query embeds.

    SemanticSearch wants a synchronous ``embed_query`` callable; the
    underlying ``EmbedderClient.embed`` is async. We bridge with
    ``asyncio.run`` per call and record telemetry inline so the cost cap
    and latency metrics see every query embed, not just batch-setup
    embeds.
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
