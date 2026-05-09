"""Protocols for LLM and embedding clients."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trellis.llm.types import EmbeddingResponse, LLMResponse, Message


@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM generation.

    Implementations wrap a specific provider SDK (OpenAI, Anthropic, etc.)
    and return structured ``LLMResponse`` objects with optional token usage.
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
