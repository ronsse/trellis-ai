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
    """Token usage reported by an LLM provider.

    ``total_tokens`` is ``prompt_tokens + completion_tokens`` and
    deliberately **excludes** the two cache counters. A cache read is
    billed at a fraction of the standard input rate and a cache write at a
    premium, so they are not addends of the same quantity; folding them in
    would make *enabling* prompt caching raise ``total_tokens`` while
    lowering the bill, and would erase the only signal a cache measurement
    exists to carry. Report them beside the total, never inside it.

    The two cache fields are ``None`` when the provider reported nothing,
    and ``0`` when it reported zero. That distinction is the point: on a
    provider that caches, ``cache_read_input_tokens == 0`` across repeated
    calls sharing a prefix means a silent invalidator, which is a finding —
    while ``None`` means the question was never asked. A shared ``0`` would
    render a provider that does not report the number as a permanently
    broken cache.

    Their names are lifted verbatim from Anthropic's ``usage`` object and
    carry its semantics: **``prompt_tokens`` excludes them**, so the full
    input of an Anthropic call is ``prompt_tokens + cache_read +
    cache_creation``. That arithmetic is provider-specific and is why the
    OpenAI adapter leaves both fields unset rather than mapping its own
    ``prompt_tokens_details.cached_tokens``, which is an *inclusive subset*
    of ``prompt_tokens`` and would double-count under the same sum.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


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
