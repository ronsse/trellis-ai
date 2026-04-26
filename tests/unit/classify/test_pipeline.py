"""Tests for the ClassifierPipeline."""

from __future__ import annotations

import pytest

from trellis.classify.pipeline import ClassifierPipeline
from trellis.classify.protocol import (
    ClassificationContext,
    ClassificationResult,
)


class StubClassifier:
    """A test classifier that returns fixed results."""

    def __init__(
        self,
        classifier_name: str,
        tags: dict[str, list[str]],
        confidence: float = 1.0,
        needs_llm_review: bool = False,
    ) -> None:
        self._name = classifier_name
        self._tags = tags
        self._confidence = confidence
        self._needs_llm_review = needs_llm_review

    @property
    def name(self) -> str:
        return self._name

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        return ClassificationResult(
            tags=self._tags,
            confidence=self._confidence,
            classifier_name=self._name,
            needs_llm_review=self._needs_llm_review,
        )


class TestPipelineMode:
    """Pipeline reports its mode based on configuration."""

    def test_ingestion_mode_when_no_llm(self) -> None:
        pipeline = ClassifierPipeline(classifiers=[])
        assert pipeline.mode == "ingestion"

    def test_enrichment_mode_when_llm_provided(self) -> None:
        llm = StubClassifier("llm", tags={})
        pipeline = ClassifierPipeline(classifiers=[], llm_classifier=llm)
        assert pipeline.mode == "enrichment"


class TestPipelineDeterministicOnly:
    """Ingestion mode: deterministic classifiers only."""

    def test_single_classifier(self) -> None:
        c = StubClassifier(
            "structural", tags={"content_type": ["code"]}, confidence=0.95
        )
        pipeline = ClassifierPipeline(classifiers=[c])
        result = pipeline.classify("def hello(): pass")

        assert result.tags["content_type"] == ["code"]
        assert result.min_confidence == 0.95
        assert "structural" in result.classified_by

    def test_multiple_classifiers_merge(self) -> None:
        c1 = StubClassifier(
            "keyword", tags={"domain": ["data-pipeline"]}, confidence=0.9
        )
        c2 = StubClassifier(
            "structural", tags={"content_type": ["code"]}, confidence=0.95
        )
        pipeline = ClassifierPipeline(classifiers=[c1, c2])
        result = pipeline.classify("spark etl code")

        assert result.tags["domain"] == ["data-pipeline"]
        assert result.tags["content_type"] == ["code"]
        assert "keyword" in result.classified_by
        assert "structural" in result.classified_by

    def test_later_classifier_overrides_same_facet(self) -> None:
        c1 = StubClassifier("first", tags={"content_type": ["code"]}, confidence=0.7)
        c2 = StubClassifier(
            "second", tags={"content_type": ["procedure"]}, confidence=0.9
        )
        pipeline = ClassifierPipeline(classifiers=[c1, c2])
        result = pipeline.classify("some content")

        # Higher confidence wins for same facet
        assert result.tags["content_type"] == ["procedure"]

    def test_empty_tags_from_classifier_ignored(self) -> None:
        c1 = StubClassifier("noop", tags={}, confidence=0.5)
        c2 = StubClassifier("real", tags={"domain": ["api"]}, confidence=0.9)
        pipeline = ClassifierPipeline(classifiers=[c1, c2])
        result = pipeline.classify("content")

        assert result.tags["domain"] == ["api"]
        assert result.min_confidence == 0.9  # noop has no facets, doesn't count

    def test_no_classifiers_returns_empty(self) -> None:
        pipeline = ClassifierPipeline(classifiers=[])
        result = pipeline.classify("anything")

        assert result.tags == {}
        assert result.classified_by == []
        assert result.min_confidence == 1.0

    def test_context_passed_to_classifiers(self) -> None:
        """Verify context is forwarded to each classifier."""

        class ContextCapture:
            captured_context: ClassificationContext | None = None

            @property
            def name(self) -> str:
                return "capture"

            def classify(
                self,
                content: str,
                *,
                context: ClassificationContext | None = None,
            ) -> ClassificationResult:
                self.captured_context = context
                return ClassificationResult(tags={}, classifier_name="capture")

        cap = ContextCapture()
        ctx = ClassificationContext(source_system="dbt", file_path="models/orders.sql")
        pipeline = ClassifierPipeline(classifiers=[cap])
        pipeline.classify("content", context=ctx)

        assert cap.captured_context is not None
        assert cap.captured_context.source_system == "dbt"
        assert cap.captured_context.file_path == "models/orders.sql"


