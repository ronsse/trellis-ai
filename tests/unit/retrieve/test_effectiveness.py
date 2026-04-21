"""Tests for context effectiveness analysis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.retrieve.effectiveness import (
    analyze_advisory_effectiveness,
    analyze_effectiveness,
    run_advisory_fitness_loop,
    run_effectiveness_feedback,
)
from trellis.schemas.advisory import (
    Advisory,
    AdvisoryCategory,
    AdvisoryEvidence,
    DriftPattern,
)
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import Event, EventType
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

        # new = 0.7 * 0.12 + 0.3 * 0.0 = 0.084 < 0.1 threshold.
        # Post-gap-2.1 fix: suppression is soft — the advisory is kept in
        # the store with SUPPRESSED status so it can be restored if later
        # evidence vindicates it. It's excluded from retrieval by default.
        from trellis.schemas.advisory import AdvisoryStatus

        persisted = advisory_store.get("adv_suppress")
        assert persisted is not None
        assert persisted.status == AdvisoryStatus.SUPPRESSED
        assert persisted.suppressed_at is not None
        assert persisted.suppression_reason is not None
        assert "adv_suppress" not in [a.advisory_id for a in advisory_store.list()]
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


def _append_pack_pair(
    event_log,
    pack_id: str,
    *,
    advisory_ids: list[str],
    success: bool,
    occurred_at: datetime | None = None,
) -> None:
    """Append a PACK_ASSEMBLED + FEEDBACK_RECORDED pair.

    When ``occurred_at`` is ``None`` the event log stamps ``now`` via
    ``emit()``. Drift tests (gap 2.4) key off ``Event.occurred_at`` so
    pass an explicit timestamp to control the window filter.
    """
    if occurred_at is None:
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={"advisory_ids": advisory_ids},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={"pack_id": pack_id, "success": success},
        )
        return
    event_log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            occurred_at=occurred_at,
            payload={"advisory_ids": advisory_ids},
        )
    )
    event_log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            occurred_at=occurred_at,
            payload={"pack_id": pack_id, "success": success},
        )
    )


def _emit_outcomes(
    event_log,
    advisory_id: str,
    *,
    successes: int,
    failures: int,
) -> None:
    """Emit ``successes`` winning + ``failures`` losing PACK/FEEDBACK pairs."""
    for count, success in enumerate(([True] * successes) + ([False] * failures)):
        _append_pack_pair(
            event_log,
            f"pack-{advisory_id}-{count}",
            advisory_ids=[advisory_id],
            success=success,
        )


# ---------------------------------------------------------------------------
# Gap 2.1 — suppression is reversible (soft-suppress + auto-restore + audit)
# ---------------------------------------------------------------------------


class TestAdvisorySuppressionReversibility:
    """The fitness loop no longer hard-deletes failing advisories; it
    soft-suppresses them so later evidence can restore them."""

    def test_suppression_is_recorded_in_event_log(self, event_log, advisory_store):
        """An ADVISORY_SUPPRESSED event is emitted for audit."""
        adv = _make_advisory("adv_audit", confidence=0.12)
        advisory_store.put(adv)
        _emit_outcomes(event_log, "adv_audit", successes=0, failures=3)

        run_advisory_fitness_loop(
            event_log,
            advisory_store,
            min_presentations=3,
            suppress_below=0.1,
            blend_weight=0.3,
        )

        events = event_log.get_events(event_type=EventType.ADVISORY_SUPPRESSED)
        assert len(events) == 1
        assert events[0].entity_id == "adv_audit"
        assert events[0].payload["advisory_id"] == "adv_audit"
        assert events[0].payload["old_confidence"] == 0.12
        assert "reason" in events[0].payload

    def test_manual_restore_after_fitness_loop_suppression(
        self, event_log, advisory_store
    ):
        """An operator can restore a suppressed advisory by id."""
        from trellis.schemas.advisory import AdvisoryStatus

        adv = _make_advisory("adv_manual", confidence=0.12)
        advisory_store.put(adv)
        _emit_outcomes(event_log, "adv_manual", successes=0, failures=3)

        run_advisory_fitness_loop(
            event_log,
            advisory_store,
            min_presentations=3,
            suppress_below=0.1,
            blend_weight=0.3,
        )
        assert advisory_store.get("adv_manual").status == AdvisoryStatus.SUPPRESSED  # type: ignore[union-attr]

        restored = advisory_store.restore("adv_manual")
        assert restored is not None
        assert restored.status == AdvisoryStatus.ACTIVE
        assert "adv_manual" in [a.advisory_id for a in advisory_store.list()]

    def test_auto_restore_when_evidence_recovers(self, event_log, advisory_store):
        """When a suppressed advisory's fitness climbs above
        suppress_below + hysteresis, the fitness loop auto-restores it."""
        from trellis.schemas.advisory import AdvisoryStatus

        # Start the advisory in suppressed state with low confidence.
        adv = _make_advisory("adv_rebound", confidence=0.05)
        advisory_store.put(adv)
        advisory_store.suppress("adv_rebound", reason="initial suppression")
        assert advisory_store.get("adv_rebound").status == AdvisoryStatus.SUPPRESSED  # type: ignore[union-attr]

        # Fresh evidence shows consistent success.
        _emit_outcomes(event_log, "adv_rebound", successes=3, failures=0)

        report = run_advisory_fitness_loop(
            event_log,
            advisory_store,
            min_presentations=3,
            suppress_below=0.1,
            blend_weight=0.5,  # high blend so one round can push confidence up
        )

        # new_confidence = 0.5 * 0.05 + 0.5 * 1.0 = 0.525
        # restore_above = 0.1 + 0.05 = 0.15 → well below 0.525 → restore fires
        assert "adv_rebound" in report.advisories_restored
        restored = advisory_store.get("adv_rebound")
        assert restored is not None
        assert restored.status == AdvisoryStatus.ACTIVE
        assert restored.suppressed_at is None

        events = event_log.get_events(event_type=EventType.ADVISORY_RESTORED)
        assert len(events) == 1
        assert events[0].entity_id == "adv_rebound"

    def test_suppressed_advisory_stays_suppressed_without_hysteresis_margin(
        self, event_log, advisory_store
    ):
        """Confidence climbing above suppress_below but within the
        hysteresis band does NOT auto-restore (prevents flapping)."""
        from trellis.schemas.advisory import AdvisoryStatus

        adv = _make_advisory("adv_hold", confidence=0.09)
        advisory_store.put(adv)
        advisory_store.suppress("adv_hold", reason="narrow miss")

        # Add weak evidence — 1 success, 2 failures → success_rate ≈ 0.333.
        # new = 0.7 * 0.09 + 0.3 * 0.333 = 0.063 + 0.100 = 0.163
        # restore_above = 0.1 + 0.05 = 0.15 → 0.163 > 0.15 → would restore.
        # Tune to land *inside* the hysteresis band instead:
        # with 0 successes and 1 failure out of 3 total, we need presentations=3.
        # Just barely above threshold but below restore_above.
        # new = 0.7 * 0.09 + 0.3 * (1/3) = 0.063 + 0.100 = 0.163. > 0.15.
        # Use confidence=0.05 to land in band:
        # new = 0.7*0.05 + 0.3*(1/3) = 0.035 + 0.100 = 0.135. < 0.15 ✓
        # and still > suppress_below=0.1 ✓
        adv_in_band = _make_advisory("adv_band", confidence=0.05)
        advisory_store.put(adv_in_band)
        advisory_store.suppress("adv_band", reason="band test")
        _emit_outcomes(event_log, "adv_band", successes=1, failures=2)

        report = run_advisory_fitness_loop(
            event_log,
            advisory_store,
            min_presentations=3,
            suppress_below=0.1,
            blend_weight=0.3,
        )

        assert "adv_band" not in report.advisories_restored
        assert advisory_store.get("adv_band").status == AdvisoryStatus.SUPPRESSED  # type: ignore[union-attr]

    def test_report_includes_restored_field(self, event_log, advisory_store):
        """AdvisoryEffectivenessReport gained an `advisories_restored` list."""
        report = run_advisory_fitness_loop(
            event_log, advisory_store, min_presentations=3
        )
        assert report.advisories_restored == []


# ---------------------------------------------------------------------------
# Gap 2.4 — drift detection (regime-shift signal distinct from smoothed
# confidence updates)
# ---------------------------------------------------------------------------


class TestAdvisoryDriftDetection:
    """Gap 2.4 — smoothed confidence masks regime shifts. The drift
    detector runs a recent-vs-full windowed comparison and raises
    ``ADVISORY_DRIFT_DETECTED`` for operator review."""

    def test_regime_shift_decline_is_flagged(self, event_log, advisory_store):
        """Old packs succeed, recent packs fail → regime shift alert."""
        adv = _make_advisory("adv_decline", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        old = now - timedelta(days=20)  # outside 7d recent window
        recent = now - timedelta(days=2)

        # 5 old successes, 5 recent failures → full rate 0.5, recent 0.0
        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-old-{i}",
                advisory_ids=["adv_decline"],
                success=True,
                occurred_at=old,
            )
        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-recent-{i}",
                advisory_ids=["adv_decline"],
                success=False,
                occurred_at=recent,
            )

        report = analyze_advisory_effectiveness(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            drift_window_days=7,
        )

        assert len(report.advisories_drifting) == 1
        alert = report.advisories_drifting[0]
        assert alert.advisory_id == "adv_decline"
        assert alert.pattern == DriftPattern.REGIME_SHIFT_DECLINE
        assert alert.full_success_rate == 0.5
        assert alert.recent_success_rate == 0.0
        assert alert.recent_presentations == 5
        assert alert.full_presentations == 10
        assert alert.window_days == 30
        assert alert.recent_window_days == 7

    def test_lift_sign_flip_is_flagged(self, event_log, advisory_store):
        """Advisory was helpful historically, harmful recently → sign flip."""
        adv = _make_advisory("adv_flip", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        old = now - timedelta(days=20)
        recent = now - timedelta(days=2)

        # Old: adv wins (4 wins with adv, 1 loss without) → lift positive
        for i in range(4):
            _append_pack_pair(
                event_log,
                f"pack-old-adv-{i}",
                advisory_ids=["adv_flip"],
                success=True,
                occurred_at=old,
            )
        _append_pack_pair(
            event_log,
            "pack-old-none",
            advisory_ids=[],
            success=False,
            occurred_at=old,
        )

        # Recent: adv loses (0 wins with adv across 3 presentations,
        # 1 win without) → lift negative and magnitude > 0.1
        for i in range(3):
            _append_pack_pair(
                event_log,
                f"pack-recent-adv-{i}",
                advisory_ids=["adv_flip"],
                success=False,
                occurred_at=recent,
            )
        _append_pack_pair(
            event_log,
            "pack-recent-none",
            advisory_ids=[],
            success=True,
            occurred_at=recent,
        )

        report = analyze_advisory_effectiveness(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            drift_window_days=7,
        )

        # Full rate: 4/7 ≈ 0.571; recent rate 0/3 = 0. Drop is > 0.25
        # so this also trips the regime-shift rule, which wins by policy
        # (stronger signal when both fire).
        assert len(report.advisories_drifting) == 1
        alert = report.advisories_drifting[0]
        assert alert.advisory_id == "adv_flip"
        assert alert.pattern == DriftPattern.REGIME_SHIFT_DECLINE
        assert alert.full_lift > 0
        assert alert.recent_lift < 0

    def test_pure_sign_flip_without_large_decline(self, event_log, advisory_store):
        """Sign flip with a smaller rate drop → sign-flip pattern label."""
        adv = _make_advisory("adv_pure_flip", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        old = now - timedelta(days=20)
        recent = now - timedelta(days=2)

        # Old: adv 3 successes / 1 failure → 75% with adv.
        # Without adv: 1 success / 3 failures → 25% baseline.
        # Old lift is strongly positive.
        for i in range(3):
            _append_pack_pair(
                event_log,
                f"pack-old-adv-s-{i}",
                advisory_ids=["adv_pure_flip"],
                success=True,
                occurred_at=old,
            )
        _append_pack_pair(
            event_log,
            "pack-old-adv-f",
            advisory_ids=["adv_pure_flip"],
            success=False,
            occurred_at=old,
        )
        _append_pack_pair(
            event_log,
            "pack-old-none-s",
            advisory_ids=[],
            success=True,
            occurred_at=old,
        )
        for i in range(3):
            _append_pack_pair(
                event_log,
                f"pack-old-none-f-{i}",
                advisory_ids=[],
                success=False,
                occurred_at=old,
            )

        # Recent: adv 2 successes / 1 failure (67% — close to 75%).
        # Without adv: 3 successes / 0 failures (100% — much higher).
        # Recent lift flips negative, but recent adv success_rate stays
        # close to the full-window rate so regime-shift rule does not
        # fire.
        for i in range(2):
            _append_pack_pair(
                event_log,
                f"pack-recent-adv-s-{i}",
                advisory_ids=["adv_pure_flip"],
                success=True,
                occurred_at=recent,
            )
        _append_pack_pair(
            event_log,
            "pack-recent-adv-f",
            advisory_ids=["adv_pure_flip"],
            success=False,
            occurred_at=recent,
        )
        for i in range(3):
            _append_pack_pair(
                event_log,
                f"pack-recent-none-s-{i}",
                advisory_ids=[],
                success=True,
                occurred_at=recent,
            )

        report = analyze_advisory_effectiveness(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            drift_window_days=7,
        )

        assert len(report.advisories_drifting) == 1
        alert = report.advisories_drifting[0]
        assert alert.pattern == DriftPattern.LIFT_SIGN_FLIP
        assert alert.full_lift > 0
        assert alert.recent_lift < 0
        # Confirm this case does NOT meet the regime-shift rate-drop bar
        assert alert.full_success_rate - alert.recent_success_rate < 0.25

    def test_stable_advisory_is_not_flagged(self, event_log, advisory_store):
        """Consistent success across windows → no alert."""
        adv = _make_advisory("adv_stable", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        old = now - timedelta(days=20)
        recent = now - timedelta(days=2)

        for i in range(3):
            _append_pack_pair(
                event_log,
                f"pack-old-{i}",
                advisory_ids=["adv_stable"],
                success=True,
                occurred_at=old,
            )
        for i in range(3):
            _append_pack_pair(
                event_log,
                f"pack-recent-{i}",
                advisory_ids=["adv_stable"],
                success=True,
                occurred_at=recent,
            )

        report = analyze_advisory_effectiveness(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            drift_window_days=7,
        )
        assert report.advisories_drifting == []

    def test_drift_window_skipped_when_unset(self, event_log, advisory_store):
        """drift_window_days=None disables drift analysis entirely."""
        adv = _make_advisory("adv_nodrift", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        old = now - timedelta(days=20)
        recent = now - timedelta(days=2)

        # Same setup as regime-shift test — would normally fire.
        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-old-{i}",
                advisory_ids=["adv_nodrift"],
                success=True,
                occurred_at=old,
            )
        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-recent-{i}",
                advisory_ids=["adv_nodrift"],
                success=False,
                occurred_at=recent,
            )

        report = analyze_advisory_effectiveness(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            drift_window_days=None,
        )
        assert report.advisories_drifting == []

    def test_drift_window_skipped_when_equal_or_larger_than_full(
        self, event_log, advisory_store
    ):
        """drift_window_days >= days is treated as 'no sub-window'."""
        adv = _make_advisory("adv_fullwindow", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        recent = now - timedelta(days=2)

        for i in range(6):
            _append_pack_pair(
                event_log,
                f"pack-{i}",
                advisory_ids=["adv_fullwindow"],
                success=(i < 3),
                occurred_at=recent,
            )

        # drift_window_days == days → no meaningful sub-window
        report = analyze_advisory_effectiveness(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            drift_window_days=30,
        )
        assert report.advisories_drifting == []

        # drift_window_days > days → also disabled
        report = analyze_advisory_effectiveness(
            event_log,
            advisory_store,
            days=7,
            min_presentations=3,
            drift_window_days=30,
        )
        assert report.advisories_drifting == []

    def test_drift_requires_minimum_recent_presentations(
        self, event_log, advisory_store
    ):
        """Advisory with 1 recent presentation is not flagged (noise floor)."""
        adv = _make_advisory("adv_sparse", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        old = now - timedelta(days=20)
        recent = now - timedelta(days=2)

        # 5 old successes, 1 recent failure → too sparse for drift call
        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-old-{i}",
                advisory_ids=["adv_sparse"],
                success=True,
                occurred_at=old,
            )
        _append_pack_pair(
            event_log,
            "pack-recent-only",
            advisory_ids=["adv_sparse"],
            success=False,
            occurred_at=recent,
        )

        report = analyze_advisory_effectiveness(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            drift_window_days=7,
        )
        assert report.advisories_drifting == []

    def test_fitness_loop_emits_drift_detected_event(
        self, event_log, advisory_store
    ):
        """run_advisory_fitness_loop emits ADVISORY_DRIFT_DETECTED per alert."""
        adv = _make_advisory("adv_emit", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        old = now - timedelta(days=20)
        recent = now - timedelta(days=2)

        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-old-{i}",
                advisory_ids=["adv_emit"],
                success=True,
                occurred_at=old,
            )
        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-recent-{i}",
                advisory_ids=["adv_emit"],
                success=False,
                occurred_at=recent,
            )

        report = run_advisory_fitness_loop(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            blend_weight=0.3,
            drift_window_days=7,
        )

        assert len(report.advisories_drifting) == 1

        events = event_log.get_events(event_type=EventType.ADVISORY_DRIFT_DETECTED)
        assert len(events) == 1
        evt = events[0]
        assert evt.entity_id == "adv_emit"
        assert evt.entity_type == "advisory"
        assert evt.payload["pattern"] == DriftPattern.REGIME_SHIFT_DECLINE
        assert evt.payload["full_success_rate"] == 0.5
        assert evt.payload["recent_success_rate"] == 0.0
        assert evt.payload["window_days"] == 30
        assert evt.payload["recent_window_days"] == 7

    def test_fitness_loop_still_updates_confidence_despite_drift(
        self, event_log, advisory_store
    ):
        """Drift is an operator signal — confidence update still runs.

        The smoothed blend continues because the drift event is the
        operator's cue to investigate; gating the automated tuning on
        human review is a separate design call (see 2.2 deferred)."""
        adv = _make_advisory("adv_drift_update", confidence=0.5)
        advisory_store.put(adv)

        now = datetime.now(tz=UTC)
        old = now - timedelta(days=20)
        recent = now - timedelta(days=2)

        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-old-{i}",
                advisory_ids=["adv_drift_update"],
                success=True,
                occurred_at=old,
            )
        for i in range(5):
            _append_pack_pair(
                event_log,
                f"pack-recent-{i}",
                advisory_ids=["adv_drift_update"],
                success=False,
                occurred_at=recent,
            )

        run_advisory_fitness_loop(
            event_log,
            advisory_store,
            days=30,
            min_presentations=3,
            blend_weight=0.3,
            drift_window_days=7,
        )

        # Full-window success_rate = 5/10 = 0.5
        # new_confidence = 0.7 * 0.5 + 0.3 * 0.5 = 0.5 (unchanged)
        # — still executes the blend update as designed.
        updated = advisory_store.get("adv_drift_update")
        assert updated is not None
        assert updated.confidence == 0.5
