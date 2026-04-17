"""Tests for OpenAI provider implementations.

Tests inject a mock async client via the ``client=`` kwarg, so the real
``openai`` SDK is not required to run them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trellis.llm.protocol import EmbedderClient, LLMClient
from trellis.llm.providers.openai import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBEDDING_MODEL,
    OpenAIClient,
    OpenAIEmbedder,
    _extract_usage,
)
from trellis.llm.types import Message, TokenUsage

# -- Mock builders ---------------------------------------------------------


def _make_chat_response(
    content: str = "hi",
    model: str = "gpt-4o-mini",
    *,
    usage: tuple[int, int, int] | None = (10, 5, 15),
) -> SimpleNamespace:
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage_obj = (
        SimpleNamespace(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
        )
        if usage
        else None
    )
    return SimpleNamespace(choices=[choice], model=model, usage=usage_obj)


def _make_embedding_response(
    embeddings: list[list[float]],
    model: str = "text-embedding-3-small",
    *,
    usage: tuple[int, int, int] | None = (3, 0, 3),
) -> SimpleNamespace:
    data = [SimpleNamespace(embedding=emb) for emb in embeddings]
    usage_obj = (
        SimpleNamespace(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
        )
        if usage
        else None
    )
    return SimpleNamespace(data=data, model=model, usage=usage_obj)


def _chat_mock(response: SimpleNamespace) -> AsyncMock:
    create = AsyncMock(return_value=response)
    completions = SimpleNamespace(create=create)
    chat = SimpleNamespace(completions=completions)
    return SimpleNamespace(chat=chat), create


def _embeddings_mock(response: SimpleNamespace) -> tuple[SimpleNamespace, AsyncMock]:
    create = AsyncMock(return_value=response)
    embeddings = SimpleNamespace(create=create)
    return SimpleNamespace(embeddings=embeddings), create


# -- Tests: OpenAIClient ---------------------------------------------------


class TestOpenAIClient:
    def test_satisfies_llm_client_protocol(self) -> None:
        client_obj, _ = _chat_mock(_make_chat_response())
        c = OpenAIClient(client=client_obj)
        assert isinstance(c, LLMClient)

    async def test_generate_returns_llm_response(self) -> None:
        client_obj, _ = _chat_mock(_make_chat_response(content="hello world"))
        c = OpenAIClient(client=client_obj)
        resp = await c.generate(messages=[Message(role="user", content="hi")])
        assert resp.content == "hello world"
        assert resp.usage is not None
        assert resp.usage.total_tokens == 15

    async def test_generate_forwards_params(self) -> None:
        client_obj, create = _chat_mock(_make_chat_response())
        c = OpenAIClient(client=client_obj)
        await c.generate(
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="usr"),
            ],
            temperature=0.7,
            max_tokens=1000,
            model="gpt-4o",
        )
        call_kwargs = create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o"
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 1000
        assert call_kwargs["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ]

    async def test_uses_default_model_when_not_specified(self) -> None:
        client_obj, create = _chat_mock(_make_chat_response())
        c = OpenAIClient(client=client_obj)
        await c.generate(messages=[Message(role="user", content="hi")])
        assert create.call_args.kwargs["model"] == DEFAULT_CHAT_MODEL

    async def test_handles_null_content(self) -> None:
        client_obj, _ = _chat_mock(_make_chat_response(content=None))  # type: ignore[arg-type]
        c = OpenAIClient(client=client_obj)
        resp = await c.generate(messages=[Message(role="user", content="hi")])
        assert resp.content == ""

    async def test_missing_usage_returns_none(self) -> None:
        client_obj, _ = _chat_mock(_make_chat_response(usage=None))
        c = OpenAIClient(client=client_obj)
        resp = await c.generate(messages=[Message(role="user", content="hi")])
        assert resp.usage is None


# -- Tests: OpenAIEmbedder -------------------------------------------------


class TestOpenAIEmbedder:
    def test_satisfies_embedder_client_protocol(self) -> None:
        client_obj, _ = _embeddings_mock(_make_embedding_response([[0.1, 0.2]]))
        e = OpenAIEmbedder(client=client_obj)
        assert isinstance(e, EmbedderClient)

    async def test_embed_returns_response(self) -> None:
        client_obj, _ = _embeddings_mock(_make_embedding_response([[0.1, 0.2, 0.3]]))
        e = OpenAIEmbedder(client=client_obj)
        resp = await e.embed("hello")
        assert resp.embedding == [0.1, 0.2, 0.3]
        assert resp.model == "text-embedding-3-small"
        assert resp.usage is not None

    async def test_embed_uses_default_model(self) -> None:
        client_obj, create = _embeddings_mock(_make_embedding_response([[0.0]]))
        e = OpenAIEmbedder(client=client_obj)
        await e.embed("x")
        assert create.call_args.kwargs["model"] == DEFAULT_EMBEDDING_MODEL

    async def test_embed_custom_model(self) -> None:
        client_obj, create = _embeddings_mock(_make_embedding_response([[0.0]]))
        e = OpenAIEmbedder(client=client_obj)
        await e.embed("x", model="text-embedding-3-large")
        assert create.call_args.kwargs["model"] == "text-embedding-3-large"

    async def test_embed_batch(self) -> None:
        vectors = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        client_obj, create = _embeddings_mock(_make_embedding_response(vectors))
        e = OpenAIEmbedder(client=client_obj)
        results = await e.embed_batch(["a", "b", "c"])
        assert len(results) == 3
        assert results[0].embedding == [0.1, 0.2]
        assert results[2].embedding == [0.5, 0.6]
        # One call was made for the whole batch
        assert create.call_count == 1
        assert create.call_args.kwargs["input"] == ["a", "b", "c"]

    async def test_embed_batch_usage_on_first_only(self) -> None:
        vectors = [[0.1], [0.2]]
        client_obj, _ = _embeddings_mock(_make_embedding_response(vectors))
        e = OpenAIEmbedder(client=client_obj)
        results = await e.embed_batch(["a", "b"])
        assert results[0].usage is not None
        assert results[1].usage is None

    async def test_embed_batch_empty_input(self) -> None:
        client_obj, create = _embeddings_mock(_make_embedding_response([]))
        e = OpenAIEmbedder(client=client_obj)
        results = await e.embed_batch([])
        assert results == []
        assert create.call_count == 0


# -- Tests: _extract_usage helper ------------------------------------------


class TestExtractUsage:
    def test_none_returns_none(self) -> None:
        assert _extract_usage(None) is None

    def test_object_with_attrs(self) -> None:
        obj = SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10)
        usage = _extract_usage(obj)
        assert usage == TokenUsage(
            prompt_tokens=7, completion_tokens=3, total_tokens=10
        )

    def test_dict_input(self) -> None:
        usage = _extract_usage(
            {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
        )
        assert usage is not None
        assert usage.total_tokens == 7

    def test_missing_fields_default_to_zero(self) -> None:
        obj = SimpleNamespace()
        usage = _extract_usage(obj)
        assert usage == TokenUsage()


# -- Tests: import error handling ------------------------------------------


class TestImportGuard:
    def test_module_not_found_when_openai_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the openai SDK cannot be imported, raise a helpful error."""
        import builtins

        real_import = builtins.__import__

        msg = "No module named 'openai'"

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "openai":
                raise ModuleNotFoundError(msg)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)
        with pytest.raises(ModuleNotFoundError, match="llm-openai"):
            OpenAIClient(api_key="sk-test")
