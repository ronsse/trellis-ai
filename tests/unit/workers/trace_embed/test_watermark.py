"""The cursor. Every test here is about it failing safe rather than working."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from trellis_workers.trace_embed.watermark import (
    WATERMARK_VERSION,
    WATERMARK_VERSION_WITH_RETRY,
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


class TestRetryList:
    """The list of held traces the cursor has moved past."""

    def test_a_saved_list_reloads_oldest_first(self, tmp_path) -> None:
        path = tmp_path / "wm.json"
        wm = TraceEmbedWatermark(path)
        wm.advance_to(_cursor(9))
        assert wm.set_retry([_cursor(5, "b"), _cursor(2, "a")]) is True
        wm.save()
        assert TraceEmbedWatermark(path).retry == (_cursor(2, "a"), _cursor(5, "b"))

    def test_restating_the_same_list_changes_nothing(self, tmp_path) -> None:
        path = tmp_path / "wm.json"
        wm = TraceEmbedWatermark(path)
        wm.set_retry([_cursor(2, "a")])
        wm.save()
        again = TraceEmbedWatermark(path)
        assert again.set_retry([_cursor(2, "a")]) is False
        again.save()

    def test_a_file_from_before_the_list_loads_with_an_empty_one(
        self, tmp_path
    ) -> None:
        path = tmp_path / "wm.json"
        path.write_text(
            json.dumps({"version": 1, "cursor": _cursor(5).to_json()}),
            encoding="utf-8",
        )
        wm = TraceEmbedWatermark(path)
        assert wm.cursor == _cursor(5)
        assert wm.retry == ()

    @pytest.mark.parametrize(
        "bad", ["not-a-list", [{"created_at": "nope", "trace_id": "x"}], [1]]
    )
    def test_an_unreadable_list_degrades_the_whole_file_to_a_rescan(
        self, tmp_path, bad
    ) -> None:
        """Keeping the cursor without the traces it was allowed to pass would
        skip them for good, so the cursor goes with the list."""
        path = tmp_path / "wm.json"
        path.write_text(
            json.dumps(
                {
                    "version": WATERMARK_VERSION_WITH_RETRY,
                    "cursor": _cursor(5).to_json(),
                    "retry": bad,
                }
            ),
            encoding="utf-8",
        )
        wm = TraceEmbedWatermark(path)
        assert wm.cursor is None
        assert wm.retry == ()

    def test_only_a_file_that_holds_something_back_is_unreadable_to_an_old_build(
        self, tmp_path
    ) -> None:
        """A build from before the list would read the cursor and ignore the
        list. It must read a non-empty list's file as an unknown version, and an
        empty list's file as it always has."""
        path = tmp_path / "wm.json"
        wm = TraceEmbedWatermark(path)
        wm.advance_to(_cursor(9))
        wm.set_retry([_cursor(2, "a")])
        wm.save()
        held = json.loads(path.read_text(encoding="utf-8"))
        assert held["version"] == WATERMARK_VERSION_WITH_RETRY
        assert held["version"] != WATERMARK_VERSION

        wm.set_retry([])
        wm.save()
        settled = json.loads(path.read_text(encoding="utf-8"))
        assert settled["version"] == WATERMARK_VERSION
        assert "retry" not in settled

    def test_reset_forgets_the_list_and_persists(self, tmp_path) -> None:
        path = tmp_path / "wm.json"
        wm = TraceEmbedWatermark(path)
        wm.advance_to(_cursor(9))
        wm.set_retry([_cursor(2, "a")])
        wm.save()
        again = TraceEmbedWatermark(path)
        again.reset()
        again.save()
        reloaded = TraceEmbedWatermark(path)
        assert reloaded.cursor is None
        assert reloaded.retry == ()

    def test_a_trace_id_appears_once(self, tmp_path) -> None:
        wm = TraceEmbedWatermark(tmp_path / "wm.json")
        wm.set_retry([_cursor(2, "a"), _cursor(2, "a")])
        assert wm.retry == (_cursor(2, "a"),)
