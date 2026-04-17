"""Tests for Advisory schema."""

from __future__ import annotations

from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence


class TestAdvisoryEvidence:
    def test_basic(self) -> None:
        ev = AdvisoryEvidence(
            sample_size=47,
            success_rate_with=0.82,
            success_rate_without=0.34,
            effect_size=0.48,
            representative_trace_ids=["t1", "t2"],
        )
        assert ev.sample_size == 47
        assert ev.effect_size == 0.48
        assert len(ev.representative_trace_ids) == 2

    def test_defaults(self) -> None:
        ev = AdvisoryEvidence(
            sample_size=5,
            success_rate_with=0.8,
            success_rate_without=0.5,
            effect_size=0.3,
        )
        assert ev.representative_trace_ids == []


class TestAdvisory:
    def test_basic(self) -> None:
        adv = Advisory(
            category=AdvisoryCategory.ENTITY,
            confidence=0.85,
            message="Entity X appears in 82% of successful packs",
            evidence=AdvisoryEvidence(
                sample_size=47,
                success_rate_with=0.82,
                success_rate_without=0.34,
                effect_size=0.48,
            ),
            scope="platform",
            entity_id="ent_x",
        )
        assert adv.category == AdvisoryCategory.ENTITY
        assert adv.confidence == 0.85
        assert adv.entity_id == "ent_x"
        assert len(adv.advisory_id) == 26  # ULID

    def test_all_categories(self) -> None:
        evidence = AdvisoryEvidence(
            sample_size=10,
            success_rate_with=0.8,
            success_rate_without=0.4,
            effect_size=0.4,
        )
        for cat in AdvisoryCategory:
            adv = Advisory(
                category=cat,
                confidence=0.5,
                message=f"Test {cat.value}",
                evidence=evidence,
                scope="global",
            )
            assert adv.category == cat

    def test_defaults(self) -> None:
        adv = Advisory(
            category=AdvisoryCategory.APPROACH,
            confidence=0.5,
            message="test",
            evidence=AdvisoryEvidence(
                sample_size=5,
                success_rate_with=0.8,
                success_rate_without=0.4,
                effect_size=0.4,
            ),
            scope="global",
        )
        assert adv.entity_id is None
        assert adv.metadata == {}

    def test_serialization_roundtrip(self) -> None:
        adv = Advisory(
            category=AdvisoryCategory.ANTI_PATTERN,
            confidence=0.73,
            message="Skipping validation correlates with failure",
            evidence=AdvisoryEvidence(
                sample_size=23,
                success_rate_with=0.26,
                success_rate_without=0.70,
                effect_size=-0.44,
                representative_trace_ids=["t1"],
            ),
            scope="data-pipeline",
            entity_id="ent_skip_validation",
        )
        data = adv.model_dump(mode="json")
        restored = Advisory.model_validate(data)
        assert restored.advisory_id == adv.advisory_id
        assert restored.confidence == adv.confidence
        assert restored.evidence.effect_size == -0.44
