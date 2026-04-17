"""Tests for context effectiveness analysis."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.retrieve.effectiveness import (
    analyze_advisory_effectiveness,
    analyze_effectiveness,
    run_advisory_fitness_loop,
    run_effectiveness_feedback,
)
from trellis.schemas.advisory import Advisory, AdvisoryCategory, AdvisoryEvidence
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


@pytest.fixture
def doc_store(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


def test_empty_analysis(event_log):
    report = analyze_effectiveness(event_log, days=30)
    assert report.total_packs == 0
    assert report.total_feedback == 0
    assert report.success_rate == 0.0
    assert report.item_scores == []
    assert report.noise_candidates == []


def test_packs_without_feedback(event_log):
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id="pack-1",
        entity_type="pack",
        payload={"injected_item_ids": ["item-a", "item-b"]},
    )
    report = analyze_effectiveness(event_log, days=30)
    assert report.total_packs == 1
    assert report.total_feedback == 0


def test_effectiveness_with_feedback(event_log):
    # Pack 1 with items a, b - successful
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id="pack-1",
        entity_type="pack",
        payload={"injected_item_ids": ["item-a", "item-b"]},
    )
    event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="test",
        entity_id="pack-1",
        entity_type="pack",
        payload={"pack_id": "pack-1", "success": True},
    )

    # Pack 2 with items a, c - failed
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id="pack-2",
        entity_type="pack",
        payload={"injected_item_ids": ["item-a", "item-c"]},
    )
    event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="test",
        entity_id="pack-2",
        entity_type="pack",
        payload={"pack_id": "pack-2", "success": False},
    )

    report = analyze_effectiveness(event_log, days=30, min_appearances=1)
    assert report.total_packs == 2
    assert report.total_feedback == 2
    assert report.success_rate == 0.5

    # item-a appears in both packs: 1 success, 1 failure = 50%
    item_a = next(i for i in report.item_scores if i["item_id"] == "item-a")
    assert item_a["appearances"] == 2
    assert item_a["success_rate"] == 0.5

    # item-b only in successful pack
    item_b = next(i for i in report.item_scores if i["item_id"] == "item-b")
    assert item_b["success_rate"] == 1.0

    # item-c only in failed pack
    item_c = next(i for i in report.item_scores if i["item_id"] == "item-c")
    assert item_c["success_rate"] == 0.0


def test_noise_candidates(event_log):
    # Create 3 packs all with item-noise, all failed
    for i in range(3):
        pack_id = f"pack-{i}"
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={"injected_item_ids": ["item-noise"]},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={"pack_id": pack_id, "success": False},
        )

    report = analyze_effectiveness(event_log, days=30, min_appearances=2)
    assert "item-noise" in report.noise_candidates


def test_to_dict(event_log):
    report = analyze_effectiveness(event_log, days=30)
    d = report.model_dump()
    assert "total_packs" in d
    assert "total_feedback" in d
    assert "success_rate" in d
    assert "item_scores" in d
    assert "noise_candidates" in d


def test_min_appearances_filter(event_log):
    # One pack with item-rare, one feedback
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id="pack-1",
        entity_type="pack",
        payload={"injected_item_ids": ["item-rare"]},
    )
    event_log.emit(
        EventType.FEEDBACK_RECORDED,
        source="test",
        entity_id="pack-1",
        entity_type="pack",
        payload={"pack_id": "pack-1", "success": True},
    )

    # min_appearances=2 should filter out item-rare (only 1 appearance)
    report = analyze_effectiveness(event_log, days=30, min_appearances=2)
    assert len(report.item_scores) == 0

    # min_appearances=1 should include it
    report = analyze_effectiveness(event_log, days=30, min_appearances=1)
    assert len(report.item_scores) == 1


class TestRunEffectivenessFeedback:
    """Tests for the wired feedback loop."""

    def test_applies_noise_tags_to_candidates(self, event_log, doc_store):
        """Items identified as noise candidates get signal_quality='noise'."""
        # Insert a document that will become a noise candidate
        doc_id = doc_store.put(
            "item-noise",
            "noisy content",
            {"content_tags": {"signal_quality": "standard"}},
        )

        # Create 3 packs all with item-noise, all failed
        for i in range(3):
            pack_id = f"pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"injected_item_ids": [doc_id]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": False},
            )

        report = run_effectiveness_feedback(
            event_log, doc_store, days=30, min_appearances=2
        )

        # The doc should now be tagged as noise
        doc = doc_store.get(doc_id)
        assert doc is not None
        assert doc["metadata"]["content_tags"]["signal_quality"] == "noise"
        assert doc_id in report.noise_candidates

    def test_noop_when_no_noise(self, event_log, doc_store):
        """When there are no noise candidates, no docs are modified."""
        doc_id = doc_store.put(
            "item-good",
            "good content",
            {"content_tags": {"signal_quality": "high"}},
        )

        # One successful pack
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack-1",
            entity_type="pack",
            payload={"injected_item_ids": [doc_id]},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="pack-1",
            entity_type="pack",
            payload={"pack_id": "pack-1", "success": True},
        )

        report = run_effectiveness_feedback(
            event_log, doc_store, days=30, min_appearances=1
        )

        # Doc stays unchanged
        doc = doc_store.get(doc_id)
        assert doc is not None
        assert doc["metadata"]["content_tags"]["signal_quality"] == "high"
        assert report.noise_candidates == []

    def test_empty_events_returns_clean_report(self, event_log, doc_store):
        """No events -> clean report, no errors."""
        report = run_effectiveness_feedback(event_log, doc_store, days=30)
        assert report.total_packs == 0
        assert report.noise_candidates == []


# --- Advisory fitness tests ---


def _make_advisory(
    advisory_id: str = "adv_1",
    confidence: float = 0.5,
    scope: str = "global",
) -> Advisory:
    """Helper to create a test advisory."""
    return Advisory(
        advisory_id=advisory_id,
        category=AdvisoryCategory.ENTITY,
        confidence=confidence,
        message="Test advisory",
        evidence=AdvisoryEvidence(
            sample_size=10,
            success_rate_with=0.8,
            success_rate_without=0.4,
            effect_size=0.4,
        ),
        scope=scope,
    )


@pytest.fixture
def advisory_store(tmp_path: Path):
    return AdvisoryStore(tmp_path / "advisories.json")


class TestAnalyzeAdvisoryEffectiveness:
    """Tests for advisory-outcome correlation analysis."""

    def test_empty_events(self, event_log, advisory_store):
        report = analyze_advisory_effectiveness(event_log, advisory_store)
        assert report.total_packs_with_advisories == 0
        assert report.total_feedback == 0
        assert report.advisory_scores == []

    def test_packs_without_advisories(self, event_log, advisory_store):
        """Packs with no advisory_ids produce no advisory scores."""
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack-1",
            entity_type="pack",
            payload={"advisory_ids": []},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="pack-1",
            entity_type="pack",
            payload={"pack_id": "pack-1", "success": True},
        )
        report = analyze_advisory_effectiveness(event_log, advisory_store)
        assert report.total_packs_with_advisories == 0
        assert report.advisory_scores == []

    def test_advisory_success_correlation(self, event_log, advisory_store):
        """Advisory present in successful packs gets high success rate."""
        adv = _make_advisory("adv_good")
        advisory_store.put(adv)

        # 3 packs with adv_good, all successful
        for i in range(3):
            pack_id = f"pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_good"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": True},
            )

        report = analyze_advisory_effectiveness(
            event_log, advisory_store, min_presentations=3
        )
        assert len(report.advisory_scores) == 1
        score = report.advisory_scores[0]
        assert score.advisory_id == "adv_good"
        assert score.presentations == 3
        assert score.successes == 3
        assert score.success_rate == 1.0

    def test_advisory_failure_correlation(self, event_log, advisory_store):
        """Advisory present in failed packs gets low success rate."""
        adv = _make_advisory("adv_bad")
        advisory_store.put(adv)

        # 3 packs with adv_bad, all failed
        for i in range(3):
            pack_id = f"pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_bad"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": False},
            )

        report = analyze_advisory_effectiveness(
            event_log, advisory_store, min_presentations=3
        )
        assert len(report.advisory_scores) == 1
        assert report.advisory_scores[0].success_rate == 0.0
        assert report.advisory_scores[0].failures == 3

    def test_min_presentations_filter(self, event_log, advisory_store):
        """Advisories below min_presentations threshold are not scored."""
        adv = _make_advisory("adv_rare")
        advisory_store.put(adv)

        # Only 2 presentations
        for i in range(2):
            pack_id = f"pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_rare"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": True},
            )

        report = analyze_advisory_effectiveness(
            event_log, advisory_store, min_presentations=3
        )
        assert report.advisory_scores == []

        # With lower threshold, it should appear
        report = analyze_advisory_effectiveness(
            event_log, advisory_store, min_presentations=1
        )
        assert len(report.advisory_scores) == 1

    def test_lift_calculation(self, event_log, advisory_store):
        """Lift = advisory success rate - baseline success rate."""
        # Pack 1: with advisory, succeeded
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack-1",
            entity_type="pack",
            payload={"advisory_ids": ["adv_lift"]},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="pack-1",
            entity_type="pack",
            payload={"pack_id": "pack-1", "success": True},
        )
        # Pack 2: with advisory, succeeded
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack-2",
            entity_type="pack",
            payload={"advisory_ids": ["adv_lift"]},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="pack-2",
            entity_type="pack",
            payload={"pack_id": "pack-2", "success": True},
        )
        # Pack 3: without advisory, failed
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack-3",
            entity_type="pack",
            payload={"advisory_ids": []},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id="pack-3",
            entity_type="pack",
            payload={"pack_id": "pack-3", "success": False},
        )

        report = analyze_advisory_effectiveness(
            event_log, advisory_store, min_presentations=1
        )
        assert len(report.advisory_scores) == 1
        score = report.advisory_scores[0]
        # Advisory: 2/2 = 100%; Baseline (without): 0/1 = 0%
        assert score.success_rate == 1.0
        assert score.baseline_rate == 0.0
        assert score.lift == 1.0

    def test_multiple_advisories_sorted_by_lift(self, event_log, advisory_store):
        """Advisory scores are sorted by lift descending."""
        # adv_a: 2 successes, 1 failure
        for i in range(3):
            pack_id = f"pack-a-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_a"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": i < 2},
            )

        # adv_b: 3 successes, 0 failures
        for i in range(3):
            pack_id = f"pack-b-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_b"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": True},
            )

        report = analyze_advisory_effectiveness(
            event_log, advisory_store, min_presentations=3
        )
        assert len(report.advisory_scores) == 2
        assert report.advisory_scores[0].advisory_id == "adv_b"
        assert report.advisory_scores[1].advisory_id == "adv_a"


class TestRunAdvisoryFitnessLoop:
    """Tests for the full advisory fitness feedback loop."""

    def test_boosts_successful_advisory(self, event_log, advisory_store):
        """Advisory with high success rate gets confidence boosted."""
        adv = _make_advisory("adv_boost", confidence=0.5)
        advisory_store.put(adv)

        for i in range(3):
            pack_id = f"pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_boost"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": True},
            )

        report = run_advisory_fitness_loop(
            event_log, advisory_store, min_presentations=3, blend_weight=0.3
        )

        updated = advisory_store.get("adv_boost")
        assert updated is not None
        # new = 0.7 * 0.5 + 0.3 * 1.0 = 0.65
        assert updated.confidence == 0.65
        assert "adv_boost" in report.advisories_boosted

    def test_suppresses_failing_advisory(self, event_log, advisory_store):
        """Advisory with very low success rate and low confidence gets suppressed."""
        adv = _make_advisory("adv_suppress", confidence=0.12)
        advisory_store.put(adv)

        for i in range(3):
            pack_id = f"pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_suppress"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": False},
            )

        report = run_advisory_fitness_loop(
            event_log,
            advisory_store,
            min_presentations=3,
            suppress_below=0.1,
            blend_weight=0.3,
        )

        # new = 0.7 * 0.12 + 0.3 * 0.0 = 0.084 < 0.1 threshold
        assert advisory_store.get("adv_suppress") is None
        assert "adv_suppress" in report.advisories_suppressed

    def test_reduces_confidence_without_suppressing(self, event_log, advisory_store):
        """Advisory with mixed results gets reduced confidence but stays."""
        adv = _make_advisory("adv_mixed", confidence=0.6)
        advisory_store.put(adv)

        # 1 success, 2 failures
        for i in range(3):
            pack_id = f"pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_mixed"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": i == 0},
            )

        report = run_advisory_fitness_loop(
            event_log, advisory_store, min_presentations=3, blend_weight=0.3
        )

        updated = advisory_store.get("adv_mixed")
        assert updated is not None
        # success_rate = 1/3 ~= 0.333
        # new = 0.7 * 0.6 + 0.3 * 0.333 = 0.42 + 0.1 = 0.52
        assert updated.confidence < 0.6
        assert updated.confidence >= 0.1  # not suppressed
        assert "adv_mixed" not in report.advisories_boosted
        assert "adv_mixed" not in report.advisories_suppressed

    def test_empty_events_noop(self, event_log, advisory_store):
        """No events produces clean report, no modifications."""
        adv = _make_advisory("adv_noop", confidence=0.5)
        advisory_store.put(adv)

        report = run_advisory_fitness_loop(event_log, advisory_store)

        assert report.advisory_scores == []
        assert report.advisories_boosted == []
        assert report.advisories_suppressed == []
        # Original advisory unchanged
        assert advisory_store.get("adv_noop").confidence == 0.5

    def test_advisory_not_in_store_skipped(self, event_log, advisory_store):
        """Advisories in telemetry but not in store are safely skipped."""
        # Emit events with an advisory ID that doesn't exist in store
        for i in range(3):
            pack_id = f"pack-{i}"
            event_log.emit(
                EventType.PACK_ASSEMBLED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"advisory_ids": ["adv_ghost"]},
            )
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                source="test",
                entity_id=pack_id,
                entity_type="pack",
                payload={"pack_id": pack_id, "success": True},
            )

        report = run_advisory_fitness_loop(
            event_log, advisory_store, min_presentations=3
        )
        # Score computed, but no boost/suppress since advisory isn't in store
        assert len(report.advisory_scores) == 1
        assert report.advisories_boosted == []
        assert report.advisories_suppressed == []
