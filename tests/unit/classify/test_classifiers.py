"""Tests for deterministic classifiers."""

from __future__ import annotations

from trellis.classify.classifiers.keyword import KeywordDomainClassifier
from trellis.classify.classifiers.source_system import SourceSystemClassifier
from trellis.classify.classifiers.structural import StructuralClassifier
from trellis.classify.protocol import ClassificationContext


class TestStructuralClassifier:
    """StructuralClassifier detects content shape from structure."""

    def setup_method(self) -> None:
        self.classifier = StructuralClassifier()

    def test_name(self) -> None:
        assert self.classifier.name == "structural"

    def test_detects_code_fences(self) -> None:
        content = "Here is some code:\n```python\ndef hello():\n    pass\n```"
        result = self.classifier.classify(content)
        assert "content_type" in result.tags
        assert "code" in result.tags["content_type"]

    def test_detects_function_definitions(self) -> None:
        content = (
            "def process_data(records):\n"
            "    for r in records:\n"
            "        yield transform(r)"
        )
        result = self.classifier.classify(content)
        assert "code" in result.tags.get("content_type", [])

    def test_detects_import_statements(self) -> None:
        content = "from datetime import datetime\nimport json\n\ndef main(): pass"
        result = self.classifier.classify(content)
        assert "code" in result.tags.get("content_type", [])

    def test_detects_class_definitions(self) -> None:
        content = "class UserService:\n    def __init__(self):\n        pass"
        result = self.classifier.classify(content)
        assert "code" in result.tags.get("content_type", [])

    def test_detects_procedure_numbered_steps(self) -> None:
        content = (
            "1. Stop the service\n"
            "2. Run the migration\n"
            "3. Restart the service\n"
            "4. Verify health check\n"
        )
        result = self.classifier.classify(content)
        assert "procedure" in result.tags.get("content_type", [])

    def test_short_numbered_list_not_procedure(self) -> None:
        """A two-line numbered list with few lines isn't a procedure."""
        content = "1. Item one\n2. Item two"
        result = self.classifier.classify(content)
        assert "procedure" not in result.tags.get("content_type", [])

    def test_detects_error_resolution(self) -> None:
        content = (
            "Got a Traceback when running deploy:\n"
            "  File 'app.py', line 42\n"
            "ImportError: No module named 'foo'\n\n"
            "Fixed by adding foo to requirements.txt"
        )
        result = self.classifier.classify(content)
        assert "error-resolution" in result.tags.get("content_type", [])

    def test_error_without_fix_not_error_resolution(self) -> None:
        """Error keywords alone don't trigger error-resolution."""
        content = "Got an error: connection refused"
        result = self.classifier.classify(content)
        assert "error-resolution" not in result.tags.get("content_type", [])

    def test_detects_configuration_from_file_path(self) -> None:
        ctx = ClassificationContext(file_path="config/settings.yaml")
        result = self.classifier.classify("key: value", context=ctx)
        assert "configuration" in result.tags.get("content_type", [])

    def test_configuration_from_toml_path(self) -> None:
        ctx = ClassificationContext(file_path="pyproject.toml")
        result = self.classifier.classify("", context=ctx)
        assert "configuration" in result.tags.get("content_type", [])

    def test_low_signal_quality_for_short_content(self) -> None:
        result = self.classifier.classify("ok")
        assert "low" in result.tags.get("signal_quality", [])

    def test_normal_content_no_signal_quality_tag(self) -> None:
        result = self.classifier.classify(
            "This is a normal length piece of content with enough detail."
        )
        assert "signal_quality" not in result.tags

    def test_empty_content_is_low_quality(self) -> None:
        result = self.classifier.classify("")
        assert "low" in result.tags.get("signal_quality", [])

    def test_confidence_is_high_for_structural(self) -> None:
        content = "```python\nprint('hello')\n```"
        result = self.classifier.classify(content)
        assert result.confidence >= 0.9

    def test_no_context_still_works(self) -> None:
        result = self.classifier.classify(
            "just some plain text with enough words for quality"
        )
        assert result.classifier_name == "structural"


