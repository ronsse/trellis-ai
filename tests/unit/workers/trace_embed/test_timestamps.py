"""A pass orders and bounds traces by instant, whatever offset they carry.

A trace's ``created_at`` is whatever its writer gave it: aware in any zone, or
naive. The pass sorts, takes a minimum over and compares those stamps in
Python, and the store filters them in SQL. Both must read an instant, or one
naive trace among aware ones raises ``TypeError`` out of the sort and the pass
never gets past it again.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

from trellis_workers.trace_embed import TraceEmbedWatermark, run_trace_embed_pass
from trellis_workers.trace_embed.watermark import TraceCursor

from .conftest import make_trace

_PLUS_14 = timezone(timedelta(hours=14))
_MINUS_11 = timezone(timedelta(hours=-11))

#: Midnight UTC on 2026-08-01, as that instant reads in two far-apart zones.
_MIDNIGHT_IN_PLUS_14 = datetime(2026, 8, 1, 14, 0, tzinfo=_PLUS_14)
_MIDNIGHT_IN_MINUS_11 = datetime(2026, 7, 31, 13, 0, tzinfo=_MINUS_11)


def _append(registry, traces) -> None:
    for trace in traces:
        registry.operational.trace_store.append(trace)


def _embedded_order(recorder, traces) -> list[str]:
    """Trace ids in the order the embedder saw them."""
    return [
        next(t.trace_id for t in traces if t.intent in text) for text in recorder.texts
    ]


def test_a_naive_trace_among_aware_ones_does_not_stop_the_pass(
    registry, watermark_path, recorder
) -> None:
    aware = [make_trace(n) for n in range(4)]
    naive = make_trace(4, base=datetime(2026, 8, 1))  # noqa: DTZ001
    after = make_trace(5)
    traces = [*aware, naive, after]
    _append(registry, traces)

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.to_dict()["status"] == "ok"
    assert report.embedded == 6
    assert _embedded_order(recorder, traces) == [t.trace_id for t in traces]
    # The cursor reached the newest instant, and reads as an aware stamp.
    cursor = TraceEmbedWatermark(watermark_path).cursor
    assert cursor == TraceCursor(created_at=after.created_at, trace_id="trace-0005")
    assert cursor is not None
    assert cursor.created_at.utcoffset() == timedelta(0)


def test_traces_in_distant_zones_are_embedded_in_instant_order(
    registry, watermark_path, recorder
) -> None:
    # Even n are written in +14:00, odd n in -11:00, yet n is the instant order.
    # Read as text the two zones sort 25 hours apart.
    traces = [
        make_trace(
            n, base=_MIDNIGHT_IN_PLUS_14 if n % 2 == 0 else _MIDNIGHT_IN_MINUS_11
        )
        for n in range(8)
    ]
    _append(registry, traces)

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.embedded == 8
    assert _embedded_order(recorder, traces) == [t.trace_id for t in traces]
    assert TraceEmbedWatermark(watermark_path).cursor == TraceCursor(
        created_at=traces[-1].created_at, trace_id=traces[-1].trace_id
    )

    again = run_trace_embed_pass(registry, watermark_path=watermark_path)
    assert again.embedded == 0
    assert len(recorder.texts) == 8


def test_a_naive_cursor_saved_by_an_earlier_build_still_loads(
    registry, watermark_path, recorder
) -> None:
    """The cursor file kept the writer's stamp, so an old one may be naive."""
    traces = [make_trace(n) for n in range(6)]
    _append(registry, traces)
    watermark_path.write_text(
        json.dumps(
            {
                "version": 1,
                "cursor": {
                    "created_at": traces[2].created_at.replace(tzinfo=None).isoformat(),
                    "trace_id": traces[2].trace_id,
                },
            }
        )
    )

    report = run_trace_embed_pass(registry, watermark_path=watermark_path)

    assert report.to_dict()["status"] == "ok"
    assert _embedded_order(recorder, traces) == [t.trace_id for t in traces[3:]]
    assert TraceEmbedWatermark(watermark_path).cursor == TraceCursor(
        created_at=traces[-1].created_at, trace_id=traces[-1].trace_id
    )


class TestTraceCursorReadsAnInstant:
    def test_a_naive_stamp_is_read_as_utc(self) -> None:
        naive = datetime(2026, 8, 1, 3, 4, 5)  # noqa: DTZ001
        cursor = TraceCursor(created_at=naive, trace_id="t")
        assert cursor == TraceCursor(created_at=naive.replace(tzinfo=UTC), trace_id="t")

    def test_an_offset_stamp_equals_the_same_instant_in_utc(self) -> None:
        at = datetime(2026, 8, 1, 3, 4, 5, tzinfo=UTC)
        assert TraceCursor(created_at=at.astimezone(_MINUS_11), trace_id="t") == (
            TraceCursor(created_at=at, trace_id="t")
        )

    def test_a_loaded_naive_stamp_compares_with_an_aware_one(self) -> None:
        loaded = TraceCursor.from_json(
            {"created_at": "2026-08-01T00:00:00", "trace_id": "a"}
        )
        later = TraceCursor(
            created_at=datetime(2026, 8, 1, 0, 1, tzinfo=UTC), trace_id="a"
        )
        assert loaded is not None
        assert later > loaded
        assert not loaded > later

    def test_advancing_from_a_loaded_naive_cursor(self, watermark_path) -> None:
        watermark_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "cursor": {"created_at": "2026-08-01T00:00:00", "trace_id": "a"},
                }
            )
        )
        mark = TraceEmbedWatermark(watermark_path)
        later = TraceCursor(
            created_at=datetime(2026, 8, 1, 0, 1, tzinfo=_MINUS_11), trace_id="a"
        )
        assert mark.advance_to(later) is True
        assert mark.cursor == later
