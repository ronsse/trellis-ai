"""Protocols for LLM and embedding clients."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trellis.llm.types import EmbeddingResponse, LLMResponse, Message


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM generation.

    Implementations wrap a specific provider SDK (OpenAI, Anthropic, etc.)
    and return structured ``LLMResponse`` objects with optional token usage.

    ``temperature`` is **advisory**.  It stays on this protocol because the
    OpenAI-compatible path — which is what the reference deployment runs,
    against a local Ollama — accepts and honours it, and callers legitimately
    depend on it (``distill`` and ``reconcile`` both ask for ``0.0``).  A
    provider that cannot express it drops it rather than raising: Anthropic
    removed ``temperature`` / ``top_p`` / ``top_k`` from the Messages API on
    Claude Opus 4.7 and later, and from the Python SDK's signatures in 1.x, so
    :class:`trellis.llm.providers.anthropic.AnthropicClient` never forwards it.

    The consequence worth stating plainly: **a caller cannot assume a
    temperature it passes was applied.**  Treat it as a hint to providers that
    have the knob, never as a determinism guarantee — which it was not on any
    model even when it was accepted.
    """

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse: ...


@runtime_checkable
class EmbedderClient(Protocol):
    """Protocol for text embedding.

    Implementations wrap a specific provider SDK and return vectors
    with optional token usage.
    """

    async def embed(
        self,
        text: str,
        *,
        model: str | None = None,
    ) -> EmbeddingResponse: ...

    async def embed_batch(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[EmbeddingResponse]: ...
