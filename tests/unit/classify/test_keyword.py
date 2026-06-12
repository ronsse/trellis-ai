"""Focused tests for KeywordDomainClassifier.

The combined ``test_classifiers.py`` covers domain dictionary lookup; these
tests pin down protocol conformance, retrieval-affinity emission, and the
``min_hits`` knob.
"""

from __future__ import annotations

from trellis.classify.classifiers.keyword import KeywordDomainClassifier
from trellis.classify.protocol import (
    BOTH_MODES,
    ClassificationContext,
    ClassificationResult,
    Classifier,
)


class TestProtocolConformance:
    """KeywordDomainClassifier satisfies the Classifier protocol."""

    def test_is_classifier_instance(self) -> None:
        assert isinstance(KeywordDomainClassifier(), Classifier)

    def test_classify_returns_classification_result(self) -> None:
        result = KeywordDomainClassifier().classify("dbt spark warehouse")
        assert isinstance(result, ClassificationResult)
        assert result.classifier_name == "keyword_domain"

    def test_allowed_modes_is_both(self) -> None:
        assert KeywordDomainClassifier().allowed_modes == BOTH_MODES


class TestHappyPath:
    """Representative content yields sensible domain tags + confidence."""

    def test_data_pipeline_content_tagged_with_high_confidence(self) -> None:
        c = KeywordDomainClassifier()
        result = c.classify(
            "The dbt model loads spark output into the snowflake warehouse "
            "via an airflow dag."
        )
        assert "data-pipeline" in result.tags.get("domain", [])
        # Confidence should be in (0.6, 0.95] for any positive match
        assert 0.6 < result.confidence <= 0.95
        assert result.needs_llm_review is False


class TestRetrievalAffinity:
    """Affinity keywords surface alongside domain tags."""

    def test_operational_affinity_from_error_keywords(self) -> None:
        c = KeywordDomainClassifier()
        # Two infrastructure hits + an operational keyword
        result = c.classify("The kubernetes cluster failure caused a deploy timeout.")
        assert "infrastructure" in result.tags.get("domain", [])
        assert "operational" in result.tags.get("retrieval_affinity", [])


class TestEdgeCaseEmptyContent:
    """Empty content flags llm_review with no matches."""

    def test_empty_content_returns_no_tags(self) -> None:
        result = KeywordDomainClassifier().classify("", context=ClassificationContext())
        assert result.tags == {}
        assert result.needs_llm_review is True
        assert result.confidence == 0.5


class TestMinHitsConfiguration:
    """The min_hits constructor argument controls match sensitivity."""

    def test_min_hits_one_lets_single_keyword_match(self) -> None:
        # With default min_hits=2, a single 'terraform' wouldn't tag.
        # Lowering to 1 should.
        c = KeywordDomainClassifier(min_hits=1)
        result = c.classify("The terraform plan looks good.")
        assert "infrastructure" in result.tags.get("domain", [])