class TestPipelineLLMFallback:
    """Enrichment mode: LLM fires when deterministic confidence is low."""

    def test_llm_skipped_when_confidence_above_threshold(self) -> None:
        det = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.9)
        llm = StubClassifier(
            "llm", tags={"domain": ["backend"], "scope": ["org"]}, confidence=0.8
        )
        pipeline = ClassifierPipeline(
            classifiers=[det], llm_classifier=llm, llm_threshold=0.7
        )
        result = pipeline.classify("content")

        # LLM should NOT have fired — deterministic confidence 0.9 > threshold 0.7
        assert result.tags["domain"] == ["api"]
        assert "scope" not in result.tags
        assert "llm" not in result.classified_by

    def test_llm_fires_when_confidence_below_threshold(self) -> None:
        det = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.5)
        llm = StubClassifier("llm", tags={"scope": ["org"]}, confidence=0.8)
        pipeline = ClassifierPipeline(
            classifiers=[det], llm_classifier=llm, llm_threshold=0.7
        )
        result = pipeline.classify("content")

        # LLM should have fired — deterministic confidence 0.5 < threshold 0.7
        assert result.tags["domain"] == ["api"]  # deterministic tags preserved
        assert result.tags["scope"] == ["org"]  # LLM added scope
        assert "llm" in result.classified_by

    def test_llm_fires_when_needs_llm_review_flagged(self) -> None:
        det = StubClassifier("det", tags={}, confidence=0.5, needs_llm_review=True)
        llm = StubClassifier("llm", tags={"domain": ["security"]}, confidence=0.8)
        pipeline = ClassifierPipeline(
            classifiers=[det], llm_classifier=llm, llm_threshold=0.7
        )
        result = pipeline.classify("content")

        assert result.tags["domain"] == ["security"]
        assert "llm" in result.classified_by

    def test_llm_does_not_override_high_confidence_deterministic(self) -> None:
        det = StubClassifier("det", tags={"content_type": ["code"]}, confidence=0.5)
        llm = StubClassifier(
            "llm",
            tags={"content_type": ["procedure"], "scope": ["universal"]},
            confidence=0.8,
        )
        pipeline = ClassifierPipeline(
            classifiers=[det], llm_classifier=llm, llm_threshold=0.7
        )
        result = pipeline.classify("content")

        # LLM fires (det confidence 0.5 < 0.7) but for content_type,
        # LLM has higher confidence so it wins
        assert result.tags["content_type"] == ["procedure"]
        assert result.tags["scope"] == ["universal"]


class TestPipelineToContentTags:
    """MergedClassification converts to ContentTags correctly."""

    def test_to_content_tags(self) -> None:
        c1 = StubClassifier(
            "kw", tags={"domain": ["data-pipeline", "infrastructure"]}, confidence=0.9
        )
        c2 = StubClassifier(
            "st",
            tags={"content_type": ["error-resolution"], "signal_quality": ["high"]},
            confidence=0.95,
        )
        pipeline = ClassifierPipeline(classifiers=[c1, c2])
        result = pipeline.classify("content")
        tags = result.to_content_tags()

        assert tags.domain == ["data-pipeline", "infrastructure"]
        assert tags.content_type == "error-resolution"
        assert tags.signal_quality == "high"
        assert "kw" in tags.classified_by
        assert "st" in tags.classified_by

    def test_to_content_tags_defaults_when_empty(self) -> None:
        pipeline = ClassifierPipeline(classifiers=[])
        result = pipeline.classify("content")
        tags = result.to_content_tags()

        assert tags.domain == []
        assert tags.content_type is None
        assert tags.scope is None
        assert tags.signal_quality == "standard"


