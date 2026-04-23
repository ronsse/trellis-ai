"""Tests for PACK_ASSEMBLED telemetry aggregation (Gap 3.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.retrieve.telemetry import (
    PackTelemetryReport,
    analyze_pack_telemetry,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _emit_pack(
    log: SQLiteEventLog,
    *,
    pack_id: str,
    injected: list[dict] | None = None,
    rejected: list[dict] | None = None,
    strategies: list[str] | None = None,
) -> None:
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="pack_builder",
        entity_id=pack_id,
        entity_type="pack",
        payload={
            "items_count": len(injected or []),
            "injected_items": injected or [],
            "rejected_items": rejected or [],
            "strategies_used": strategies or [],
            "budget_max_items": 5,
            "budget_max_tokens": 100,
        },
    )


class TestAnalyzePackTelemetry:
    def test_empty_window_returns_note(self, event_log) -> None:
        report = analyze_pack_telemetry(event_log, days=7)
        assert isinstance(report, PackTelemetryReport)
        assert report.total_packs == 0
        assert report.notes
        assert "No PACK_ASSEMBLED" in report.notes[0]

    def test_counts_injected_and_rejected(self, event_log) -> None:
        _emit_pack(
            event_log,
            pack_id="p1",
            injected=[{"item_id": "a", "strategy_source": "keyword"}],
            rejected=[
                {"item_id": "b", "reason": "dedup", "strategy_source": "semantic"},
                {
                    "item_id": "c",
                    "reason": "max_items",
                    "strategy_source": "keyword",
                },
            ],
        )
        report = analyze_pack_telemetry(event_log, days=7)
        assert report.total_packs == 1
        assert report.total_injected_items == 1
        assert report.total_rejected_items == 2
        assert report.mean_items_per_pack == pytest.approx(1.0)
        assert report.mean_rejected_per_pack == pytest.approx(2.0)

    def test_budget_hit_rates(self, event_log) -> None:
        # pack 1: hits max_items only
        _emit_pack(
            event_log,
            pack_id="p1",
            injected=[],
            rejected=[{"reason": "max_items", "strategy_source": "s"}],
        )
        # pack 2: hits token_budget only
        _emit_pack(
            event_log,
            pack_id="p2",
            injected=[],
            rejected=[{"reason": "token_budget", "strategy_source": "s"}],
        )
        # pack 3: neither
        _emit_pack(
            event_log,
            pack_id="p3",
            injected=[{"strategy_source": "s"}],
            rejected=[{"reason": "dedup", "strategy_source": "s"}],
        )
        # pack 4: both budget hits
        _emit_pack(
            event_log,
            pack_id="p4",
            injected=[],
            rejected=[
                {"reason": "max_items", "strategy_source": "s"},
                {"reason": "token_budget", "strategy_source": "s"},
            ],
        )
        report = analyze_pack_telemetry(event_log, days=7)
        assert report.total_packs == 4
        assert report.max_items_hit_rate == pytest.approx(0.5)
        assert report.max_tokens_hit_rate == pytest.approx(0.5)
        assert report.any_budget_hit_rate == pytest.approx(0.75)

    def test_rejection_reason_distribution(self, event_log) -> None:
        _emit_pack(
            event_log,
            pack_id="p1",
            rejected=[
                {"reason": "dedup", "strategy_source": "k"},
                {"reason": "dedup", "strategy_source": "k"},
                {"reason": "max_items", "strategy_source": "k"},
            ],
        )
        report = analyze_pack_telemetry(event_log, days=7)
        assert report.rejection_reason_counts["dedup"] == 2
        assert report.rejection_reason_counts["max_items"] == 1
        assert report.rejection_reason_rates["dedup"] == pytest.approx(2 / 3)

    def test_strategy_contribution(self, event_log) -> None:
        _emit_pack(
            event_log,
            pack_id="p1",
            injected=[
                {"strategy_source": "keyword"},
                {"strategy_source": "keyword"},
                {"strategy_source": "semantic"},
            ],
            rejected=[
                {"reason": "dedup", "strategy_source": "keyword"},
                {"reason": "max_items", "strategy_source": "semantic"},
                {"reason": "token_budget", "strategy_source": "semantic"},
            ],
        )
        report = analyze_pack_telemetry(event_log, days=7)
        contribs = {c.strategy: c for c in report.strategy_contributions}
        kw = contribs["keyword"]
        assert kw.injected == 2
        assert kw.rejected == 1
        assert kw.yield_rate == pytest.approx(2 / 3)
        sem = contribs["semantic"]
        assert sem.injected == 1
        assert sem.rejected == 2
        assert sem.yield_rate == pytest.approx(1 / 3)

    def test_missing_strategy_source_bucketed_as_unknown(self, event_log) -> None:
        _emit_pack(
            event_log,
            pack_id="p1",
            injected=[{"item_id": "a"}],
            rejected=[{"reason": "dedup"}],
        )
        report = analyze_pack_telemetry(event_log, days=7)
        unknown = {c.strategy: c for c in report.strategy_contributions}["unknown"]
        assert unknown.injected == 1
        assert unknown.rejected == 1

    def test_finding_fires_on_high_budget_saturation(self, event_log) -> None:
        for i in range(10):
            _emit_pack(
                event_log,
                pack_id=f"p-{i}",
                injected=[],
                rejected=[{"reason": "max_items", "strategy_source": "keyword"}],
            )
        report = analyze_pack_telemetry(event_log, days=7)
        assert report.max_items_hit_rate == pytest.approx(1.0)
        assert any("max_items" in f for f in report.findings)

    def test_finding_fires_on_low_strategy_yield(self, event_log) -> None:
        # Strategy "noisy" contributes 5 injected / 25 seen = 20% yield
        for i in range(5):
            _emit_pack(
                event_log,
                pack_id=f"p-{i}",
                injected=[{"strategy_source": "noisy"}],
                rejected=[
                    {"reason": "dedup", "strategy_source": "noisy"},
                    {"reason": "dedup", "strategy_source": "noisy"},
                    {"reason": "dedup", "strategy_source": "noisy"},
                    {"reason": "dedup", "strategy_source": "noisy"},
                ],
            )
        report = analyze_pack_telemetry(event_log, days=7)
        noisy = {c.strategy: c for c in report.strategy_contributions}["noisy"]
        assert noisy.yield_rate < 0.25
        assert any("noisy" in f for f in report.findings)

    def test_top_rejection_reasons_per_strategy(self, event_log) -> None:
        _emit_pack(
            event_log,
            pack_id="p1",
            rejected=[
                {"reason": "dedup", "strategy_source": "keyword"},
                {"reason": "dedup", "strategy_source": "keyword"},
                {"reason": "dedup", "strategy_source": "keyword"},
                {"reason": "max_items", "strategy_source": "keyword"},
            ],
        )
        report = analyze_pack_telemetry(event_log, days=7)
        kw = {c.strategy: c for c in report.strategy_contributions}["keyword"]
        assert kw.top_rejection_reasons[0] == ("dedup", 3)
        assert ("max_items", 1) in kw.top_rejection_reasons
