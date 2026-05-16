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


class TestClassificationDegradedTelemetry:
    """When the enrichment upstream fails AND an ``event_log`` is wired,
    the classifier emits a ``CLASSIFICATION_DEGRADED`` event with the
    documented payload shape so analyzers can correlate the degradation
    with the upstream ``EXTRACTION_FAILED`` event by timestamp +
    ``subject_entity_id``.

    Without an event_log the classifier silently degrades (matches the
    optional-event-log pattern across the codebase).
    """

    def test_failure_emits_classification_degraded(self, tmp_path) -> None:
        from pathlib import Path

        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        log = SQLiteEventLog(Path(tmp_path) / "events.db")
        try:
            svc = MagicMock(spec=EnrichmentService)
            svc.enrich = AsyncMock(return_value=_failure_result())

            c = LLMFacetClassifier(enrichment_service=svc, event_log=log)
            ctx = ClassificationContext(node_id="node-42", title="Auth Notes")
            result = asyncio.run(c.classify_async("anything", context=ctx))

            # Sentinel result still returned — telemetry is additive.
            assert result.needs_llm_review is True
            assert result.confidence == 0.0
            assert result.classifier_name == "llm_facet"

            events = log.get_events(event_type=EventType.CLASSIFICATION_DEGRADED)
            assert len(events) == 1
            event = events[0]
            assert event.source == "llm_facet"
            assert event.entity_id == "node-42"
            payload = event.payload
            assert payload["classifier_id"] == "llm_facet"
            assert payload["upstream_failure_kind"] == "enrichment_failure"
            assert payload["subject_entity_id"] == "node-42"
            assert payload["degraded_to"] == "needs_llm_review"
        finally:
            log.close()

    def test_failure_without_event_log_is_silent(self) -> None:
        """No event_log => no event. Mirrors EnrichmentService's
        optional-telemetry contract."""

        svc = MagicMock(spec=EnrichmentService)
        svc.enrich = AsyncMock(return_value=_failure_result())

        # Default constructor — no event_log wired.
        c = LLMFacetClassifier(enrichment_service=svc)
        result = asyncio.run(c.classify_async("anything"))

        # Still degrades — telemetry is additive, not load-bearing.
        assert result.needs_llm_review is True
        assert result.confidence == 0.0

    def test_failure_without_context_records_none_subject(self, tmp_path) -> None:
        """``subject_entity_id`` falls back to ``None`` when context isn't
        supplied — documented in the EventType docstring."""

        from pathlib import Path

        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        log = SQLiteEventLog(Path(tmp_path) / "events.db")
        try:
            svc = MagicMock(spec=EnrichmentService)
            svc.enrich = AsyncMock(return_value=_failure_result())

            c = LLMFacetClassifier(enrichment_service=svc, event_log=log)
            asyncio.run(c.classify_async("anything"))

            events = log.get_events(event_type=EventType.CLASSIFICATION_DEGRADED)
            assert len(events) == 1
            assert events[0].payload["subject_entity_id"] is None
            assert events[0].entity_id is None
        finally:
            log.close()

    def test_success_does_not_emit(self, tmp_path) -> None:
        """Happy path never emits — zero noise for consumers that opt in."""

        from pathlib import Path

        from trellis.stores.base.event_log import EventType
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        log = SQLiteEventLog(Path(tmp_path) / "events.db")
        try:
            svc = MagicMock(spec=EnrichmentService)
            svc.enrich = AsyncMock(return_value=_success_result())

            c = LLMFacetClassifier(enrichment_service=svc, event_log=log)
            result = asyncio.run(c.classify_async("anything"))
            assert result.needs_llm_review is False

            events = log.get_events(event_type=EventType.CLASSIFICATION_DEGRADED)
            assert events == []
        finally:
            log.close()
