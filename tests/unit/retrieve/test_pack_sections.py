"""Tests for sectioned pack composition analysis."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.retrieve.pack_sections import analyze_pack_sections
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _emit_sectioned(
    log: SQLiteEventLog,
    *,
    pack_id: str,
    sections: list[dict],
) -> None:
    log.emit(
        EventType.PACK_ASSEMBLED,
        source="pack_builder",
        entity_id=pack_id,
        entity_type="sectioned_pack",
        payload={
            "intent": "test",
            "section_count": len(sections),
            "total_items": sum(s.get("items_count", 0) for s in sections),
            "sections": sections,
        },
    )


class TestAnalyzePackSections:
    def test_empty_window(self, event_log: SQLiteEventLog) -> None:
        report = analyze_pack_sections(event_log, days=30)
        assert report.total_sectioned_packs == 0
        assert report.section_stats == []
        assert report.empty_section_flags == []

    def test_aggregates_section_stats(self, event_log: SQLiteEventLog) -> None:
        _emit_sectioned(
            event_log,
            pack_id="p1",
            sections=[
                {"name": "domain", "items_count": 3, "item_ids": ["a", "b", "c"]},
                {"name": "tactical", "items_count": 2, "item_ids": ["x", "y"]},
            ],
        )
        _emit_sectioned(
            event_log,
            pack_id="p2",
            sections=[
                {"name": "domain", "items_count": 1, "item_ids": ["a"]},
                {"name": "tactical", "items_count": 0, "item_ids": []},
            ],
        )
        report = analyze_pack_sections(event_log, days=30)
        assert report.total_sectioned_packs == 2
        by_name = {s.name: s for s in report.section_stats}
        assert by_name["domain"].packs_count == 2
        assert by_name["domain"].total_items == 4
        assert by_name["domain"].unique_items == 3  # a, b, c deduped across packs
        assert by_name["tactical"].empty_count == 1
        assert by_name["tactical"].empty_rate == 0.5
        assert by_name["domain"].avg_items == 2.0

    def test_empty_rate_flag(self, event_log: SQLiteEventLog) -> None:
        for i in range(4):
            _emit_sectioned(
                event_log,
                pack_id=f"p{i}",
                sections=[
                    {"name": "always_empty", "items_count": 0, "item_ids": []},
                    {"name": "filled", "items_count": 2, "item_ids": ["a", "b"]},
                ],
            )
        report = analyze_pack_sections(event_log, days=30, empty_rate_threshold=0.5)
        assert "always_empty" in report.empty_section_flags
        assert "filled" not in report.empty_section_flags

    def test_ignores_flat_packs(self, event_log: SQLiteEventLog) -> None:
        """Flat pack payloads (no `sections` key) don't contribute."""
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="pack_builder",
            entity_id="flat1",
            entity_type="pack",
            payload={"intent": "flat", "injected_item_ids": ["x", "y"]},
        )
        report = analyze_pack_sections(event_log, days=30)
        assert report.total_sectioned_packs == 0
        assert report.section_stats == []

    def test_sorted_by_packs_count_desc(self, event_log: SQLiteEventLog) -> None:
        _emit_sectioned(
            event_log,
            pack_id="p1",
            sections=[
                {"name": "rare", "items_count": 1, "item_ids": ["a"]},
            ],
        )
        for i in range(3):
            _emit_sectioned(
                event_log,
                pack_id=f"common_{i}",
                sections=[
                    {"name": "common", "items_count": 1, "item_ids": ["b"]},
                ],
            )
        report = analyze_pack_sections(event_log, days=30)
        assert [s.name for s in report.section_stats] == ["common", "rare"]
