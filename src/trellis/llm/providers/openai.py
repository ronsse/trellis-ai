"""OpenAI provider for ``LLMClient`` and ``EmbedderClient``.

Requires the ``[llm-openai]`` optional extra::

    pip install trellis-ai[llm-openai]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog

from trellis.errors import ConfigError
from trellis.llm.types import EmbeddingResponse, LLMResponse, Message, TokenUsage

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionMessageParam

logger = structlog.get_logger(__name__)

DEFAULT_CHAT_MODEL = "gpt-4o-mini"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def _build_async_client(
    *,
    api_key: str | None,
    base_url: str | None,
    setting: str,
) -> AsyncOpenAI:
    """Construct an ``AsyncOpenAI`` client, deferring the SDK import.

    Raises :class:`~trellis.errors.ConfigError` naming *setting* when the
    SDK is installed but resolves no API key — neither *api_key* nor the
    SDK's own ``OPENAI_API_KEY`` env var fallback. The constructor's
    untyped ``openai.OpenAIError`` is chained as the cause; its text stays
    out of the message, mirroring
    ``trellis.stores.registry._build_openai_embedding_fn`` (#786). *setting*
    is a parameter because :class:`OpenAIClient` and :class:`OpenAIEmbedder`
    are configured under different ``llm:`` sub-keys.
    """
    try:
        from openai import AsyncOpenAI, OpenAIError  # noqa: PLC0415
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
    try:
        return AsyncOpenAI(**kwargs)
    except OpenAIError as exc:
        literal_setting = setting.removesuffix("_env")
        msg = (
            "OpenAI is configured but no API key was found. Set"
            f" {setting} to the name of an environment variable holding"
            f" the key, {literal_setting} to a literal value, or export"
            " OPENAI_API_KEY."
        )
        raise ConfigError(msg, setting=setting) from exc


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
        self._client = client or _build_async_client(
            api_key=api_key, base_url=base_url, setting="llm.api_key_env"
        )

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        chosen_model = model or self._default_model
        # Message.role is Literal["system", "user", "assistant"] which matches
        # the OpenAI SDK's ChatCompletionMessageParam union, but mypy can't pick
        # one TypedDict from {"role": str, "content": str} without help.
        sdk_messages = cast(
            "list[ChatCompletionMessageParam]",
            [{"role": m.role, "content": m.content} for m in messages],
        )
        resp = await self._client.chat.completions.create(
            model=chosen_model,
            messages=sdk_messages,
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
        self._client = client or _build_async_client(
            api_key=api_key, base_url=base_url, setting="llm.embedding.api_key_env"
        )

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
