"""Tests for LLMFacetClassifier."""

from __future__ import annotations

import asyncio

import pytest

from trellis.classify.classifiers.llm import LLMFacetClassifier
from trellis.classify.protocol import ClassificationContext, MergedClassification
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

    def test_maps_auto_class_to_document_form_not_content_type(self) -> None:
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

        # `architecture` is not a ContentType; filing it under that facet made
        # `to_content_tags` raise for nine of the ten enrichment values.
        assert result.tags.get("content_type") is None
        assert result.tags.get("document_form") == ["architecture"]

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


class TestEnrichmentProducesValidContentTags:
    """The regression slate that was missing.

    Every prior test asserted on the raw ``tags`` dict and never converted it,
    so a mapping that could not become a ``ContentTags`` at all looked green.
    """

    @staticmethod
    def _classify(auto_class: str, auto_tags: list[str] | None = None):
        import asyncio
        from unittest.mock import MagicMock

        from trellis_workers.enrichment.service import (
            EnrichmentResult,
            EnrichmentService,
        )

        service = MagicMock(spec=EnrichmentService)

        async def _enrich(*_a, **_k):
            return EnrichmentResult(
                auto_tags=auto_tags or [],
                auto_class=auto_class,
                tag_confidence=0.9,
                class_confidence=0.9,
                success=True,
            )

        service.enrich = _enrich
        return asyncio.run(LLMFacetClassifier(service).classify_async("body"))

    @pytest.mark.parametrize(
        "auto_class",
        [
            "meeting",
            "architecture",
            "reference",
            "journal",
            "project",
            "brainstorm",
            "documentation",
            "task-list",
            "research",
            "notes",
        ],
    )
    def test_every_enrichment_class_survives_to_content_tags(
        self, auto_class: str
    ) -> None:
        """All ten of ``DEFAULT_CLASSIFICATIONS``, not just the one that fit.

        Nine of these used to raise ``ValidationError`` — ``documentation`` is
        the single value the two vocabularies share, which is why spot checks
        could pass while the path was broken.
        """
        result = self._classify(auto_class)
        merged = MergedClassification(
            tags=result.tags,
            confidence_per_facet={"document_form": result.confidence},
            classified_by=[result.classifier_name],
        )
        tags = merged.to_content_tags()
        assert tags.custom["document_form"] == [auto_class]
        assert tags.content_type is None

    def test_out_of_band_keys_do_not_become_tags(self) -> None:
        """``_auto_*`` are scores and prose, not classifications."""
        merged = MergedClassification(
            tags={"_auto_importance": ["0.8"], "_auto_summary": ["a summary"]},
            classified_by=["llm_facet"],
        )
        assert merged.to_content_tags().custom == {}

    def test_unmodelled_facets_are_preserved_not_dropped(self) -> None:
        """Silently dropping a classifier's claim is how this went unnoticed."""
        merged = MergedClassification(
            tags={"domain": ["ops"], "some_future_facet": ["v"]},
            classified_by=["x"],
        )
        tags = merged.to_content_tags()
        assert tags.domain == ["ops"]
        assert tags.custom == {"some_future_facet": ["v"]}
