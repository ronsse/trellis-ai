"""Tests for classification schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trellis.schemas.classification import ContentTags


class TestContentTagsDefaults:
    """ContentTags creates with sensible defaults."""

    def test_empty_tags(self) -> None:
        tags = ContentTags()
        assert tags.domain == []
        assert tags.content_type is None
        assert tags.scope is None
        assert tags.signal_quality == "standard"
        assert tags.custom == {}
        assert tags.classified_by == []
        assert tags.classification_version == "2"

    def test_tags_with_all_fields(self) -> None:
        tags = ContentTags(
            domain=["data-pipeline", "infrastructure"],
            content_type="error-resolution",
            scope="project",
            signal_quality="high",
            custom={"team": ["platform"]},
            classified_by=["structural", "keyword_domain"],
            classification_version="2",
        )
        assert tags.domain == ["data-pipeline", "infrastructure"]
        assert tags.content_type == "error-resolution"
        assert tags.scope == "project"
        assert tags.signal_quality == "high"
        assert tags.custom == {"team": ["platform"]}
        assert tags.classified_by == ["structural", "keyword_domain"]
        assert tags.classification_version == "2"


class TestContentTagsForbidsExtras:
    """ContentTags rejects unknown fields."""

    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ContentTags(
                domain=["api"],
                nope="bad",  # type: ignore[call-arg]
            )


class TestContentTagsValidation:
    """ContentTags validates controlled vocabularies."""

    def test_content_type_accepts_valid_values(self) -> None:
        for ct in [
            "pattern",
            "decision",
            "error-resolution",
            "discovery",
            "procedure",
            "constraint",
            "configuration",
            "code",
        ]:
            tags = ContentTags(content_type=ct)
            assert tags.content_type == ct

    def test_content_type_rejects_invalid(self) -> None:
        with pytest.raises(ValidationError):
            ContentTags(content_type="made-up-type")

    def test_scope_accepts_valid_values(self) -> None:
        for s in ["universal", "org", "project", "ephemeral"]:
            tags = ContentTags(scope=s)
            assert tags.scope == s

    def test_scope_rejects_invalid(self) -> None:
        with pytest.raises(ValidationError):
            ContentTags(scope="galaxy-wide")

    def test_signal_quality_accepts_valid_values(self) -> None:
        for sq in ["high", "standard", "low", "noise"]:
            tags = ContentTags(signal_quality=sq)
            assert tags.signal_quality == sq

    def test_signal_quality_rejects_invalid(self) -> None:
        with pytest.raises(ValidationError):
            ContentTags(signal_quality="mega-important")

    def test_domain_accepts_any_strings(self) -> None:
        """Domain is multi-label and extensible -- no controlled vocab."""
        tags = ContentTags(domain=["custom-domain", "another"])
        assert tags.domain == ["custom-domain", "another"]


class TestContentTagsSerialization:
    """ContentTags round-trips through JSON."""

    def test_round_trip(self) -> None:
        tags = ContentTags(
            domain=["data-pipeline"],
            content_type="procedure",
            scope="org",
            signal_quality="high",
            classified_by=["structural"],
        )
        data = tags.model_dump()
        restored = ContentTags(**data)
        assert restored == tags

    def test_json_round_trip(self) -> None:
        tags = ContentTags(
            domain=["infrastructure", "security"],
            content_type="constraint",
            signal_quality="low",
        )
        json_str = tags.model_dump_json()
        restored = ContentTags.model_validate_json(json_str)
        assert restored == tags

    def test_none_fields_excluded_from_dump(self) -> None:
        tags = ContentTags()
        data = tags.model_dump(exclude_none=True)
        assert "content_type" not in data
        assert "scope" not in data
        assert "signal_quality" in data  # has default, not None


class TestRetrievalAffinity:
    def test_accepts_valid_values(self) -> None:
        for ra in ["domain_knowledge", "technical_pattern", "operational", "reference"]:
            tags = ContentTags(retrieval_affinity=[ra])
            assert tags.retrieval_affinity == [ra]

    def test_rejects_invalid(self) -> None:
        with pytest.raises(ValidationError):
            ContentTags(retrieval_affinity=["made_up_affinity"])

    def test_defaults_empty(self) -> None:
        tags = ContentTags()
        assert tags.retrieval_affinity == []

    def test_multi_label(self) -> None:
        tags = ContentTags(retrieval_affinity=["domain_knowledge", "technical_pattern"])
        assert len(tags.retrieval_affinity) == 2

    def test_round_trip(self) -> None:
        tags = ContentTags(retrieval_affinity=["operational", "reference"])
        data = tags.model_dump()
        restored = ContentTags(**data)
        assert restored.retrieval_affinity == ["operational", "reference"]

    def test_classification_version_is_2(self) -> None:
        tags = ContentTags()
        assert tags.classification_version == "2"
