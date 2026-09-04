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
    UNSUPPORTED_SAMPLING_PARAMS,
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
    model: str = DEFAULT_MODEL,
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

    async def test_forwards_max_tokens_but_not_temperature(self) -> None:
        """``max_tokens`` rides; ``temperature`` is dropped (#500)."""
        client_obj, create = _messages_mock(_make_message_response())
        c = AnthropicClient(client=client_obj)
        await c.generate(
            messages=[Message(role="user", content="hi")],
            temperature=0.9,
            max_tokens=2048,
        )
        call_kwargs = create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 2048
        assert "temperature" not in call_kwargs

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


# -- Tests: no sampling parameter reaches the Anthropic API ----------------
#
# This is the load-bearing claim of #500, so it is pinned *at the boundary*
# (the kwargs handed to ``messages.create``) rather than one layer in.
#
# An ``AsyncMock`` accepts any kwargs, so a test that merely asserts "we
# called create" cannot see that we passed a parameter the real SDK would
# reject.  ``_StrictSdkMessages`` closes that gap: its ``create`` declares the
# keyword-only parameters the *real* ``anthropic`` 1.x signature exposes for
# the calls this adapter makes, and **no ``**kwargs`` catch-all** — so any
# extra keyword is a ``TypeError`` raised by Python itself, exactly as the
# real SDK raises one.  The real SDK is never importable in this suite (it is
# an optional extra nothing installs), which is precisely why the fake has to
# model the signature rather than swallow everything.
#
# If the adapter ever legitimately grows a new request parameter (``system``
# is the only optional one today), this fake must be widened deliberately.
# That friction is the point.


class _StrictSdkMessages:
    """A ``messages`` namespace whose ``create`` rejects unknown kwargs.

    Mirrors the ``anthropic`` 1.x signature for the subset of parameters
    ``AnthropicClient`` uses.  ``temperature`` / ``top_p`` / ``top_k`` are
    absent from 1.x, so they are absent here.
    """

    def __init__(self, response: SimpleNamespace) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        system: str | None = None,
    ) -> SimpleNamespace:
        recorded: dict[str, object] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system is not None:
            recorded["system"] = system
        self.calls.append(recorded)
        return self._response


def _strict_client(
    response: SimpleNamespace | None = None,
) -> tuple[SimpleNamespace, _StrictSdkMessages]:
    msgs = _StrictSdkMessages(response or _make_message_response())
    return SimpleNamespace(messages=msgs), msgs


class TestStrictFakeIsNotVacuous:
    """The fake must actually be able to fail — otherwise it proves nothing."""

    async def test_unknown_kwarg_raises_typeerror(self) -> None:
        _, msgs = _strict_client()
        with pytest.raises(TypeError):
            await msgs.create(  # type: ignore[call-arg]
                model="m",
                messages=[],
                max_tokens=1,
                temperature=0.3,
            )

    async def test_expected_kwargs_are_accepted(self) -> None:
        _, msgs = _strict_client()
        await msgs.create(model="m", messages=[], max_tokens=1, system="s")
        assert msgs.calls == [
            {"model": "m", "messages": [], "max_tokens": 1, "system": "s"}
        ]


class TestNoSamplingParamsReachTheApi:
    @pytest.mark.parametrize("temperature", [0.0, 0.2, 0.3, 0.9, 1.0])
    async def test_generate_survives_a_signature_without_temperature(
        self, temperature: float
    ) -> None:
        """The call itself must not raise against an SDK-shaped signature.

        Every temperature the repo actually passes is covered: ``0.0``
        (``distill``, ``reconcile``), ``0.2`` (``extract.llm``), ``0.3`` (the
        protocol default, also ``EnrichmentService``).
        """
        client_obj, msgs = _strict_client()
        resp = await AnthropicClient(client=client_obj).generate(
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="hi"),
            ],
            temperature=temperature,
            max_tokens=321,
        )
        assert resp.content == "hi"
        assert msgs.calls[0]["max_tokens"] == 321
        assert msgs.calls[0]["system"] == "sys"

    async def test_generate_survives_without_a_system_message(self) -> None:
        client_obj, msgs = _strict_client()
        await AnthropicClient(client=client_obj).generate(
            messages=[Message(role="user", content="hi")],
            temperature=0.0,
        )
        assert "system" not in msgs.calls[0]

    @pytest.mark.parametrize("param", UNSUPPORTED_SAMPLING_PARAMS)
    async def test_named_sampling_param_is_absent_from_the_request(
        self, param: str
    ) -> None:
        """Derived from the module's own roster, so extending it extends this."""
        client_obj, create = _messages_mock(_make_message_response())
        await AnthropicClient(client=client_obj).generate(
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="hi"),
            ],
            temperature=0.9,
        )
        assert param not in create.call_args.kwargs

    async def test_default_call_sends_exactly_the_expected_keys(self) -> None:
        """Pins the whole request shape, so the invariant cannot be satisfied
        by an adapter that stopped sending everything."""
        client_obj, create = _messages_mock(_make_message_response())
        await AnthropicClient(client=client_obj).generate(
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="hi"),
            ],
        )
        assert set(create.call_args.kwargs) == {
            "model",
            "messages",
            "max_tokens",
            "system",
        }

    def test_roster_names_every_removed_sampling_param(self) -> None:
        assert set(UNSUPPORTED_SAMPLING_PARAMS) == {
            "temperature",
            "top_p",
            "top_k",
        }


class TestDefaultModelIsThePinnedSnapshot:
    """#500 correction: the dated form is Haiku 4.5's *Claude API ID*, and
    ``claude-haiku-4-5`` is the alias that resolves to it — so this is a
    deliberate pin, not a stale snapshot of a modern alias."""

    def test_default_model_is_the_dated_claude_api_id(self) -> None:
        assert DEFAULT_MODEL == "claude-haiku-4-5-20251001"