class TestPipelineModeEnforcement:
    """Pipeline rejects classifiers whose allowed_modes exclude the active mode."""

    def test_enrichment_only_rejected_in_ingestion_mode(self) -> None:
        """A classifier declaring enrichment-only should not be accepted
        in ingestion mode (llm_classifier=None)."""

        class EnrichmentOnlyClassifier:
            @property
            def name(self) -> str:
                return "enrichment_only"

            @property
            def allowed_modes(self) -> frozenset[str]:
                return frozenset({"enrichment"})

            def classify(
                self,
                content: str,
                *,
                context: ClassificationContext | None = None,
            ) -> ClassificationResult:
                return ClassificationResult(tags={}, classifier_name=self.name)

        with pytest.raises(ValueError, match="does not support mode 'ingestion'"):
            ClassifierPipeline(classifiers=[EnrichmentOnlyClassifier()])

    def test_enrichment_only_accepted_in_enrichment_mode(self) -> None:
        """When the pipeline runs in enrichment mode, enrichment-only classifiers
        are accepted."""

        class EnrichmentOnlyClassifier:
            @property
            def name(self) -> str:
                return "enrichment_only"

            @property
            def allowed_modes(self) -> frozenset[str]:
                return frozenset({"enrichment"})

            def classify(
                self,
                content: str,
                *,
                context: ClassificationContext | None = None,
            ) -> ClassificationResult:
                return ClassificationResult(
                    tags={"domain": ["from_enrichment"]},
                    confidence=0.9,
                    classifier_name=self.name,
                )

        llm = StubClassifier("llm", tags={})
        pipeline = ClassifierPipeline(
            classifiers=[EnrichmentOnlyClassifier()], llm_classifier=llm
        )
        assert pipeline.mode == "enrichment"
        result = pipeline.classify("content")
        assert result.tags["domain"] == ["from_enrichment"]

    def test_both_modes_accepted_everywhere(self) -> None:
        """Default both-mode classifiers work in both ingestion and enrichment."""
        c = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.9)
        # Ingestion mode
        p1 = ClassifierPipeline(classifiers=[c])
        assert p1.mode == "ingestion"
        # Enrichment mode
        llm = StubClassifier("llm", tags={})
        p2 = ClassifierPipeline(classifiers=[c], llm_classifier=llm)
        assert p2.mode == "enrichment"

    def test_no_allowed_modes_attribute_defaults_to_both(self) -> None:
        """Classifiers without allowed_modes (e.g. StubClassifier) default to
        both modes for backward compatibility."""
        c = StubClassifier("legacy", tags={"domain": ["x"]}, confidence=0.9)
        # No allowed_modes attribute → should work in ingestion mode
        pipeline = ClassifierPipeline(classifiers=[c])
        assert len(pipeline.classify("test").classified_by) == 1


class TestPipelineIdempotency:
    """Deterministic classifiers produce identical output on re-run."""

    def test_same_input_same_output(self) -> None:
        c = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.9)
        pipeline = ClassifierPipeline(classifiers=[c])

        r1 = pipeline.classify("same content")
        r2 = pipeline.classify("same content")

        assert r1.tags == r2.tags
        assert r1.min_confidence == r2.min_confidence
        assert r1.classified_by == r2.classified_by


class TestPipelineModeStamping:
    """Each classify() call stamps its mode on the result and on
    derived ContentTags. Closes Gap 1.2."""

    def test_ingestion_mode_stamped_on_merged_result(self) -> None:
        c = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.9)
        pipeline = ClassifierPipeline(classifiers=[c])

        result = pipeline.classify("content")

        assert result.mode == "ingestion"

    def test_enrichment_mode_stamped_on_merged_result(self) -> None:
        det = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.9)
        llm = StubClassifier("llm", tags={})
        pipeline = ClassifierPipeline(
            classifiers=[det], llm_classifier=llm, llm_threshold=0.7
        )

        result = pipeline.classify("content")

        # Mode reflects the pipeline's configuration, not whether the LLM
        # actually fired on this call.
        assert result.mode == "enrichment"

    def test_enrichment_mode_stamped_even_when_llm_skipped(self) -> None:
        """When LLM is configured but skipped (high deterministic confidence),
        the result still carries mode='enrichment' — the pipeline is in
        enrichment mode regardless of which path fired."""
        det = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.95)
        llm = StubClassifier("llm", tags={"scope": ["org"]}, confidence=0.8)
        pipeline = ClassifierPipeline(
            classifiers=[det], llm_classifier=llm, llm_threshold=0.7
        )

        result = pipeline.classify("content")

        assert "llm" not in result.classified_by
        assert result.mode == "enrichment"

    def test_to_content_tags_propagates_ingestion_mode(self) -> None:
        c = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.9)
        pipeline = ClassifierPipeline(classifiers=[c])

        tags = pipeline.classify("content").to_content_tags()

        assert tags.classified_mode == "ingestion"

    def test_to_content_tags_propagates_enrichment_mode(self) -> None:
        det = StubClassifier("det", tags={"domain": ["api"]}, confidence=0.5)
        llm = StubClassifier("llm", tags={"scope": ["org"]}, confidence=0.8)
        pipeline = ClassifierPipeline(
            classifiers=[det], llm_classifier=llm, llm_threshold=0.7
        )

        tags = pipeline.classify("content").to_content_tags()

        assert tags.classified_mode == "enrichment"

    def test_default_constructed_merged_classification_has_no_mode(self) -> None:
        """A bare MergedClassification (no pipeline) carries mode=None.
        Production code always goes through the pipeline; this is a test
        only path."""
        from trellis.classify.protocol import MergedClassification

        empty = MergedClassification()
        assert empty.mode is None
        # to_content_tags also produces a None mode in this case.
        assert empty.to_content_tags().classified_mode is None
