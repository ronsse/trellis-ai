"""Tests for trellis.llm.types."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trellis.llm.types import EmbeddingResponse, LLMResponse, Message, TokenUsage


class TestMessage:
    def test_valid_roles(self) -> None:
        for role in ("system", "user", "assistant"):
            msg = Message(role=role, content="hello")
            assert msg.role == role

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Message(role="tool", content="hello")

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Message(role="user", content="hello", name="test")

    def test_content_stripped(self) -> None:
        msg = Message(role="user", content="  hello  ")
        assert msg.content == "hello"


class TestTokenUsage:
    def test_defaults_to_zero(self) -> None:
        usage = TokenUsage()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0

    def test_explicit_values(self) -> None:
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        assert usage.total_tokens == 150


class TestLLMResponse:
    def test_minimal(self) -> None:
        resp = LLMResponse(content="hello")
        assert resp.content == "hello"
        assert resp.model is None
        assert resp.usage is None

    def test_with_usage(self) -> None:
        usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        resp = LLMResponse(content="hi", model="gpt-4o", usage=usage)
        assert resp.model == "gpt-4o"
        assert resp.usage.total_tokens == 15


class TestEmbeddingResponse:
    def test_minimal(self) -> None:
        resp = EmbeddingResponse(embedding=[0.1, 0.2, 0.3])
        assert len(resp.embedding) == 3
        assert resp.model is None

    def test_defaults_to_empty_list(self) -> None:
        resp = EmbeddingResponse()
        assert resp.embedding == []
