"""LLM client abstractions — protocols and types.

This package defines the provider-agnostic interfaces that Trellis components
use to call LLMs, embed text, and score cross-encoder pairs.  Reference
implementations for specific providers live in ``trellis.llm.providers``
and are gated behind optional extras (``[llm-openai]``, ``[llm-anthropic]``).
"""

from trellis.llm.protocol import CrossEncoderClient, EmbedderClient, LLMClient
from trellis.llm.types import EmbeddingResponse, LLMResponse, Message, TokenUsage

__all__ = [
    "CrossEncoderClient",
    "EmbedderClient",
    "EmbeddingResponse",
    "LLMClient",
    "LLMResponse",
    "Message",
    "TokenUsage",
]
