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

    def test_cache_fields_default_to_none_not_zero(self) -> None:
        # ``None`` = the provider reported nothing. ``0`` = it reported
        # zero, which for a cache read across a stable prefix is a finding.
        # A shared default of 0 would render every non-caching provider as
        # a permanently broken cache.
        usage = TokenUsage()
        assert usage.cache_creation_input_tokens is None
        assert usage.cache_read_input_tokens is None

    def test_reported_zero_is_kept_distinct_from_absence(self) -> None:
        usage = TokenUsage(cache_creation_input_tokens=0, cache_read_input_tokens=0)
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 0
        assert usage != TokenUsage()

    def test_row_written_before_the_fields_existed_still_parses(self) -> None:
        # ``TrellisModel`` sets ``extra="forbid"``, so a new field without a
        # default would reject every historical payload. This is the reason
        # both cache fields carry one.
        usage = TokenUsage.model_validate(
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        )
        assert usage.cache_read_input_tokens is None

    def test_total_tokens_is_stored_not_derived_from_cache_fields(self) -> None:
        # The model never recomputes the total. Whatever a provider adapter
        # put there is what a consumer reads, which is what makes the
        # adapter-side exclusion rule the only place it can be broken.
        usage = TokenUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cache_read_input_tokens=9_999,
            cache_creation_input_tokens=8_888,
        )
        assert usage.total_tokens == 15


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
