"""Tests for AdvisoryGenerator."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from trellis.retrieve.advisory_generator import AdvisoryGenerator
from trellis.schemas.advisory import AdvisoryCategory
from trellis.stores.advisory_store import AdvisoryStore
from trellis.stores.base.event_log import EventLog, EventType


def _event(
    event_type: EventType,
    entity_id: str,
    payload: dict,
) -> MagicMock:
    """Create a mock Event."""
    ev = MagicMock()
    ev.event_type = event_type
    ev.entity_id = entity_id
    ev.payload = payload
    ev.occurred_at = datetime.now(tz=UTC)
    return ev


def _pack_event(
    pack_id: str,
    item_ids: list[str],
    *,
    domain: str = "global",
    intent: str = "test query",
    items: list[dict] | None = None,
) -> MagicMock:
    """Create a PACK_ASSEMBLED event."""
    payload: dict = {
        "injected_item_ids": item_ids,
        "domain": domain,
        "intent": intent,
        "strategies_used": ["keyword"],
    }
    if items is not None:
        payload["injected_items"] = items
    return _event(EventType.PACK_ASSEMBLED, pack_id, payload)


def _feedback_event(
    pack_id: str,
    *,
    success: bool,
) -> MagicMock:
    """Create a FEEDBACK_RECORDED event."""
    return _event(
        EventType.FEEDBACK_RECORDED,
        pack_id,
        {"pack_id": pack_id, "success": success},
    )


class TestAdvisoryGeneratorEmpty:
    """Generator produces empty report with no data."""

    def test_no_events(self, tmp_path: Path) -> None:
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.return_value = []
        store = AdvisoryStore(tmp_path / "adv.json")

        gen = AdvisoryGenerator(event_log, store)
        report = gen.generate(days=30)

        assert report.advisories_generated == 0
        assert report.advisories_stored == 0

    def test_packs_without_feedback(self, tmp_path: Path) -> None:
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [
            [_pack_event("p1", ["a", "b"])],  # pack events
            [],  # no feedback
        ]
        store = AdvisoryStore(tmp_path / "adv.json")

        gen = AdvisoryGenerator(event_log, store)
        report = gen.generate(days=30)

        assert report.advisories_generated == 0
        assert report.total_packs == 1
        assert report.total_feedback == 0


class TestEntityCorrelation:
    """Entity correlation finds items disproportionately in successes."""

    def test_entity_with_high_success_rate(self, tmp_path: Path) -> None:
        """An item appearing mostly in successful packs → ENTITY advisory."""
        packs = []
        feedback = []
        # Item "good_entity" in 8 successful packs, 1 failure
        for i in range(8):
            packs.append(_pack_event(f"p{i}", ["good_entity", f"other{i}"]))
            feedback.append(_feedback_event(f"p{i}", success=True))

        packs.append(_pack_event("p8", ["good_entity"]))
        feedback.append(_feedback_event("p8", success=False))

        # 5 packs without "good_entity" — 2 success, 3 failure
        for i in range(9, 14):
            packs.append(_pack_event(f"p{i}", [f"other{i}"]))
            feedback.append(_feedback_event(f"p{i}", success=(i < 11)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        entity_advs = [
            a
            for a in store.list()
            if a.category == AdvisoryCategory.ENTITY and a.entity_id == "good_entity"
        ]
        assert len(entity_advs) >= 1
        adv = entity_advs[0]
        assert adv.evidence.success_rate_with > 0.8
        assert adv.evidence.effect_size > 0

    def test_no_advisory_below_min_sample(self, tmp_path: Path) -> None:
        """Items with too few appearances produce no advisory."""
        packs = [_pack_event("p1", ["rare_item"])]
        feedback = [_feedback_event("p1", success=True)]

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=5)
        report = gen.generate(days=30)

        assert report.advisories_generated == 0


class TestAntiPatternDetection:
    """Anti-pattern detection finds items disproportionately in failures."""

    def test_item_correlating_with_failure(self, tmp_path: Path) -> None:
        packs = []
        feedback = []
        # "bad_entity" in 8 failed packs, 1 success
        for i in range(8):
            packs.append(_pack_event(f"p{i}", ["bad_entity"]))
            feedback.append(_feedback_event(f"p{i}", success=False))

        packs.append(_pack_event("p8", ["bad_entity"]))
        feedback.append(_feedback_event("p8", success=True))

        # 5 packs without "bad_entity" — 4 success, 1 failure
        for i in range(9, 14):
            packs.append(_pack_event(f"p{i}", [f"other{i}"]))
            feedback.append(_feedback_event(f"p{i}", success=(i < 13)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        anti_advs = [
            a
            for a in store.list()
            if a.category == AdvisoryCategory.ANTI_PATTERN
            and a.entity_id == "bad_entity"
        ]
        assert len(anti_advs) >= 1
        adv = anti_advs[0]
        assert adv.evidence.effect_size < 0


class TestStrategyCorrelation:
    """Strategy correlation finds strategies with outcome differences."""

    def test_strategy_with_high_success(self, tmp_path: Path) -> None:
        packs = []
        feedback = []
        # Packs with semantic strategy → mostly success
        for i in range(6):
            packs.append(
                _pack_event(
                    f"p{i}",
                    [f"item{i}"],
                    items=[{"strategy_source": "semantic"}],
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=True))

        # Packs without semantic → mostly failure
        for i in range(6, 12):
            packs.append(
                _pack_event(
                    f"p{i}",
                    [f"item{i}"],
                    items=[{"strategy_source": "keyword"}],
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=(i == 6)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        approach_advs = [
            a for a in store.list() if a.category == AdvisoryCategory.APPROACH
        ]
        assert len(approach_advs) >= 1


class TestScopeAnalysis:
    """Scope analysis compares pack breadth with outcomes."""

    def test_narrow_packs_better(self, tmp_path: Path) -> None:
        packs = []
        feedback = []
        # Small packs (3 items) → 5/6 success
        for i in range(6):
            packs.append(_pack_event(f"s{i}", [f"a{i}", f"b{i}", f"c{i}"]))
            feedback.append(_feedback_event(f"s{i}", success=(i < 5)))

        # Large packs (20 items) → 1/6 success
        for i in range(6):
            items = [f"item{j}" for j in range(20)]
            packs.append(_pack_event(f"l{i}", items))
            feedback.append(_feedback_event(f"l{i}", success=(i == 0)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        scope_advs = [a for a in store.list() if a.category == AdvisoryCategory.SCOPE]
        assert len(scope_advs) >= 1
        assert scope_advs[0].evidence.effect_size > 0


class TestQueryImprovement:
    """Query improvement finds keywords that correlate with success."""

    def test_keyword_correlation(self, tmp_path: Path) -> None:
        packs = []
        feedback = []
        # Packs with "deployment" in intent → mostly success
        for i in range(6):
            packs.append(
                _pack_event(
                    f"p{i}",
                    [f"item{i}"],
                    intent="deployment checklist review",
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=True))

        # Packs without "deployment" → mostly failure
        for i in range(6, 12):
            packs.append(
                _pack_event(
                    f"p{i}",
                    [f"item{i}"],
                    intent="general task review",
                )
            )
            feedback.append(_feedback_event(f"p{i}", success=(i == 6)))

        event_log = MagicMock(spec=EventLog)
        event_log.get_events.side_effect = [packs, feedback]

        store = AdvisoryStore(tmp_path / "adv.json")
        gen = AdvisoryGenerator(event_log, store, min_sample_size=3)
        gen.generate(days=30)

        query_advs = [a for a in store.list() if a.category == AdvisoryCategory.QUERY]
        # "deployment" should appear as a correlating keyword
        deployment_advs = [a for a in query_advs if "deployment" in a.message.lower()]
        assert len(deployment_advs) >= 1


class TestConfidenceComputation:
    """Confidence scales with sample size and effect size."""

    def test_low_sample_low_confidence(self, tmp_path: Path) -> None:
        gen = AdvisoryGenerator.__new__(AdvisoryGenerator)
        # n=2, effect=0.5 → sample_factor=0.2, effect_factor=1.0 → 0.2
        assert gen._compute_confidence(2, 0.5) == 0.2

    def test_high_sample_high_confidence(self, tmp_path: Path) -> None:
        gen = AdvisoryGenerator.__new__(AdvisoryGenerator)
        # n=20, effect=0.5 → sample_factor=1.0, effect_factor=1.0 → 1.0
        assert gen._compute_confidence(20, 0.5) == 1.0

    def test_weak_effect_low_confidence(self, tmp_path: Path) -> None:
        gen = AdvisoryGenerator.__new__(AdvisoryGenerator)
        # n=20, effect=0.1 → sample_factor=1.0, effect_factor=0.2 → 0.2
        assert gen._compute_confidence(20, 0.1) == 0.2


class TestAdvisoryReport:
    """AdvisoryReport captures generation metadata."""

    def test_report_fields(self, tmp_path: Path) -> None:
        event_log = MagicMock(spec=EventLog)
        event_log.get_events.return_value = []
        store = AdvisoryStore(tmp_path / "adv.json")

        gen = AdvisoryGenerator(event_log, store)
        report = gen.generate(days=7)

        assert report.analysis_window_days == 7
        assert report.total_packs == 0
        assert report.total_feedback == 0
        assert report.advisories_generated == 0
        assert report.advisories_stored == 0
