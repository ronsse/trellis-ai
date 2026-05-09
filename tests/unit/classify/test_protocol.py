"""Tests for the Classifier protocol and its supporting dataclasses.

The combined ``test_classifiers.py`` exercises concrete classifiers; these
tests focus on the protocol module itself: dataclass defaults, the
``Classifier`` runtime-checkable protocol, and the
``MergedClassification.to_content_tags`` mapping.
"""

from __future__ import annotations

from datetime import datetime

from trellis.classify.protocol import (
    BOTH_MODES,
    ENRICHMENT_ONLY,
    ClassificationContext,
    ClassificationResult,
    Classifier,
    MergedClassification,
)
from trellis.schemas.classification import ContentTags


class _OkClassifier:
    """Minimal valid Classifier — used for protocol conformance checks."""

    @property
    def name(self) -> str:
        return "ok"

    @property
    def allowed_modes(self) -> frozenset[str]:
        return BOTH_MODES

    def classify(
        self,
        content: str,
        *,
        context: ClassificationContext | None = None,
    ) -> ClassificationResult:
        return ClassificationResult(
            tags={"domain": ["api"]},
            confidence=0.9,
            classifier_name=self.name,
        )


class TestModeConstants:
    """Canonical mode sets are frozen and well-formed."""

    def test_both_modes_contains_ingestion_and_enrichment(self) -> None:
        assert frozenset({"ingestion", "enrichment"}) == BOTH_MODES

    def test_enrichment_only_is_singleton(self) -> None:
        assert frozenset({"enrichment"}) == ENRICHMENT_ONLY

    def test_mode_sets_are_frozen(self) -> None:
        # frozenset is hashable; mutable set would not be
        assert isinstance(BOTH_MODES, frozenset)
        assert isinstance(ENRICHMENT_ONLY, frozenset)


class TestClassifierProtocol:
    """``Classifier`` is runtime-checkable and accepts conforming classes."""

    def test_conforming_class_is_instance(self) -> None:
        # Happy path: a class with name/allowed_modes/classify is a Classifier.
        assert isinstance(_OkClassifier(), Classifier)

    def test_classify_returns_classification_result(self) -> None:
        result = _OkClassifier().classify("hello")
        assert isinstance(result, ClassificationResult)
        assert result.classifier_name == "ok"
        assert result.tags == {"domain": ["api"]}


class TestClassificationContextDefaults:
    """ClassificationContext defaults are safe (no mutable shared state)."""

    def test_default_strings_are_empty(self) -> None:
        ctx = ClassificationContext()
        assert ctx.title == ""
        assert ctx.source_system == ""
        assert ctx.file_path == ""
        assert ctx.entity_type == ""
        assert ctx.node_id == ""

    def test_default_metadata_is_independent_dict(self) -> None:
        # Edge case: dataclass field(default_factory=dict) must not share state.
        a = ClassificationContext()
        b = ClassificationContext()
        a.existing_metadata["k"] = "v"
        assert b.existing_metadata == {}


class TestClassificationResultDefaults:
    """ClassificationResult default values match the expected schema."""

    def test_defaults(self) -> None:
        r = ClassificationResult()
        assert r.tags == {}
        assert r.confidence == 1.0
        assert r.classifier_name == ""
        assert r.needs_llm_review is False


class TestMergedClassificationMinConfidence:
    """min_confidence picks the lowest per-facet confidence, or 1.0 if empty."""

    def test_returns_min_when_populated(self) -> None:
        m = MergedClassification(
            confidence_per_facet={"domain": 0.7, "content_type": 0.4, "scope": 0.9},
        )
        assert m.min_confidence == 0.4

    def test_returns_one_when_empty(self) -> None:
        # Edge case: empty dict yields 1.0 sentinel rather than raising.
        m = MergedClassification()
        assert m.min_confidence == 1.0


class TestMergedClassificationToContentTags:
    """to_content_tags maps the merged dict into a ContentTags Pydantic model."""

    def test_happy_path_maps_all_facets(self) -> None:
        merged = MergedClassification(
            tags={
                "domain": ["api", "infrastructure"],
                "content_type": ["code"],
                "scope": ["project"],
                "signal_quality": ["high"],
                "retrieval_affinity": ["technical_pattern", "reference"],
            },
            classified_by=["structural", "keyword_domain"],
            mode="ingestion",
        )
        ct = merged.to_content_tags()
        assert isinstance(ct, ContentTags)
        assert ct.domain == ["api", "infrastructure"]
        assert ct.content_type == "code"
        assert ct.scope == "project"
        assert ct.signal_quality == "high"
        assert ct.retrieval_affinity == ["technical_pattern", "reference"]
        assert ct.classified_by == ["structural", "keyword_domain"]
        assert ct.classified_mode == "ingestion"
        assert isinstance(ct.classified_at, datetime)

    def test_empty_signal_quality_defaults_to_standard(self) -> None:
        # Edge case: when the merged dict has no signal_quality entry, the
        # ContentTags default of "standard" should win.
        merged = MergedClassification(tags={"domain": ["api"]})
        ct = merged.to_content_tags()
        assert ct.signal_quality == "standard"
        assert ct.content_type is None
        assert ct.scope is None
        assert ct.retrieval_affinity == []
