"""Tests for Anthropic provider implementation.

Tests inject a mock async client via the ``client=`` kwarg, so the real
``anthropic`` SDK is not required to run them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trellis.llm.protocol import LLMClient
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


class TestImportGuard:
    def test_module_not_found_when_anthropic_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the anthropic SDK cannot be imported, raise a helpful error."""
        import builtins

        real_import = builtins.__import__

        msg = "No module named 'anthropic'"

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "anthropic":
                raise ModuleNotFoundError(msg)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)
        with pytest.raises(ModuleNotFoundError, match="llm-anthropic"):
            AnthropicClient(api_key="sk-ant-test")
