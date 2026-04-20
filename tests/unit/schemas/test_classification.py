"""Tests for classification schemas."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trellis.schemas.classification import (
    RESERVED_NAMESPACES,
    ContentTags,
    DataClassification,
    Lifecycle,
)


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

    @pytest.mark.parametrize(
        "content_type",
        [
            "pattern",
            "decision",
            "error-resolution",
            "discovery",
            "procedure",
            "constraint",
            "configuration",
            "code",
        ],
    )
    def test_content_type_accepts_valid_values(self, content_type: str) -> None:
        tags = ContentTags(content_type=content_type)
        assert tags.content_type == content_type

    def test_content_type_rejects_invalid(self) -> None:
        with pytest.raises(ValidationError):
            ContentTags(content_type="made-up-type")

    @pytest.mark.parametrize("scope", ["universal", "org", "project", "ephemeral"])
    def test_scope_accepts_valid_values(self, scope: str) -> None:
        tags = ContentTags(scope=scope)
        assert tags.scope == scope

    def test_scope_rejects_invalid(self) -> None:
        with pytest.raises(ValidationError):
            ContentTags(scope="galaxy-wide")

    @pytest.mark.parametrize("signal_quality", ["high", "standard", "low", "noise"])
    def test_signal_quality_accepts_valid_values(self, signal_quality: str) -> None:
        tags = ContentTags(signal_quality=signal_quality)
        assert tags.signal_quality == signal_quality

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
    @pytest.mark.parametrize(
        "affinity",
        ["domain_knowledge", "technical_pattern", "operational", "reference"],
    )
    def test_accepts_valid_values(self, affinity: str) -> None:
        tags = ContentTags(retrieval_affinity=[affinity])
        assert tags.retrieval_affinity == [affinity]

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


class TestReservedNamespaces:
    """Reserved namespaces are rejected in ContentTags.custom and .domain.

    See docs/design/adr-tag-vocabulary-split.md §2.3-§2.4 for the decision
    record. Each reserved name blocks both the bare form (``"sensitivity"``)
    and the namespaced form (``"sensitivity:pii"``).
    """

    @pytest.mark.parametrize("reserved", sorted(RESERVED_NAMESPACES))
    def test_rejects_bare_reserved_key_in_custom(self, reserved: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ContentTags(custom={reserved: ["value"]})
        assert reserved in str(exc_info.value)
        assert "reserved namespace" in str(exc_info.value)

    @pytest.mark.parametrize("reserved", sorted(RESERVED_NAMESPACES))
    def test_rejects_namespaced_key_in_custom(self, reserved: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ContentTags(custom={f"{reserved}:pii": ["value"]})
        assert reserved in str(exc_info.value)

    @pytest.mark.parametrize("reserved", sorted(RESERVED_NAMESPACES))
    def test_rejects_bare_reserved_value_in_domain(self, reserved: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ContentTags(domain=[reserved])
        assert reserved in str(exc_info.value)

    @pytest.mark.parametrize("reserved", sorted(RESERVED_NAMESPACES))
    def test_rejects_namespaced_value_in_domain(self, reserved: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ContentTags(domain=[f"{reserved}:pii"])
        assert reserved in str(exc_info.value)

    @pytest.mark.parametrize(
        ("reserved", "expected_substring"),
        [
            ("sensitivity", "DataClassification.sensitivity"),
            ("regulatory", "DataClassification.regulatory_tags"),
            ("jurisdiction", "DataClassification.jurisdiction"),
            ("lifecycle", "Lifecycle.state"),
            ("authority", "derived from graph position"),
            ("retention", "PolicyType.RETENTION"),
            ("redaction", "PolicyType.REDACTION"),
        ],
    )
    def test_error_message_points_to_correct_destination(
        self, reserved: str, expected_substring: str
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ContentTags(custom={f"{reserved}:x": ["y"]})
        msg = str(exc_info.value)
        assert expected_substring in msg
        assert "adr-tag-vocabulary-split.md" in msg

    def test_non_reserved_custom_keys_are_allowed(self) -> None:
        tags = ContentTags(
            custom={"team": ["platform"], "project-phase": ["discovery"]},
        )
        assert tags.custom == {"team": ["platform"], "project-phase": ["discovery"]}

    def test_non_reserved_domain_values_are_allowed(self) -> None:
        tags = ContentTags(
            domain=["data-pipeline", "uc:governance", "sp:legal"],
        )
        assert tags.domain == ["data-pipeline", "uc:governance", "sp:legal"]

    def test_substring_match_is_not_rejected(self) -> None:
        """Only bare or colon-delimited reserved names are rejected.

        A value like ``"sensitivity-aware"`` or ``"life-cycle-management"`` is
        not a namespace collision -- the reservation applies to ``name`` and
        ``name:*`` only.
        """
        tags = ContentTags(
            custom={
                "sensitivity-aware": ["x"],
                "my_retention_plan": ["y"],
            },
            domain=["life-cycle-management"],
        )
        assert "sensitivity-aware" in tags.custom
        assert "my_retention_plan" in tags.custom
        assert "life-cycle-management" in tags.domain


class TestDataClassification:
    """DataClassification is defined but not required in Phase 0.

    Tests cover shape and defaults only -- no consumer enforces this schema
    yet. See docs/design/adr-tag-vocabulary-split.md §4 for the phased rollout.
    """

    def test_defaults_to_internal_sensitivity(self) -> None:
        dc = DataClassification()
        assert dc.sensitivity == "internal"
        assert dc.regulatory_tags == []
        assert dc.jurisdiction == []
        assert dc.classification_version == "1"

    @pytest.mark.parametrize(
        "sensitivity", ["public", "internal", "confidential", "restricted"]
    )
    def test_accepts_all_sensitivity_values(self, sensitivity: str) -> None:
        dc = DataClassification(sensitivity=sensitivity)
        assert dc.sensitivity == sensitivity

    def test_rejects_unknown_sensitivity(self) -> None:
        with pytest.raises(ValidationError):
            DataClassification(sensitivity="top-secret")  # type: ignore[arg-type]

    def test_regulatory_tags_are_open(self) -> None:
        dc = DataClassification(regulatory_tags=["pii", "gdpr", "custom-tag"])
        assert dc.regulatory_tags == ["pii", "gdpr", "custom-tag"]

    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            DataClassification(nope="bad")  # type: ignore[call-arg]

    def test_round_trip(self) -> None:
        dc = DataClassification(
            sensitivity="confidential",
            regulatory_tags=["pii", "gdpr"],
            jurisdiction=["eu"],
            classified_by=["regex"],
        )
        assert DataClassification(**dc.model_dump()) == dc


class TestLifecycle:
    """Lifecycle is defined but not required in Phase 0.

    See docs/design/adr-tag-vocabulary-split.md §4.
    """

    def test_defaults_to_current(self) -> None:
        lc = Lifecycle()
        assert lc.state == "current"
        assert lc.valid_from is None
        assert lc.valid_until is None
        assert lc.superseded_by is None

    def test_accepts_all_states(self) -> None:
        for state in ["draft", "current", "deprecated", "superseded", "archived"]:
            lc = Lifecycle(state=state)
            assert lc.state == state

    def test_rejects_unknown_state(self) -> None:
        with pytest.raises(ValidationError):
            Lifecycle(state="zombie")  # type: ignore[arg-type]

    def test_supports_supersession_metadata(self) -> None:
        lc = Lifecycle(
            state="superseded",
            superseded_by="doc-42",
            deprecation_reason="replaced by v2",
        )
        assert lc.superseded_by == "doc-42"
        assert lc.deprecation_reason == "replaced by v2"

    def test_round_trip_with_timestamps(self) -> None:
        lc = Lifecycle(
            state="deprecated",
            valid_from=datetime(2024, 1, 1, tzinfo=UTC),
            valid_until=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert Lifecycle(**lc.model_dump()) == lc

    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            Lifecycle(nope="bad")  # type: ignore[call-arg]
