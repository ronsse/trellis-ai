"""Anthropic provider for ``LLMClient``.

Requires the ``[llm-anthropic]`` optional extra::

    pip install trellis-ai[llm-anthropic]

Anthropic does not currently offer first-party text embeddings; use
:class:`trellis.llm.providers.openai.OpenAIEmbedder` (or another
``EmbedderClient`` implementation) for embeddings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.llm.types import LLMResponse, Message, TokenUsage

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

logger = structlog.get_logger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _build_async_client(
    *,
    api_key: str | None,
    base_url: str | None,
) -> AsyncAnthropic:
    """Construct an ``AsyncAnthropic`` client, deferring the SDK import."""
    try:
        from anthropic import AsyncAnthropic  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        msg = (
            "anthropic is required for the Anthropic provider. "
            "Install with: pip install trellis-ai[llm-anthropic]"
        )
        raise ModuleNotFoundError(msg) from exc

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncAnthropic(**kwargs)


class AnthropicClient:
    """``LLMClient`` implementation backed by the Anthropic Messages API.

    System messages are collapsed into the ``system`` parameter per the
    Messages API convention.  All other ``Message`` entries become
    conversation turns.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str = DEFAULT_MODEL,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._default_model = default_model
        self._client = client or _build_async_client(api_key=api_key, base_url=base_url)

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        chosen_model = model or self._default_model
        system_text, conversation = _split_system(messages)

        kwargs: dict[str, Any] = {
            "model": chosen_model,
            "messages": conversation,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_text:
            kwargs["system"] = system_text

        resp = await self._client.messages.create(**kwargs)
        content = _extract_text(resp)
        usage = _extract_usage(resp.usage)
        return LLMResponse(
            content=content,
            model=getattr(resp, "model", chosen_model),
            usage=usage,
        )


def _split_system(
    messages: list[Message],
) -> tuple[str, list[dict[str, str]]]:
    """Collapse ``system`` messages into one system string and return the rest."""
    system_parts: list[str] = []
    conversation: list[dict[str, str]] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        else:
            conversation.append({"role": m.role, "content": m.content})
    return "\n\n".join(system_parts), conversation


def _extract_text(resp: Any) -> str:
    """Concatenate text blocks from a Messages API response."""
    blocks = getattr(resp, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "".join(parts)


def _extract_usage(usage: Any) -> TokenUsage | None:
    """Map an Anthropic usage object to ``TokenUsage``."""
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    return TokenUsage(
        prompt_tokens=int(input_tokens),
        completion_tokens=int(output_tokens),
        total_tokens=int(input_tokens) + int(output_tokens),
    )
