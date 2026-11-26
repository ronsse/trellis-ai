"""Anthropic provider for ``LLMClient``.

Requires the ``[llm-anthropic]`` optional extra::

    pip install trellis-ai[llm-anthropic]

Anthropic does not currently offer first-party text embeddings; use
:class:`trellis.llm.providers.openai.OpenAIEmbedder` (or another
``EmbedderClient`` implementation) for embeddings.

**No sampling parameter is ever sent.** ``temperature`` / ``top_p`` /
``top_k`` are absent from every request this adapter builds, and that is
the module's load-bearing invariant — see :meth:`AnthropicClient.generate`
for why it is unconditional rather than model-gated, and
``tests/unit/llm/test_anthropic_provider.py`` for the boundary test that
pins it against a fake whose signature matches the real SDK's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.llm.types import LLMResponse, Message, TokenUsage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from anthropic import AsyncAnthropic

logger = structlog.get_logger(__name__)

#: Default model when the operator configures none.  This is the *Claude
#: API ID* for Claude Haiku 4.5, not a snapshot of an alias: for models
#: before the 4.6 generation the dated form is the ID and ``claude-haiku-4-5``
#: is the convenience alias that resolves to it, so the dateless form would
#: be the looser pointer rather than the more modern one.  The pin is
#: deliberate — Trellis stores what these passes produce, and a default that
#: changes model underneath an operator is worse for a durable memory store
#: than one that requires an explicit bump.  The model-deprecations page
#: gives it a **tentative** retirement of "not sooner than 2026-10-15" (read
#: 2026-09-04), so the pin has a known expiry — but tentative is not a
#: commitment, and it is **not** the earliest of the current models:
#: ``claude-sonnet-4-5-20250929`` is listed for 2026-09-29 and
#: ``claude-opus-4-5-20251101`` for 2026-11-24.  Re-read that page rather
#: than trusting this comment; override the pin with ``model:`` in the
#: ``llm:`` config block.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

#: Sampling parameters this adapter refuses to forward.  Named so the
#: invariant is enumerable by a test rather than implied by the absence of
#: three assignments.
UNSUPPORTED_SAMPLING_PARAMS: tuple[str, ...] = ("temperature", "top_p", "top_k")


def _build_async_client(
    *,
    api_key: str | None,
    base_url: str | None,
    default_headers: Mapping[str, str] | None = None,
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
    if default_headers:
        kwargs["default_headers"] = dict(default_headers)
    return AsyncAnthropic(**kwargs)


class AnthropicClient:
    """``LLMClient`` implementation backed by the Anthropic Messages API.

    System messages are collapsed into the ``system`` parameter per the
    Messages API convention.  All other ``Message`` entries become
    conversation turns.

    **``reasoning_effort``** (``llm.reasoning_effort`` in config) is a
    per-client setting, not a ``generate`` argument, so the ``LLMClient``
    protocol is unchanged.  When set, every request carries adaptive
    thinking plus an effort level::

        thinking={"type": "adaptive"}
        output_config={"effort": <reasoning_effort>}

    The value is forwarded verbatim (the API documents ``low``, ``medium``,
    ``high``, ``xhigh`` and ``max``; the endpoint rejects anything else).  No
    ``budget_tokens`` is ever sent: the fixed-budget form
    ``{"type": "enabled", "budget_tokens": N}`` is deprecated on the 4.6
    models and rejected with a 400 on 4.7 and later, and it would need
    ``1024 <= N < max_tokens`` checking here.  Adaptive thinking instead
    draws from ``max_tokens``, so the budget-below-``max_tokens`` rule is
    satisfied by construction and ``max_tokens`` must cover thinking *and*
    answer.  Adaptive thinking and ``effort`` need a 4.6 or later model;
    the pre-4.6 line, including :data:`DEFAULT_MODEL`, refuses them, so set
    ``model:`` with it.  Thinking blocks in the reply are dropped by
    :func:`_extract_text`; ``content`` is only the text answer.  When
    ``None`` neither key is sent and the request is exactly what it was
    before the setting existed.

    **``default_headers``** is sent on every request; the registry uses it
    for ``anthropic-workspace-id`` when a route sets ``workspace_id_env``.
    ``None`` or empty sends no extra header.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str = DEFAULT_MODEL,
        reasoning_effort: str | None = None,
        default_headers: Mapping[str, str] | None = None,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._default_model = default_model
        self._reasoning_effort = reasoning_effort
        self._client = client or _build_async_client(
            api_key=api_key, base_url=base_url, default_headers=default_headers
        )

    async def generate(
        self,
        *,
        messages: list[Message],
        # Accepted for ``LLMClient`` conformance and deliberately unused —
        # dropping it is the point of this method.  See the docstring.
        temperature: float = 0.3,  # noqa: ARG002
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        """Generate a completion.  ``temperature`` is accepted and **dropped**.

        The parameter stays in the signature because ``LLMClient`` is
        provider-agnostic and the OpenAI-compatible path (local Ollama, which
        is what the reference deployment runs) both accepts and honours it.
        Translating a provider-agnostic request into one provider's wire shape
        is this adapter's job — it already does the same for ``system``.

        The drop is **unconditional**, not gated on the chosen model, for two
        reasons.  Model-gating needs a roster of which models still accept a
        sampling parameter, and that roster rots on every release while being
        unfalsifiable here (``base_url`` may point at any gateway).  More
        decisively, it would not work: the parameter is *absent from the
        Anthropic Python SDK's 1.x and 2.x signatures* (2.3.0 read), so passing
        it raises ``TypeError`` in the client before any model ever sees it.  A
        branch that kept it for the models that still accept it would therefore
        be both dead and broken.  Unconditional is the only shape that is
        correct across every SDK major the extra admits and every model.

        As of 2026-09-04 the API side is: Claude 4.7 and later — Opus 4.7,
        Opus 4.8, Opus 5, Sonnet 5, Fable/Mythos 5 — return a 400 for any of
        the three set to a **non-default** value, while the 4.5/4.6 line,
        including this module's :data:`DEFAULT_MODEL`, still accepts them
        (Haiku 4.5 takes ``temperature`` or ``top_p``, not both).  So the
        finding that motivated this is not "the default model is broken"; it
        is that the SDK floor and any model bump each reach the same failure
        by a different route.  Note that every value Trellis passes is
        non-default (``0.0`` / ``0.2`` / ``0.3`` against an API default of
        ``1.0``), so the API-side rejection is reachable, not theoretical.
        """
        chosen_model = model or self._default_model
        system_text, conversation = _split_system(messages)

        # ``temperature`` is deliberately not forwarded — see the docstring.
        kwargs: dict[str, Any] = {
            "model": chosen_model,
            "messages": conversation,
            "max_tokens": max_tokens,
        }
        if system_text:
            kwargs["system"] = system_text
        if self._reasoning_effort is not None:
            # See the class docstring for the mapping and why no budget.
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self._reasoning_effort}

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


