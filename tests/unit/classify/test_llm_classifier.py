"""Tests for LLMFacetClassifier."""

from __future__ import annotations

import asyncio

from trellis.classify.classifiers.llm import LLMFacetClassifier
from trellis.classify.protocol import ClassificationContext
from trellis.llm import LLMResponse, Message
from trellis_workers.enrichment.service import EnrichmentService


class FakeLLM:
    """Fake LLMClient that returns canned JSON content."""

    def __init__(self, response: str) -> None:
        self._response = response

    async def generate(
        self,
        *,
        messages: list[Message],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(content=self._response, model=model)


def _run(coro):
    return asyncio.run(coro)


class TestLLMFacetClassifier:
    """LLMFacetClassifier wraps EnrichmentService for faceted output."""

    def test_name(self) -> None:
        svc = EnrichmentService(llm=FakeLLM("{}"))
        c = LLMFacetClassifier(enrichment_service=svc)
        assert c.name == "llm_facet"

    def test_maps_auto_tags_to_domain(self) -> None:
        response = (
            '{"tags": ["data-pipeline", "infrastructure"],'
            ' "class": "architecture",'
            ' "summary": "test summary",'
            ' "importance": 0.7,'
            ' "tag_confidence": 0.85,'
            ' "class_confidence": 0.9}'
        )
        svc = EnrichmentService(llm=FakeLLM(response))
        c = LLMFacetClassifier(enrichment_service=svc)
        result = _run(c.classify_async("some content about data pipelines"))

        assert "data-pipeline" in result.tags.get("domain", [])
        assert "infrastructure" in result.tags.get("domain", [])

    def test_maps_auto_class_to_content_type(self) -> None:
        response = (
            '{"tags": ["api"],'
            ' "class": "architecture",'
            ' "summary": "arch notes",'
            ' "importance": 0.5,'
            ' "tag_confidence": 0.8,'
            ' "class_confidence": 0.9}'
        )
        svc = EnrichmentService(llm=FakeLLM(response))
        c = LLMFacetClassifier(enrichment_service=svc)
        result = _run(c.classify_async("api architecture notes"))

        assert result.tags.get("content_type") == ["architecture"]

    def test_confidence_from_enrichment(self) -> None:
        response = (
            '{"tags": ["security"],'
            ' "class": "reference",'
            ' "summary": "ref",'
            ' "importance": 0.6,'
            ' "tag_confidence": 0.7,'
            ' "class_confidence": 0.85}'
        )
        svc = EnrichmentService(llm=FakeLLM(response))
        c = LLMFacetClassifier(enrichment_service=svc)
        result = _run(c.classify_async("security reference"))

        assert result.confidence == 0.7

    def test_failed_enrichment_returns_low_confidence(self) -> None:
        class BrokenLLM:
            async def generate(
                self,
                *,
                messages: list[Message],
                temperature: float = 0.3,
                max_tokens: int = 500,
                model: str | None = None,
            ) -> LLMResponse:
                msg = "LLM unavailable"
                raise RuntimeError(msg)

        svc = EnrichmentService(llm=BrokenLLM())
        c = LLMFacetClassifier(enrichment_service=svc)
        result = _run(c.classify_async("content"))

        assert result.tags == {}
        assert result.confidence == 0.0
        assert result.needs_llm_review is True

    def test_context_title_passed_through(self) -> None:
        response = (
            '{"tags": ["testing"],'
            ' "class": "documentation",'
            ' "summary": "test docs",'
            ' "importance": 0.4,'
            ' "tag_confidence": 0.8,'
            ' "class_confidence": 0.8}'
        )
        svc = EnrichmentService(llm=FakeLLM(response))
        c = LLMFacetClassifier(enrichment_service=svc)
        ctx = ClassificationContext(title="Test Guide")
        result = _run(c.classify_async("content", context=ctx))

        assert result.classifier_name == "llm_facet"

    def test_sync_classify_delegates_to_async(self) -> None:
        """The sync classify() method wraps the async one."""
        response = (
            '{"tags": ["api"],'
            ' "class": "reference",'
            ' "summary": "ref",'
            ' "importance": 0.5,'
            ' "tag_confidence": 0.8,'
            ' "class_confidence": 0.8}'
        )
        svc = EnrichmentService(llm=FakeLLM(response))
        c = LLMFacetClassifier(enrichment_service=svc)
        result = c.classify("api reference content")

        assert "api" in result.tags.get("domain", [])

    def test_importance_preserved_in_metadata(self) -> None:
        response = (
            '{"tags": ["ml-ops"],'
            ' "class": "architecture",'
            ' "summary": "ML system design",'
            ' "importance": 0.85,'
            ' "tag_confidence": 0.9,'
            ' "class_confidence": 0.9}'
        )
        svc = EnrichmentService(llm=FakeLLM(response))
        c = LLMFacetClassifier(enrichment_service=svc)
        result = _run(c.classify_async("ML architecture"))

        assert result.tags.get("_auto_importance") == [0.85]
        assert result.tags.get("_auto_summary") == ["ML system design"]
