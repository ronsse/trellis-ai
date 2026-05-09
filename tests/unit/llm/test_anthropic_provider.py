"""Tests for Anthropic provider implementation.

Tests inject a mock async client via the ``client=`` kwarg, so the real
``anthropic`` SDK is not required to run them.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trellis.llm.protocol import LLMClient
from trellis.llm.providers import anthropic as anthropic_provider
from trellis.llm.providers.anthropic import (
    DEFAULT_MODEL,
    AnthropicClient,
    _extract_text,
    _extract_usage,
    _split_system,
)
from trellis.llm.types import Message, TokenUsage

# -- Mock builders ---------------------------------------------------------


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _make_message_response(
    text: str = "hi",
    model: str = "claude-haiku-4-5-20251001",
    *,
    usage: tuple[int, int] | None = (10, 5),
    blocks: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    content_blocks = blocks if blocks is not None else [_text_block(text)]
    usage_obj = (
        SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1])
        if usage
        else None
    )
    return SimpleNamespace(content=content_blocks, model=model, usage=usage_obj)


def _messages_mock(response: SimpleNamespace) -> tuple[SimpleNamespace, AsyncMock]:
    create = AsyncMock(return_value=response)
    messages = SimpleNamespace(create=create)
    return SimpleNamespace(messages=messages), create


# -- Tests: AnthropicClient ------------------------------------------------


class TestAnthropicClient:
    def test_satisfies_llm_client_protocol(self) -> None:
        client_obj, _ = _messages_mock(_make_message_response())
        c = AnthropicClient(client=client_obj)
        assert isinstance(c, LLMClient)

    async def test_generate_returns_llm_response(self) -> None:
        client_obj, _ = _messages_mock(_make_message_response(text="hello world"))
        c = AnthropicClient(client=client_obj)
        resp = await c.generate(messages=[Message(role="user", content="hi")])
        assert resp.content == "hello world"
        assert resp.usage is not None
        assert resp.usage.prompt_tokens == 10
        assert resp.usage.completion_tokens == 5
        assert resp.usage.total_tokens == 15

    async def test_system_message_split_into_system_param(self) -> None:
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(client=client_obj)
        await c.generate(
            messages=[
                Message(role="system", content="You are helpful."),
                Message(role="user", content="hi"),
            ],
        )
        call_kwargs = create.call_args.kwargs
        assert call_kwargs["system"] == "You are helpful."
        assert call_kwargs["messages"] == [{"role": "user", "content": "hi"}]

    async def test_multiple_system_messages_joined(self) -> None:
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(client=client_obj)
        await c.generate(
            messages=[
                Message(role="system", content="part one"),
                Message(role="system", content="part two"),
                Message(role="user", content="hi"),
            ],
        )
        assert create.call_args.kwargs["system"] == "part one\n\npart two"

    async def test_no_system_message_omits_system_param(self) -> None:
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(client=client_obj)
        await c.generate(messages=[Message(role="user", content="hi")])
        assert "system" not in create.call_args.kwargs

    async def test_forwards_temperature_and_max_tokens(self) -> None:
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(client=client_obj)
        await c.generate(
            messages=[Message(role="user", content="hi")],
            temperature=0.9,
            max_tokens=2048,
        )
        call_kwargs = create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.9
        assert call_kwargs["max_tokens"] == 2048

    async def test_uses_default_model(self) -> None:
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(client=client_obj)
        await c.generate(messages=[Message(role="user", content="hi")])
        assert create.call_args.kwargs["model"] == DEFAULT_MODEL

    async def test_custom_model_override(self) -> None:
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(client=client_obj)
        await c.generate(
            messages=[Message(role="user", content="hi")],
            model="claude-opus-4-6",
        )
        assert create.call_args.kwargs["model"] == "claude-opus-4-6"

    async def test_concatenates_multiple_text_blocks(self) -> None:
        response = _make_message_response(
            blocks=[_text_block("part 1 "), _text_block("part 2")],
        )
        client_obj, _ = _messages_mock(response)
        c = AnthropicClient(client=client_obj)
        resp = await c.generate(messages=[Message(role="user", content="hi")])
        assert resp.content == "part 1 part 2"

    async def test_ignores_non_text_blocks(self) -> None:
        response = _make_message_response(
            blocks=[
                _text_block("visible"),
                SimpleNamespace(type="tool_use", id="tool-1"),
            ],
        )
        client_obj, _ = _messages_mock(response)
        c = AnthropicClient(client=client_obj)
        resp = await c.generate(messages=[Message(role="user", content="hi")])
        assert resp.content == "visible"


# -- Tests: helpers --------------------------------------------------------


class TestSplitSystem:
    def test_no_system_messages(self) -> None:
        system, convo = _split_system([Message(role="user", content="hi")])
        assert system == ""
        assert convo == [{"role": "user", "content": "hi"}]

    def test_preserves_conversation_order(self) -> None:
        _, convo = _split_system(
            [
                Message(role="user", content="q1"),
                Message(role="assistant", content="a1"),
                Message(role="user", content="q2"),
            ]
        )
        assert [m["content"] for m in convo] == ["q1", "a1", "q2"]


class TestExtractText:
    def test_none_content(self) -> None:
        assert _extract_text(SimpleNamespace(content=None)) == ""

    def test_empty_list(self) -> None:
        assert _extract_text(SimpleNamespace(content=[])) == ""


class TestExtractUsage:
    def test_none(self) -> None:
        assert _extract_usage(None) is None

    def test_normal(self) -> None:
        obj = SimpleNamespace(input_tokens=100, output_tokens=50)
        usage = _extract_usage(obj)
        assert usage == TokenUsage(
            prompt_tokens=100, completion_tokens=50, total_tokens=150
        )


# -- Tests: import error handling ------------------------------------------


def _block_module_import(monkeypatch: pytest.MonkeyPatch, blocked: str) -> None:
    """Make ``import <blocked>`` raise ModuleNotFoundError; pass everything else."""
    import builtins

    real_import = builtins.__import__
    msg = f"No module named '{blocked}'"

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == blocked:
            raise ModuleNotFoundError(msg)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def _install_fake_anthropic_module(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    """Install a stub ``anthropic`` module whose AsyncAnthropic captures kwargs."""
    captured: dict[str, object] = {}

    class FakeAsyncAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_module = ModuleType("anthropic")
    fake_module.AsyncAnthropic = FakeAsyncAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    return captured


class TestImportGuard:
    def test_module_not_found_when_anthropic_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the anthropic SDK cannot be imported, raise a helpful error."""
        _block_module_import(monkeypatch, "anthropic")
        with pytest.raises(ModuleNotFoundError, match="llm-anthropic"):
            AnthropicClient(api_key="sk-ant-test")


