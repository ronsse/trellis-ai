"""OpenAI provider for ``LLMClient`` and ``EmbedderClient``.

Requires the ``[llm-openai]`` optional extra::

    pip install trellis-ai[llm-openai]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.llm.types import EmbeddingResponse, LLMResponse, Message, TokenUsage

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = structlog.get_logger(__name__)

DEFAULT_CHAT_MODEL = "gpt-4o-mini"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def _build_async_client(
    *,
    api_key: str | None,
    base_url: str | None,
) -> AsyncOpenAI:
    """Construct an ``AsyncOpenAI`` client, deferring the SDK import."""
    try:
        from openai import AsyncOpenAI  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        msg = (
            "openai is required for OpenAI providers. "
            "Install with: pip install trellis-ai[llm-openai]"
        )
        raise ModuleNotFoundError(msg) from exc

    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


class OpenAIClient:
    """``LLMClient`` implementation backed by the OpenAI chat completions API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str = DEFAULT_CHAT_MODEL,
        client: AsyncOpenAI | None = None,
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
        resp = await self._client.chat.completions.create(
            model=chosen_model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        content = choice.message.content or ""
        usage = _extract_usage(resp.usage)
        return LLMResponse(content=content, model=resp.model, usage=usage)


class OpenAIEmbedder:
    """``EmbedderClient`` implementation backed by the OpenAI embeddings API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str = DEFAULT_EMBEDDING_MODEL,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._default_model = default_model
        self._client = client or _build_async_client(api_key=api_key, base_url=base_url)

    async def embed(
        self,
        text: str,
        *,
        model: str | None = None,
    ) -> EmbeddingResponse:
        chosen_model = model or self._default_model
        resp = await self._client.embeddings.create(
            input=[text],
            model=chosen_model,
        )
        return EmbeddingResponse(
            embedding=list(resp.data[0].embedding),
            model=resp.model,
            usage=_extract_usage(resp.usage),
        )

    async def embed_batch(
        self,
        texts: list[str],
        *,
        model: str | None = None,
    ) -> list[EmbeddingResponse]:
        if not texts:
            return []
        chosen_model = model or self._default_model
        resp = await self._client.embeddings.create(
            input=texts,
            model=chosen_model,
        )
        # OpenAI returns usage totals for the batch; attach to the first
        # response and leave usage=None on the rest to avoid double-counting.
        usage = _extract_usage(resp.usage)
        results: list[EmbeddingResponse] = []
        for i, item in enumerate(resp.data):
            results.append(
                EmbeddingResponse(
                    embedding=list(item.embedding),
                    model=resp.model,
                    usage=usage if i == 0 else None,
                )
            )
        return results


def _extract_usage(usage: Any) -> TokenUsage | None:
    """Map an OpenAI usage object (or dict) to ``TokenUsage``."""
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if prompt is None and isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = usage.get("total_tokens")
    return TokenUsage(
        prompt_tokens=int(prompt or 0),
        completion_tokens=int(completion or 0),
        total_tokens=int(total or 0),
    )
