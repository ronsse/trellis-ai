"""Tests for extractor-fallback telemetry aggregation (Gap 4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.extract.telemetry import (
    ExtractorFallbackReport,
    analyze_extractor_fallbacks,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _emit_dispatch(
    log: SQLiteEventLog,
    *,
    source_hint: str | None,
    extractor: str = "stub",
    tier: str = "deterministic",
) -> None:
    log.emit(
        EventType.EXTRACTION_DISPATCHED,
        source="extraction_dispatcher",
        payload={
            "extractor_used": extractor,
            "tier": tier,
            "source_hint": source_hint,
            "entities": 1,
            "edges": 0,
            "llm_calls": 0,
            "tokens_used": 0,
            "overall_confidence": 1.0,
        },
    )


def _emit_fallback(
    log: SQLiteEventLog,
    *,
    source_hint: str | None,
    reason: str,
    chosen_tier: str = "llm",
    skipped_tier: str | None = "deterministic",
    chosen_extractor: str = "llm",
) -> None:
    log.emit(
        EventType.EXTRACTOR_FALLBACK,
        source="extraction_dispatcher",
        payload={
            "source_hint": source_hint,
            "chosen_extractor": chosen_extractor,
            "chosen_tier": chosen_tier,
            "skipped_tier": skipped_tier,
            "reason": reason,
        },
    )


class TestAnalyzeExtractorFallbacks:
    def test_empty_window_returns_note(self, event_log) -> None:
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert isinstance(report, ExtractorFallbackReport)
        assert report.total_dispatches == 0
        assert report.total_fallbacks == 0
        assert report.notes
        assert "No EXTRACTION_DISPATCHED" in report.notes[0]

    def test_counts_dispatches_and_fallbacks(self, event_log) -> None:
        for _ in range(5):
            _emit_dispatch(event_log, source_hint="dbt")
        _emit_fallback(event_log, source_hint="dbt", reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert report.total_dispatches == 5
        assert report.total_fallbacks == 1
        assert report.overall_fallback_rate == pytest.approx(0.2)

    def test_reason_counts_and_per_source(self, event_log) -> None:
        for _ in range(4):
            _emit_dispatch(event_log, source_hint="dbt")
            _emit_fallback(event_log, source_hint="dbt", reason="empty_result")
        _emit_dispatch(event_log, source_hint="lineage")
        _emit_fallback(event_log, source_hint="lineage", reason="prefer_tier_override")
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert report.reason_counts == {
            "empty_result": 4,
            "prefer_tier_override": 1,
        }
        by_source = {s.source_hint: s for s in report.per_source}
        assert by_source["dbt"].fallback_rate == pytest.approx(1.0)
        assert by_source["dbt"].reasons == {"empty_result": 4}
        assert by_source["lineage"].reasons == {"prefer_tier_override": 1}

    def test_source_without_dispatch_still_counted(self, event_log) -> None:
        # Fallback for a source that has no matching dispatch event
        _emit_fallback(event_log, source_hint="orphan", reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        by_source = {s.source_hint: s for s in report.per_source}
        assert by_source["orphan"].total_dispatches == 0
        assert by_source["orphan"].fallback_events == 1
        assert by_source["orphan"].fallback_rate == 0.0  # divide-by-zero fallback

    def test_none_source_hint_bucketed(self, event_log) -> None:
        _emit_dispatch(event_log, source_hint=None)
        _emit_fallback(event_log, source_hint=None, reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        by_source = {s.source_hint: s for s in report.per_source}
        assert "<none>" in by_source

    def test_finding_fires_on_high_empty_result_rate(self, event_log) -> None:
        # 12 dispatches with 10 empty_result fallbacks → 83% rate
        for _ in range(12):
            _emit_dispatch(event_log, source_hint="rules-heavy")
        for _ in range(10):
            _emit_fallback(event_log, source_hint="rules-heavy", reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert any("rules-heavy" in f and "empty_result" in f for f in report.findings)

    def test_finding_fires_on_high_prefer_tier_override(self, event_log) -> None:
        for _ in range(15):
            _emit_dispatch(event_log, source_hint="manual-override")
        for _ in range(12):
            _emit_fallback(
                event_log,
                source_hint="manual-override",
                reason="prefer_tier_override",
            )
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert any(
            "manual-override" in f and "lower tier" in f for f in report.findings
        )

    def test_finding_suppressed_below_sample_floor(self, event_log) -> None:
        # 5 dispatches, 5 fallbacks → 100% rate but below 10-sample floor
        for _ in range(5):
            _emit_dispatch(event_log, source_hint="sparse")
            _emit_fallback(event_log, source_hint="sparse", reason="empty_result")
        report = analyze_extractor_fallbacks(event_log, days=30)
        assert not any("sparse" in f for f in report.findings)