# -- Tests: error propagation ----------------------------------------------


class _FakeAPIError(Exception):
    """Stand-in for an anthropic SDK exception class."""


class TestErrorPropagation:
    """The adapter does not wrap SDK errors in trellis.errors types — it lets
    them propagate. These tests pin that contract so a future change is a
    deliberate decision, not an accident."""

    async def test_sdk_exception_propagates_unchanged(self) -> None:
        boom = _FakeAPIError("upstream 5xx")
        create = AsyncMock(side_effect=boom)
        client_obj = SimpleNamespace(messages=SimpleNamespace(create=create))
        c = AnthropicClient(client=client_obj)
        with pytest.raises(_FakeAPIError, match="upstream 5xx"):
            await c.generate(messages=[Message(role="user", content="hi")])

    async def test_timeout_exception_propagates_unchanged(self) -> None:
        create = AsyncMock(side_effect=TimeoutError("deadline"))
        client_obj = SimpleNamespace(messages=SimpleNamespace(create=create))
        c = AnthropicClient(client=client_obj)
        with pytest.raises(TimeoutError):
            await c.generate(messages=[Message(role="user", content="hi")])


# -- Tests: constructor kwargs ---------------------------------------------


class TestConstructorKwargs:
    async def test_default_model_kwarg_overrides_class_default(self) -> None:
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(
            default_model="claude-sonnet-4-6",
            client=client_obj,
        )
        await c.generate(messages=[Message(role="user", content="hi")])
        assert create.call_args.kwargs["model"] == "claude-sonnet-4-6"

    async def test_explicit_model_beats_constructor_default(self) -> None:
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(default_model="ctor-default", client=client_obj)
        await c.generate(
            messages=[Message(role="user", content="hi")],
            model="call-override",
        )
        assert create.call_args.kwargs["model"] == "call-override"

    def test_build_async_client_passes_api_key_and_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The adapter forwards ``api_key`` and ``base_url`` to AsyncAnthropic
        only when they are non-empty."""
        captured = _install_fake_anthropic_module(monkeypatch)
        AnthropicClient(api_key="sk-ant-test", base_url="https://example/api")
        assert captured == {
            "api_key": "sk-ant-test",
            "base_url": "https://example/api",
        }

    def test_build_async_client_omits_unset_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falsy ``api_key`` / ``base_url`` are not forwarded — the SDK falls
        back to its own env-based default."""
        captured = _install_fake_anthropic_module(monkeypatch)
        AnthropicClient()
        assert captured == {}


# -- Tests: helper edge cases ----------------------------------------------


class TestSplitSystemSeparator:
    def test_two_system_messages_joined_with_blank_line(self) -> None:
        system, _ = _split_system(
            [
                Message(role="system", content="A"),
                Message(role="user", content="q"),
                Message(role="system", content="B"),
            ]
        )
        # Conversation order between system messages does not matter — they
        # all collapse into ``system_text`` joined by a blank line.
        assert system == "A\n\nB"


class TestExtractTextEdgeCases:
    def test_text_block_with_empty_string_skipped(self) -> None:
        # Empty ``text=""`` is falsy, so the block is dropped entirely.
        block = SimpleNamespace(type="text", text="")
        resp = SimpleNamespace(content=[block])
        assert _extract_text(resp) == ""

    def test_block_missing_type_attr_skipped(self) -> None:
        block = SimpleNamespace(text="orphan")
        resp = SimpleNamespace(content=[block])
        assert _extract_text(resp) == ""


class TestExtractUsageEdgeCases:
    def test_zero_tokens(self) -> None:
        usage = _extract_usage(SimpleNamespace(input_tokens=0, output_tokens=0))
        assert usage == TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    def test_missing_attrs_default_to_zero(self) -> None:
        # ``getattr(..., 0)`` fallback for absent input/output token attrs.
        usage = _extract_usage(SimpleNamespace())
        assert usage == TokenUsage()

    def test_none_token_attrs_coerced_to_zero(self) -> None:
        # Defensive: SDK may yield ``None`` rather than omitting the attr.
        usage = _extract_usage(SimpleNamespace(input_tokens=None, output_tokens=None))
        assert usage == TokenUsage()


# -- Sanity: module exposes the documented surface -------------------------


def test_module_exports_default_model_constant() -> None:
    assert isinstance(anthropic_provider.DEFAULT_MODEL, str)
    assert anthropic_provider.DEFAULT_MODEL.startswith("claude-")