def _cache_count(usage: Any, name: str) -> int | None:
    """Read one Anthropic cache counter, preserving absence as ``None``.

    Unlike ``input_tokens`` / ``output_tokens`` above, these are **not**
    coerced through ``or 0``: ``0`` and "not reported" are different facts
    here (see :class:`~trellis.llm.types.TokenUsage`), and the ``or 0``
    idiom collapses them. An older SDK or API version that omits the field
    — or sends it as ``null`` — yields ``None``; the wire cannot separate
    those two and both mean the response carried no number.
    """
    value = getattr(usage, name, None)
    return None if value is None else int(value)


def _extract_usage(usage: Any) -> TokenUsage | None:
    """Map an Anthropic usage object to ``TokenUsage``."""
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    # ``output_tokens_details.thinking_tokens`` is included in
    # ``output_tokens``; absent (older SDK, no thinking) reads as None.
    details = getattr(usage, "output_tokens_details", None)
    return TokenUsage(
        prompt_tokens=int(input_tokens),
        completion_tokens=int(output_tokens),
        # Anthropic's ``input_tokens`` already EXCLUDES both cache
        # counters, so this sum is the uncached, standard-rate half of the
        # call and nothing else. Adding a cache read here is the one edit
        # that would make this measurement worse than not having it.
        total_tokens=int(input_tokens) + int(output_tokens),
        cache_creation_input_tokens=_cache_count(usage, "cache_creation_input_tokens"),
        cache_read_input_tokens=_cache_count(usage, "cache_read_input_tokens"),
        reasoning_tokens=_cache_count(details, "thinking_tokens"),
    )
