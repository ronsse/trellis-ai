"""Focused tests for LLMFacetClassifier.

The existing ``test_llm_classifier.py`` exercises the FakeLLM-backed
EnrichmentService path end-to-end. These tests use ``MagicMock`` to focus
on protocol conformance, ``allowed_modes``, and the EnrichmentService
adapter contract — without spinning up FakeLLM machinery.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from trellis.classify.classifiers.llm import LLMFacetClassifier
from trellis.classify.protocol import (
    ENRICHMENT_ONLY,
    ClassificationContext,
    ClassificationResult,
    Classifier,
)
from trellis_workers.enrichment.service import EnrichmentResult, EnrichmentService


def _success_result(**overrides) -> EnrichmentResult:
    base: dict[str, object] = {
        "auto_tags": ["api"],
        "auto_class": "reference",
        "auto_summary": "ref summary",
        "auto_importance": 0.5,
        "tag_confidence": 0.8,
        "class_confidence": 0.85,
    }
    base.update(overrides)
    return EnrichmentResult(success=True, **base)


def _failure_result() -> EnrichmentResult:
    return EnrichmentResult(success=False, error="boom")


class TestProtocolConformance:
    """LLMFacetClassifier conforms to the Classifier protocol."""

    def test_is_classifier_instance(self) -> None:
        svc = MagicMock(spec=EnrichmentService)
        c = LLMFacetClassifier(enrichment_service=svc)
        assert isinstance(c, Classifier)
        assert c.name == "llm_facet"

    def test_allowed_modes_is_enrichment_only(self) -> None:
        # LLM classifiers must NEVER run during ingestion.
        svc = MagicMock(spec=EnrichmentService)
        c = LLMFacetClassifier(enrichment_service=svc)
        assert c.allowed_modes == ENRICHMENT_ONLY


class TestHappyPath:
    """A successful enrichment maps cleanly into the ClassificationResult."""

    def test_classify_async_maps_facets_and_confidence(self) -> None:
        svc = MagicMock(spec=EnrichmentService)
        svc.enrich = AsyncMock(return_value=_success_result(auto_tags=["security"]))

        c = LLMFacetClassifier(enrichment_service=svc)
        ctx = ClassificationContext(title="Auth Notes")
        result = asyncio.run(c.classify_async("oauth flow", context=ctx))

        assert isinstance(result, ClassificationResult)
        assert result.tags["domain"] == ["security"]
        assert result.tags["content_type"] == ["reference"]
        # confidence = min(tag_confidence, class_confidence) = min(0.8, 0.85)
        assert result.confidence == 0.8
        assert result.classifier_name == "llm_facet"
        # title threaded through to the enrichment service
        svc.enrich.assert_awaited_once_with("oauth flow", title="Auth Notes")


class TestEdgeCaseEnrichmentFailure:
    """When the underlying EnrichmentService fails, return a 'review' result."""

    def test_failed_enrichment_returns_zero_confidence_review(self) -> None:
        svc = MagicMock(spec=EnrichmentService)
        svc.enrich = AsyncMock(return_value=_failure_result())

        c = LLMFacetClassifier(enrichment_service=svc)
        result = asyncio.run(c.classify_async("anything"))

        assert result.tags == {}
        assert result.confidence == 0.0
        assert result.needs_llm_review is True
        assert result.classifier_name == "llm_facet"
