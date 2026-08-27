"""The cursor. Every test here is about it failing safe rather than working."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trellis_workers.trace_embed.watermark import (
    WATERMARK_VERSION,
    TraceCursor,
    TraceEmbedWatermark,
)

BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _cursor(minutes: int, trace_id: str = "t") -> TraceCursor:
    return TraceCursor(created_at=BASE + timedelta(minutes=minutes), trace_id=trace_id)


class TestRoundTrip:
    def test_absent_file_reads_as_no_cursor(self, tmp_path) -> None:
        assert TraceEmbedWatermark(tmp_path / "nope.json").cursor is None

    def test_saved_cursor_reloads(self, tmp_path) -> None:
        path = tmp_path / "wm.json"
        wm = TraceEmbedWatermark(path)
        assert wm.advance_to(_cursor(5)) is True
        wm.save()
        assert TraceEmbedWatermark(path).cursor == _cursor(5)

    def test_save_is_a_no_op_when_nothing_changed(self, tmp_path) -> None:
        path = tmp_path / "wm.json"
        TraceEmbedWatermark(path).save()
        assert not path.exists()


class TestFailsSafe:
    def test_a_corrupt_file_degrades_to_a_full_rescan(self, tmp_path) -> None:
        """Not an exception, and not a guessed position: no cursor at all. The
        store-state check makes the re-scan free, so forgetting is always the
        safe direction to fail in."""
        path = tmp_path / "wm.json"
        path.write_text("{not json", encoding="utf-8")
        assert TraceEmbedWatermark(path).cursor is None

    def test_an_unknown_version_degrades_to_a_full_rescan(self, tmp_path) -> None:
        path = tmp_path / "wm.json"
        path.write_text(
            f'{{"version": {WATERMARK_VERSION + 99}, "cursor": '
            '{"created_at": "2026-08-01T00:05:00+00:00", "trace_id": "t"}}',
            encoding="utf-8",
        )
        assert TraceEmbedWatermark(path).cursor is None

    def test_an_unparseable_timestamp_degrades(self, tmp_path) -> None:
        path = tmp_path / "wm.json"
        path.write_text(
            f'{{"version": {WATERMARK_VERSION}, "cursor": '
            '{"created_at": "last tuesday", "trace_id": "t"}}',
            encoding="utf-8",
        )
        assert TraceEmbedWatermark(path).cursor is None


class TestMonotonic:
    def test_cannot_move_backwards(self, tmp_path) -> None:
        wm = TraceEmbedWatermark(tmp_path / "wm.json")
        wm.advance_to(_cursor(10))
        assert wm.advance_to(_cursor(5)) is False
        assert wm.cursor == _cursor(10)

    def test_cannot_restate_the_same_position(self, tmp_path) -> None:
        wm = TraceEmbedWatermark(tmp_path / "wm.json")
        wm.advance_to(_cursor(10))
        assert wm.advance_to(_cursor(10)) is False

    def test_trace_id_breaks_a_timestamp_tie(self, tmp_path) -> None:
        """Two traces can share a timestamp and ``TraceStore.query``'s
        ``since`` is inclusive, so the id is what keeps a same-instant sibling
        from being stepped over."""
        wm = TraceEmbedWatermark(tmp_path / "wm.json")
        wm.advance_to(_cursor(10, "aaa"))
        assert wm.advance_to(_cursor(10, "bbb")) is True
        assert wm.advance_to(_cursor(10, "aaa")) is False

    def test_reset_forgets_and_persists(self, tmp_path) -> None:
        path = tmp_path / "wm.json"
        wm = TraceEmbedWatermark(path)
        wm.advance_to(_cursor(10))
        wm.save()
        wm2 = TraceEmbedWatermark(path)
        wm2.reset()
        wm2.save()
        assert TraceEmbedWatermark(path).cursor is None
