"""Tests for trellis.llm.protocol."""

from __future__ import annotations

from trellis.llm.protocol import CrossEncoderClient, EmbedderClient, LLMClient
from trellis.llm.types import EmbeddingResponse, LLMResponse, Message

# -- Concrete implementations for protocol conformance tests ----------------


class StubLLMClient:
    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(content="stub")


class StubEmbedder:
    async def embed(self, text: str, *, model: str | None = None) -> EmbeddingResponse:
        return EmbeddingResponse(embedding=[0.0] * 3)

    async def embed_batch(
        self, texts: list[str], *, model: str | None = None
    ) -> list[EmbeddingResponse]:
        return [EmbeddingResponse(embedding=[0.0] * 3) for _ in texts]


class StubCrossEncoder:
    async def score_pairs(
        self, query: str, candidates: list[str], *, model: str | None = None
    ) -> list[float]:
        return [0.5] * len(candidates)


# -- Tests ------------------------------------------------------------------


class TestLLMClientProtocol:
    def test_isinstance_check(self) -> None:
        assert isinstance(StubLLMClient(), LLMClient)

    def test_non_conforming_rejected(self) -> None:
        assert not isinstance(object(), LLMClient)

    async def test_generate_returns_response(self) -> None:
        client = StubLLMClient()
        resp = await client.generate(messages=[Message(role="user", content="hi")])
        assert resp.content == "stub"

    async def test_generate_with_all_params(self) -> None:
        client = StubLLMClient()
        resp = await client.generate(
            messages=[
                Message(role="system", content="You are helpful."),
                Message(role="user", content="hello"),
            ],
            temperature=0.7,
            max_tokens=1000,
            model="gpt-4o",
        )
        assert isinstance(resp, LLMResponse)


class TestEmbedderClientProtocol:
    def test_isinstance_check(self) -> None:
        assert isinstance(StubEmbedder(), EmbedderClient)

    def test_non_conforming_rejected(self) -> None:
        assert not isinstance(object(), EmbedderClient)

    async def test_embed_returns_response(self) -> None:
        client = StubEmbedder()
        resp = await client.embed("hello")
        assert len(resp.embedding) == 3

    async def test_embed_batch(self) -> None:
        client = StubEmbedder()
        results = await client.embed_batch(["a", "b", "c"])
        assert len(results) == 3


class TestCrossEncoderClientProtocol:
    def test_isinstance_check(self) -> None:
        assert isinstance(StubCrossEncoder(), CrossEncoderClient)

    def test_non_conforming_rejected(self) -> None:
        assert not isinstance(object(), CrossEncoderClient)

    async def test_score_pairs(self) -> None:
        client = StubCrossEncoder()
        scores = await client.score_pairs("query", ["a", "b"])
        assert scores == [0.5, 0.5]
