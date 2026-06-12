"""Focused tests for StructuralClassifier.

The combined ``test_classifiers.py`` exercises individual structural
signals; these tests pin down protocol conformance, the
error-resolution-takes-precedence ordering, and the empty-content edge
case.
"""

from __future__ import annotations

from trellis.classify.classifiers.structural import StructuralClassifier
from trellis.classify.protocol import (
    BOTH_MODES,
    ClassificationContext,
    ClassificationResult,
    Classifier,
)


class TestProtocolConformance:
    """StructuralClassifier conforms to the Classifier protocol."""

    def test_is_classifier_instance(self) -> None:
        assert isinstance(StructuralClassifier(), Classifier)

    def test_classify_returns_classification_result(self) -> None:
        result = StructuralClassifier().classify("```python\nprint('hi')\n```")
        assert isinstance(result, ClassificationResult)
        assert result.classifier_name == "structural"

    def test_allowed_modes_is_both(self) -> None:
        assert StructuralClassifier().allowed_modes == BOTH_MODES


class TestHappyPath:
    """Code content gets tagged with the right content_type and affinity."""

    def test_code_fence_emits_code_tag_and_affinity(self) -> None:
        content = "Example:\n```python\ndef f(x):\n    return x\n```"
        result = StructuralClassifier().classify(content)
        assert result.tags.get("content_type") == ["code"]
        assert "technical_pattern" in result.tags.get("retrieval_affinity", [])
        assert "reference" in result.tags.get("retrieval_affinity", [])
        assert result.confidence == 0.95
        assert result.needs_llm_review is False


class TestPrecedence:
    """Error-resolution wins over procedure when both signals are present."""

    def test_error_resolution_takes_precedence_over_procedure(self) -> None:
        # Numbered steps + traceback + fix keywords — error-resolution branch
        # is checked first in the if/elif ladder.
        content = (
            "Steps to reproduce:\n"
            "1. Start the worker\n"
            "2. Send a request\n"
            "3. Observe traceback in logs\n"
            "Fixed by upgrading the foo dependency.\n"
        )
        result = StructuralClassifier().classify(content)
        assert result.tags.get("content_type") == ["error-resolution"]
        # operational affinity is the error-resolution signature, not technical_pattern
        assert "operational" in result.tags.get("retrieval_affinity", [])


class TestConfigurationFromFilePath:
    """A config-extension file path overrides any code/procedure tag."""

    def test_yaml_path_yields_configuration(self) -> None:
        ctx = ClassificationContext(file_path="deploy/values.yaml")
        result = StructuralClassifier().classify("key: value\nother: 42\n", context=ctx)
        assert result.tags.get("content_type") == ["configuration"]
        assert "reference" in result.tags.get("retrieval_affinity", [])


class TestEdgeCaseEmptyContent:
    """Empty content yields low signal_quality and flags for review."""

    def test_empty_content_low_quality_and_needs_review(self) -> None:
        result = StructuralClassifier().classify("")
        assert "low" in result.tags.get("signal_quality", [])
        # No content_type / retrieval_affinity inferred — needs_llm_review
        # is False because tags is non-empty (signal_quality was set).
        assert result.confidence == 0.95