class TestKeywordDomainClassifier:
    """KeywordDomainClassifier maps keyword dictionaries to domain tags."""

    def setup_method(self) -> None:
        self.classifier = KeywordDomainClassifier()

    def test_name(self) -> None:
        assert self.classifier.name == "keyword_domain"

    def test_detects_data_pipeline_domain(self) -> None:
        content = (
            "This dbt model transforms raw data using a spark job in the warehouse."
        )
        result = self.classifier.classify(content)
        assert "data-pipeline" in result.tags.get("domain", [])

    def test_detects_infrastructure_domain(self) -> None:
        content = "Deployed the kubernetes cluster using terraform in the VPC."
        result = self.classifier.classify(content)
        assert "infrastructure" in result.tags.get("domain", [])

    def test_detects_api_domain(self) -> None:
        content = "Added a new REST endpoint to handle GraphQL requests and responses."
        result = self.classifier.classify(content)
        assert "api" in result.tags.get("domain", [])

    def test_detects_security_domain(self) -> None:
        content = "Updated the auth token validation and RBAC permission checks."
        result = self.classifier.classify(content)
        assert "security" in result.tags.get("domain", [])

    def test_detects_testing_domain(self) -> None:
        content = "Added pytest fixtures and mock objects to improve coverage."
        result = self.classifier.classify(content)
        assert "testing" in result.tags.get("domain", [])

    def test_detects_observability_domain(self) -> None:
        content = "Set up prometheus metrics and grafana dashboard for alerting."
        result = self.classifier.classify(content)
        assert "observability" in result.tags.get("domain", [])

    def test_multi_domain_detection(self) -> None:
        content = (
            "Deploy the dbt spark ETL job to the kubernetes cluster using terraform."
        )
        result = self.classifier.classify(content)
        domains = result.tags.get("domain", [])
        assert len(domains) >= 2

    def test_requires_two_keyword_hits(self) -> None:
        """Single keyword hit is not enough to tag a domain."""
        content = "The terraform plan looks good."
        result = self.classifier.classify(content)
        # Only one hit for infrastructure ("terraform"), need 2+
        assert "infrastructure" not in result.tags.get("domain", [])

    def test_no_match_flags_llm_review(self) -> None:
        content = "Had a great meeting about quarterly planning."
        result = self.classifier.classify(content)
        assert result.tags.get("domain", []) == []
        assert result.needs_llm_review is True

    def test_confidence_scales_with_hits(self) -> None:
        content = (
            "The dbt spark airflow dag etl transform"
            " lineage warehouse pipeline runs daily."
        )
        result = self.classifier.classify(content)
        assert result.confidence >= 0.8

    def test_custom_domain_keywords(self) -> None:
        custom = {"payments": ["stripe", "payment", "charge", "invoice", "billing"]}
        classifier = KeywordDomainClassifier(extra_domains=custom)
        content = "Process the stripe payment and generate an invoice."
        result = classifier.classify(content)
        assert "payments" in result.tags.get("domain", [])

    def test_max_three_domains(self) -> None:
        """Even with many matches, only top 3 domains are returned."""
        content = (
            "The dbt spark etl transform pipeline "
            "deployed to kubernetes docker terraform cluster "
            "with pytest mock coverage fixtures "
            "and prometheus grafana metrics dashboard"
        )
        result = self.classifier.classify(content)
        assert len(result.tags.get("domain", [])) <= 3


class TestSourceSystemClassifier:
    """SourceSystemClassifier maps source system and file paths to tags."""

    def setup_method(self) -> None:
        self.classifier = SourceSystemClassifier()

    def test_name(self) -> None:
        assert self.classifier.name == "source_system"

    def test_dbt_source_maps_to_data_pipeline(self) -> None:
        ctx = ClassificationContext(source_system="dbt")
        result = self.classifier.classify("", context=ctx)
        assert "data-pipeline" in result.tags.get("domain", [])

    def test_unity_catalog_source(self) -> None:
        ctx = ClassificationContext(source_system="unity_catalog")
        result = self.classifier.classify("", context=ctx)
        domains = result.tags.get("domain", [])
        assert "data-pipeline" in domains

    def test_obsidian_source_maps_to_documentation(self) -> None:
        ctx = ClassificationContext(source_system="obsidian")
        result = self.classifier.classify("", context=ctx)
        assert "documentation" in result.tags.get("domain", [])

    def test_openlineage_source(self) -> None:
        ctx = ClassificationContext(source_system="openlineage")
        result = self.classifier.classify("", context=ctx)
        domains = result.tags.get("domain", [])
        assert "data-pipeline" in domains
        assert "observability" in domains

    def test_test_file_path(self) -> None:
        ctx = ClassificationContext(file_path="tests/unit/test_auth.py")
        result = self.classifier.classify("", context=ctx)
        assert "testing" in result.tags.get("domain", [])
        assert "code" in result.tags.get("content_type", [])

    def test_docs_file_path(self) -> None:
        ctx = ClassificationContext(file_path="docs/guide/setup.md")
        result = self.classifier.classify("", context=ctx)
        assert "documentation" in result.tags.get("content_type", [])

    def test_no_context_flags_llm_review(self) -> None:
        result = self.classifier.classify("some content")
        assert result.needs_llm_review is True
        assert result.confidence <= 0.3

    def test_unknown_source_system_flags_llm_review(self) -> None:
        ctx = ClassificationContext(source_system="unknown_system")
        result = self.classifier.classify("", context=ctx)
        assert result.needs_llm_review is True

    def test_git_source_needs_further_classification(self) -> None:
        ctx = ClassificationContext(source_system="git")
        result = self.classifier.classify("", context=ctx)
        # git alone doesn't tell us the domain
        assert result.tags.get("domain", []) == []

    def test_high_confidence_with_known_source(self) -> None:
        ctx = ClassificationContext(source_system="dbt")
        result = self.classifier.classify("", context=ctx)
        assert result.confidence >= 0.8
