"""Types for LLM client abstractions."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from trellis.core.base import TrellisModel


class Message(TrellisModel):
    """A single message in a conversation."""

    role: Literal["system", "user", "assistant"]
    content: str


class TokenUsage(TrellisModel):
    """Token usage reported by an LLM provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResponse(TrellisModel):
    """Response from an LLM generation call."""

    content: str
    model: str | None = None
    usage: TokenUsage | None = None


class EmbeddingResponse(TrellisModel):
    """Response from an embedding call."""

    embedding: list[float] = Field(default_factory=list)
    model: str | None = None
    usage: TokenUsage | None = None
