"""The access-log report: a scan cap must not hide a quiet key."""

from __future__ import annotations

from trellis.core import access_log
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


def _emit(log, key_id: str, n: int = 1) -> None:
    for _ in range(n):
        log.emit(
            EventType.KEY_READ,
            access_log.ACCESS_LOG_SOURCE,
            entity_id=key_id,
            entity_type="api_key",
            payload=access_log.build_payload(
                key_id=key_id,
                surface="rest",
                route="GET /api/v1/search",
                item_ids=["d"],
                item_count=1,
            ),
        )


def test_a_key_filter_finds_a_quiet_key_behind_more_rows_than_the_scan_cap(
    tmp_path,
) -> None:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    try:
        log = reg.operational.event_log
        _emit(log, "quiet")
        _emit(log, "noisy", 5100)
        everyone = access_log.summarize_key_reads(log, window_hours=1)
        assert everyone.truncated is True
        assert everyone.covered_since
        assert "may still have been used" in everyone.note
        assert "quiet" not in {k.key_id for k in everyone.keys}, "control: hidden"
        one = access_log.summarize_key_reads(log, window_hours=1, key_id="quiet")
        assert [k.key_id for k in one.keys] == ["quiet"]
        assert one.keys[0].reads == 1
        assert one.truncated is False
        assert one.note == ""
    finally:
        reg.close()


def test_a_key_with_more_rows_than_the_cap_is_marked_truncated(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(access_log, "_REPORT_SCAN_LIMIT", 20)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    try:
        log = reg.operational.event_log
        _emit(log, "busy", 30)
        report = access_log.summarize_key_reads(log, window_hours=1, key_id="busy")
        assert report.truncated is True
        assert report.covered_since
        assert "newest 20 of its reads" in report.note
        assert report.keys[0].reads == 20
    finally:
        reg.close()


def test_an_untruncated_report_says_so(tmp_path) -> None:
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    try:
        log = reg.operational.event_log
        _emit(log, "a", 3)
        report = access_log.summarize_key_reads(log, window_hours=1)
        assert report.truncated is False
        assert report.note == ""
        assert report.keys[0].reads == 3
    finally:
        reg.close()
