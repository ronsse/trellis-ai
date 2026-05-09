"""Focused tests for SourceSystemClassifier.

Combined ``test_classifiers.py`` already covers the source-system → domain
map; these tests pin down protocol conformance, retrieval-affinity
inference, and the ``no context`` edge case.
"""

from __future__ import annotations

from trellis.classify.classifiers.source_system import SourceSystemClassifier
from trellis.classify.protocol import (
    BOTH_MODES,
    ClassificationContext,
    ClassificationResult,
    Classifier,
)


class TestProtocolConformance:
    """SourceSystemClassifier conforms to the Classifier protocol."""

    def test_is_classifier_instance(self) -> None:
        assert isinstance(SourceSystemClassifier(), Classifier)

    def test_classify_returns_classification_result(self) -> None:
        ctx = ClassificationContext(source_system="dbt")
        result = SourceSystemClassifier().classify("", context=ctx)
        assert isinstance(result, ClassificationResult)
        assert result.classifier_name == "source_system"

    def test_allowed_modes_is_both(self) -> None:
        assert SourceSystemClassifier().allowed_modes == BOTH_MODES


class TestHappyPath:
    """Known source system yields the right domain + high confidence."""

    def test_dbt_source_high_confidence(self) -> None:
        ctx = ClassificationContext(source_system="dbt")
        result = SourceSystemClassifier().classify("", context=ctx)
        assert "data-pipeline" in result.tags.get("domain", [])
        assert result.confidence == 0.9
        assert result.needs_llm_review is False


class TestRetrievalAffinityInference:
    """Affinity inference covers source-system map and file-path heuristics."""

    def test_dbt_emits_pattern_and_reference(self) -> None:
        ctx = ClassificationContext(source_system="dbt")
        result = SourceSystemClassifier().classify("", context=ctx)
        affinity = result.tags.get("retrieval_affinity", [])
        assert "technical_pattern" in affinity
        assert "reference" in affinity

    def test_git_markdown_path_yields_domain_knowledge_affinity(self) -> None:
        # No source-system map for plain git, but .md path is a hint.
        ctx = ClassificationContext(source_system="git", file_path="notes/index.md")
        result = SourceSystemClassifier().classify("", context=ctx)
        assert "domain_knowledge" in result.tags.get("retrieval_affinity", [])

    def test_git_python_path_yields_technical_pattern_affinity(self) -> None:
        ctx = ClassificationContext(source_system="git", file_path="src/foo.py")
        result = SourceSystemClassifier().classify("", context=ctx)
        assert "technical_pattern" in result.tags.get("retrieval_affinity", [])

    def test_trace_entity_type_appends_operational_affinity(self) -> None:
        ctx = ClassificationContext(source_system="dbt", entity_type="trace_event")
        result = SourceSystemClassifier().classify("", context=ctx)
        assert "operational" in result.tags.get("retrieval_affinity", [])


class TestEdgeCaseNoContext:
    """Missing context flags for LLM review with low confidence."""

    def test_none_context_low_confidence(self) -> None:
        result = SourceSystemClassifier().classify("some content")
        assert result.needs_llm_review is True
        assert result.confidence == 0.3
        assert result.tags == {}
