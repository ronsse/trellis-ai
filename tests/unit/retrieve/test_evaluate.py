"""Tests for pack quality evaluation (assembly-time scoring)."""

from __future__ import annotations

import pytest

from trellis.retrieve.evaluate import (
    BUILTIN_PROFILES,
    CODE_GENERATION_PROFILE,
    DOMAIN_CONTEXT_PROFILE,
    BreadthScorer,
    CompletenessScorer,
    EfficiencyScorer,
    EvaluationProfile,
    EvaluationScenario,
    NoiseScorer,
    QualityDimension,
    RelevanceScorer,
    evaluate_pack,
)
from trellis.schemas.pack import Pack, PackItem


def _make_pack(items: list[PackItem], *, intent: str = "test intent") -> Pack:
    return Pack(intent=intent, items=items)


def _item(
    item_id: str,
    excerpt: str = "",
    *,
    relevance: float = 0.0,
    tokens: int | None = None,
    metadata: dict | None = None,
) -> PackItem:
    return PackItem(
        item_id=item_id,
        item_type="evidence",
        excerpt=excerpt,
        relevance_score=relevance,
        estimated_tokens=tokens,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# CompletenessScorer
# ---------------------------------------------------------------------------


class TestCompletenessScorer:
    def test_empty_required_returns_one(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i")
        pack = _make_pack([_item("a", "anything")])
        assert CompletenessScorer().score(pack, scenario) == 1.0

    def test_all_keywords_hit(self) -> None:
        scenario = EvaluationScenario(
            name="s", intent="i", required_coverage=["alpha", "beta"]
        )
        pack = _make_pack(
            [
                _item("a", "Alpha reference doc"),
                _item("b", "beta implementation notes"),
            ]
        )
        assert CompletenessScorer().score(pack, scenario) == 1.0

    def test_partial_coverage(self) -> None:
        scenario = EvaluationScenario(
            name="s", intent="i", required_coverage=["alpha", "beta", "gamma"]
        )
        pack = _make_pack([_item("a", "alpha text only")])
        assert CompletenessScorer().score(pack, scenario) == pytest.approx(1 / 3)

    def test_missing_lists_absent_keywords(self) -> None:
        scorer = CompletenessScorer()
        scenario = EvaluationScenario(
            name="s", intent="i", required_coverage=["alpha", "beta", "gamma"]
        )
        pack = _make_pack([_item("a", "alpha text")])
        assert scorer.missing(pack, scenario) == ["beta", "gamma"]

    def test_case_insensitive_match(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", required_coverage=["Alpha"])
        pack = _make_pack([_item("a", "ALPHA heading")])
        assert CompletenessScorer().score(pack, scenario) == 1.0


# ---------------------------------------------------------------------------
# RelevanceScorer
# ---------------------------------------------------------------------------


class TestRelevanceScorer:
    def test_empty_pack_returns_zero(self) -> None:
        pack = _make_pack([])
        assert (
            RelevanceScorer().score(pack, EvaluationScenario(name="s", intent="i"))
            == 0.0
        )

    def test_mean_relevance(self) -> None:
        pack = _make_pack(
            [
                _item("a", relevance=0.8),
                _item("b", relevance=0.4),
                _item("c", relevance=0.6),
            ]
        )
        assert RelevanceScorer().score(
            pack, EvaluationScenario(name="s", intent="i")
        ) == pytest.approx(0.6)

    def test_out_of_range_values_are_clamped(self) -> None:
        pack = _make_pack([_item("a", relevance=2.0), _item("b", relevance=-0.5)])
        assert RelevanceScorer().score(
            pack, EvaluationScenario(name="s", intent="i")
        ) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# NoiseScorer
# ---------------------------------------------------------------------------


class TestNoiseScorer:
    def test_no_domain_returns_one(self) -> None:
        pack = _make_pack(
            [_item("a", metadata={"content_tags": {"domain": ["other"]}})]
        )
        assert (
            NoiseScorer().score(pack, EvaluationScenario(name="s", intent="i")) == 1.0
        )

    def test_all_match_scenario_domain(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", domain="billing")
        pack = _make_pack(
            [
                _item("a", metadata={"content_tags": {"domain": ["billing"]}}),
                _item("b", metadata={"content_tags": {"domain": ["billing"]}}),
            ]
        )
        assert NoiseScorer().score(pack, scenario) == 1.0

    def test_mismatch_drops_score(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", domain="billing")
        pack = _make_pack(
            [
                _item("a", metadata={"content_tags": {"domain": ["billing"]}}),
                _item("b", metadata={"content_tags": {"domain": ["other"]}}),
            ]
        )
        assert NoiseScorer().score(pack, scenario) == pytest.approx(0.5)

    def test_domain_all_counts_as_match(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", domain="billing")
        pack = _make_pack([_item("a", metadata={"content_tags": {"domain": ["all"]}})])
        assert NoiseScorer().score(pack, scenario) == 1.0

    def test_flat_metadata_layout(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", domain="billing")
        pack = _make_pack(
            [
                _item("a", metadata={"domain": ["billing"]}),
                _item("b", metadata={"domain": "other"}),
            ]
        )
        assert NoiseScorer().score(pack, scenario) == pytest.approx(0.5)

    def test_untagged_items_are_excluded(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", domain="billing")
        pack = _make_pack(
            [
                _item("a", metadata={"content_tags": {"domain": ["billing"]}}),
                _item("b"),
            ]
        )
        assert NoiseScorer().score(pack, scenario) == 1.0


# ---------------------------------------------------------------------------
# BreadthScorer
# ---------------------------------------------------------------------------


class TestBreadthScorer:
    def test_empty_expected_returns_one(self) -> None:
        pack = _make_pack(
            [_item("a", metadata={"content_tags": {"content_type": "reference"}})]
        )
        assert (
            BreadthScorer().score(pack, EvaluationScenario(name="s", intent="i")) == 1.0
        )

    def test_all_categories_present(self) -> None:
        scenario = EvaluationScenario(
            name="s",
            intent="i",
            expected_categories=["reference", "tutorial"],
        )
        pack = _make_pack(
            [
                _item(
                    "a",
                    metadata={"content_tags": {"content_type": "reference"}},
                ),
                _item(
                    "b",
                    metadata={"content_tags": {"content_type": "tutorial"}},
                ),
            ]
        )
        assert BreadthScorer().score(pack, scenario) == 1.0

    def test_partial_breadth(self) -> None:
        scenario = EvaluationScenario(
            name="s",
            intent="i",
            expected_categories=["reference", "tutorial", "example"],
        )
        pack = _make_pack(
            [
                _item(
                    "a",
                    metadata={"content_tags": {"content_type": "reference"}},
                )
            ]
        )
        assert BreadthScorer().score(pack, scenario) == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# EfficiencyScorer
# ---------------------------------------------------------------------------


class TestEfficiencyScorer:
    def test_empty_required_returns_one(self) -> None:
        pack = _make_pack([_item("a", "anything", tokens=10)])
        assert (
            EfficiencyScorer().score(pack, EvaluationScenario(name="s", intent="i"))
            == 1.0
        )

    def test_all_tokens_useful(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", required_coverage=["alpha"])
        pack = _make_pack(
            [
                _item("a", "alpha notes", tokens=10),
                _item("b", "alpha more", tokens=5),
            ]
        )
        assert EfficiencyScorer().score(pack, scenario) == 1.0

    def test_partial_useful_tokens(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", required_coverage=["alpha"])
        pack = _make_pack(
            [
                _item("a", "alpha notes", tokens=10),
                _item("b", "unrelated text", tokens=30),
            ]
        )
        assert EfficiencyScorer().score(pack, scenario) == pytest.approx(10 / 40)

    def test_empty_pack_returns_zero(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", required_coverage=["alpha"])
        pack = _make_pack([])
        assert EfficiencyScorer().score(pack, scenario) == 0.0

    def test_token_fallback_from_excerpt_length(self) -> None:
        scenario = EvaluationScenario(name="s", intent="i", required_coverage=["alpha"])
        # No estimated_tokens; excerpt "alpha" (5 chars) → 5 // 4 + 1 = 2 tokens.
        pack = _make_pack([_item("a", "alpha")])
        assert EfficiencyScorer().score(pack, scenario) == 1.0


# ---------------------------------------------------------------------------
# EvaluationProfile
# ---------------------------------------------------------------------------


class TestEvaluationProfile:
    def test_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match="sum to 1.0"):
            EvaluationProfile(name="bad", weights={"a": 0.3, "b": 0.3})

    def test_weights_cannot_be_empty(self) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            EvaluationProfile(name="empty", weights={})

    def test_weights_must_be_in_range(self) -> None:
        with pytest.raises(ValueError, match=r"must be in \[0, 1\]"):
            EvaluationProfile(name="neg", weights={"a": -0.1, "b": 1.1})

    def test_builtin_profiles_validate(self) -> None:
        assert CODE_GENERATION_PROFILE.weights["completeness"] == 0.35
        assert DOMAIN_CONTEXT_PROFILE.weights["breadth"] == 0.30
        assert set(BUILTIN_PROFILES) == {"code_generation", "domain_context"}


# ---------------------------------------------------------------------------
# Top-level evaluate_pack entry point
# ---------------------------------------------------------------------------


class TestEvaluatePack:
    def test_mean_aggregation_without_profile(self) -> None:
        scenario = EvaluationScenario(
            name="s",
            intent="i",
            required_coverage=["alpha"],
            expected_categories=["reference"],
        )
        pack = _make_pack(
            [
                _item(
                    "a",
                    "alpha reference notes",
                    relevance=1.0,
                    tokens=10,
                    metadata={"content_tags": {"content_type": "reference"}},
                )
            ]
        )
        report = evaluate_pack(pack, scenario)
        assert report.scenario_name == "s"
        assert report.pack_id == pack.pack_id
        assert report.profile_name is None
        # All five dimensions score 1.0 on this pack → mean = 1.0.
        assert report.weighted_score == pytest.approx(1.0)
        assert set(report.dimensions) == {
            "completeness",
            "relevance",
            "noise",
            "breadth",
            "efficiency",
        }

    def test_weighted_aggregation_with_profile(self) -> None:
        scenario = EvaluationScenario(
            name="s",
            intent="i",
            required_coverage=["alpha", "beta"],
            expected_categories=["reference"],
        )
        pack = _make_pack(
            [
                _item(
                    "a",
                    "alpha notes",
                    relevance=0.6,
                    tokens=10,
                    metadata={"content_tags": {"content_type": "reference"}},
                )
            ]
        )
        report = evaluate_pack(pack, scenario, profile=CODE_GENERATION_PROFILE)
        assert report.profile_name == "code_generation"
        # Completeness = 0.5 (1 of 2 keywords), Relevance = 0.6, Noise = 1.0,
        # Breadth = 1.0, Efficiency = 1.0.
        expected = 0.35 * 0.5 + 0.25 * 0.6 + 0.20 * 1.0 + 0.10 * 1.0 + 0.10 * 1.0
        assert report.weighted_score == pytest.approx(expected)

    def test_missing_coverage_surfaced(self) -> None:
        scenario = EvaluationScenario(
            name="s",
            intent="i",
            required_coverage=["alpha", "beta", "gamma"],
        )
        pack = _make_pack([_item("a", "alpha only")])
        report = evaluate_pack(pack, scenario)
        assert report.missing_coverage == ["beta", "gamma"]

    def test_findings_flag_low_dimensions(self) -> None:
        scenario = EvaluationScenario(
            name="s",
            intent="i",
            domain="billing",
            required_coverage=["alpha"],
            expected_categories=["reference", "tutorial"],
        )
        pack = _make_pack(
            [
                _item(
                    "noise1",
                    "other",
                    relevance=0.1,
                    metadata={"content_tags": {"domain": ["other"]}},
                ),
                _item(
                    "noise2",
                    "other2",
                    relevance=0.1,
                    metadata={"content_tags": {"domain": ["other"]}},
                ),
            ]
        )
        report = evaluate_pack(pack, scenario)
        assert any("completeness" in f for f in report.findings)
        assert any("relevance" in f for f in report.findings)
        assert any("noise" in f for f in report.findings)
        assert any("breadth" in f for f in report.findings)

    def test_profile_with_partial_dimension_coverage_renormalizes(self) -> None:
        profile = EvaluationProfile(
            name="partial",
            weights={"completeness": 0.6, "unknown_dim": 0.4},
        )
        scenario = EvaluationScenario(name="s", intent="i", required_coverage=["alpha"])
        pack = _make_pack([_item("a", "alpha text")])
        report = evaluate_pack(pack, scenario, profile=profile)
        # Only completeness is covered; its weight (0.6) renormalizes to 1.0,
        # so weighted_score equals the completeness score itself.
        assert report.weighted_score == pytest.approx(1.0)

    def test_custom_dimension_via_protocol(self) -> None:
        class ConstantScorer:
            name = "constant"

            def score(self, pack: Pack, scenario: EvaluationScenario) -> float:
                return 0.42

        scorer: QualityDimension = ConstantScorer()
        report = evaluate_pack(
            _make_pack([]),
            EvaluationScenario(name="s", intent="i"),
            dimensions=[scorer],
        )
        assert report.dimensions == {"constant": 0.42}
        assert report.weighted_score == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Dimension predictiveness analyzer
# ---------------------------------------------------------------------------


class TestDimensionPredictiveness:
    def _emit_quality_and_feedback(
        self,
        event_log,
        *,
        pack_id: str,
        dimensions: dict[str, float],
        weighted_score: float,
        success: bool,
    ) -> None:
        from trellis.stores.base.event_log import EventType

        event_log.emit(
            EventType.PACK_QUALITY_SCORED,
            source="pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "pack_id": pack_id,
                "scenario_name": "test",
                "profile_name": None,
                "dimensions": dimensions,
                "weighted_score": weighted_score,
                "missing_coverage_count": 0,
                "findings_count": 0,
            },
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mcp.record_feedback",
            entity_id=pack_id,
            entity_type="pack",
            payload={"pack_id": pack_id, "success": success},
        )

    @pytest.fixture
    def event_log(self, tmp_path):
        from trellis.stores.sqlite.event_log import SQLiteEventLog

        log = SQLiteEventLog(tmp_path / "events.db")
        yield log
        log.close()

    def test_empty_event_log_returns_note(self, event_log) -> None:
        from trellis.retrieve.evaluate import analyze_dimension_predictiveness

        report = analyze_dimension_predictiveness(event_log, days=30)
        assert report.total_packs_scored == 0
        assert report.total_matched_feedback == 0
        assert report.dimensions == []
        assert report.notes
        assert "No packs" in report.notes[0]

    def test_strong_positive_correlation(self, event_log) -> None:
        from trellis.retrieve.evaluate import analyze_dimension_predictiveness

        # 30 packs: high completeness → success, low completeness → failure
        for i in range(15):
            self._emit_quality_and_feedback(
                event_log,
                pack_id=f"pack-hi-{i}",
                dimensions={"completeness": 0.9, "relevance": 0.5},
                weighted_score=0.8,
                success=True,
            )
        for i in range(15):
            self._emit_quality_and_feedback(
                event_log,
                pack_id=f"pack-lo-{i}",
                dimensions={"completeness": 0.1, "relevance": 0.5},
                weighted_score=0.4,
                success=False,
            )

        report = analyze_dimension_predictiveness(event_log, days=30)
        assert report.total_matched_feedback == 30
        assert report.overall_success_rate == pytest.approx(0.5)

        by_name = {d.dimension: d for d in report.dimensions}
        completeness = by_name["completeness"]
        assert completeness.correlation is not None
        assert completeness.correlation > 0.9
        assert completeness.signal_classification == "strong"
        assert completeness.mean_score_on_success == pytest.approx(0.9)
        assert completeness.mean_score_on_failure == pytest.approx(0.1)

        # Relevance is constant → zero variance → None correlation
        relevance = by_name["relevance"]
        assert relevance.correlation is None
        assert relevance.signal_classification == "insufficient_data"

    def test_insufficient_data_when_below_threshold(self, event_log) -> None:
        from trellis.retrieve.evaluate import analyze_dimension_predictiveness

        # 5 packs < 20-sample threshold
        for i in range(5):
            self._emit_quality_and_feedback(
                event_log,
                pack_id=f"p-{i}",
                dimensions={"completeness": 0.5},
                weighted_score=0.5,
                success=i % 2 == 0,
            )
        report = analyze_dimension_predictiveness(event_log, days=30)
        assert report.total_matched_feedback == 5
        by_name = {d.dimension: d for d in report.dimensions}
        assert by_name["completeness"].signal_classification == "insufficient_data"
        assert any("below the" in note for note in report.notes)

    def test_noise_dimension_flagged_in_notes(self, event_log) -> None:
        import random

        from trellis.retrieve.evaluate import analyze_dimension_predictiveness

        rng = random.Random(42)  # noqa: S311 - test RNG, not crypto
        # Random completeness, random success — should correlate near 0
        for i in range(40):
            self._emit_quality_and_feedback(
                event_log,
                pack_id=f"p-{i}",
                dimensions={"completeness": rng.random()},
                weighted_score=0.5,
                success=rng.random() > 0.5,
            )
        report = analyze_dimension_predictiveness(event_log, days=30)
        by_name = {d.dimension: d for d in report.dimensions}
        completeness = by_name["completeness"]
        assert completeness.signal_classification in {"noise", "weak"}
        if completeness.signal_classification == "noise":
            assert any("weight reduction" in note for note in report.notes)

    def test_rating_based_success(self, event_log) -> None:
        from trellis.retrieve.evaluate import analyze_dimension_predictiveness
        from trellis.stores.base.event_log import EventType

        # Emit with rating instead of explicit success
        for i in range(25):
            pack_id = f"p-{i}"
            event_log.emit(
                EventType.PACK_QUALITY_SCORED,
                source="pack_builder",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "scenario_name": "s",
                    "profile_name": None,
                    "dimensions": {"completeness": 1.0 if i < 13 else 0.0},
                    "weighted_score": 0.5,
                    "missing_coverage_count": 0,
                    "findings_count": 0,
                },
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="mcp.record_feedback",
                entity_id=pack_id,
                entity_type="pack",
                payload={
                    "pack_id": pack_id,
                    "rating": 0.8 if i < 13 else 0.2,
                },
            )
        report = analyze_dimension_predictiveness(event_log, days=30)
        assert report.total_matched_feedback == 25
        by_name = {d.dimension: d for d in report.dimensions}
        assert by_name["completeness"].signal_classification == "strong"

    def test_weighted_score_tracked_separately(self, event_log) -> None:
        from trellis.retrieve.evaluate import analyze_dimension_predictiveness

        for i in range(25):
            self._emit_quality_and_feedback(
                event_log,
                pack_id=f"p-{i}",
                dimensions={"completeness": 0.5},
                weighted_score=0.9 if i < 13 else 0.1,
                success=i < 13,
            )
        report = analyze_dimension_predictiveness(event_log, days=30)
        assert report.weighted_score_predictiveness is not None
        weighted = report.weighted_score_predictiveness
        assert weighted.dimension == "weighted_score"
        assert weighted.signal_classification == "strong"
