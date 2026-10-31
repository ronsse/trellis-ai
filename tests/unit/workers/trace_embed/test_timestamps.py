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

import pytest
from pydantic import ValidationError

from trellis.schemas.trace import Trace
from trellis_workers.trace_embed import TraceEmbedWatermark, run_trace_embed_pass
from trellis_workers.trace_embed.watermark import TraceCursor
from trellis_workers.trace_embed.worker import collect_candidates

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


def _append_as_an_earlier_build(registry, traces) -> None:
    """Insert rows keeping each trace's own offset in the stored text."""
    conn = registry.operational.trace_store._conn
    for t in traces:
        conn.execute(
            "INSERT INTO traces (trace_id, source, intent, domain, agent_id,"
            " outcome_status, trace_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                t.trace_id,
                t.source.value,
                t.intent,
                t.context.domain,
                None,
                None,
                t.model_dump_json(),
                t.created_at.isoformat(),
            ),
        )
    conn.commit()


def _at(n: int, moment: datetime):
    return make_trace(n).model_copy(update={"created_at": moment, "updated_at": moment})


def test_a_page_never_steps_over_rows_between_two_offsets(registry) -> None:
    """The walk bounds each page by the oldest instant it read, so the store
    must key every stored offset the way Python reads it. ``+15:00`` is past
    what ``strftime`` accepts as an offset in the text, which an earlier
    build could still write: one key off by its offset made the next page start
    past two rows, and nothing said so."""
    plus_15 = timezone(timedelta(hours=15))
    day = datetime(2026, 8, 1, tzinfo=UTC)
    traces = [
        _at(0, datetime(2026, 8, 1, 20, 0, tzinfo=plus_15)),  # 05:00Z
        _at(1, day.replace(hour=10)),
        _at(2, day.replace(hour=9)),
        _at(3, day.replace(hour=7)),
        _at(4, day.replace(hour=4)),
    ]
    _append_as_an_earlier_build(registry, traces)

    found, truncated = collect_candidates(
        registry.operational.trace_store,
        after=None,
        page_size=2,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert not truncated
    assert [t.trace_id for t in found] == [
        "trace-0004",  # 04:00Z
        "trace-0000",  # 05:00Z
        "trace-0003",  # 07:00Z
        "trace-0002",  # 09:00Z
        "trace-0001",  # 10:00Z
    ]


def test_known_limit_an_offset_with_seconds_can_skip_a_row_in_the_walk(
    registry,
) -> None:
    """A known limit, pinned so a fix or a regression shows. The trace's JSON,
    which the walk reads, keeps an offset only to the minute, while the store's
    key keeps the second, so the walk's bound can sit up to a minute off the key
    and step over a row within that minute. Whole-minute offsets lose nothing
    (``test_a_page_never_steps_over_rows_between_two_offsets``). Only an
    in-process ``datetime`` writes such an offset: the schema rejects the string.
    """
    sub_minute = timezone(-timedelta(hours=5, minutes=30, seconds=45))
    day = datetime(2026, 1, 1, tzinfo=UTC)
    stamp = datetime(2026, 1, 1, 1, 0, tzinfo=sub_minute)  # 06:30:45Z
    traces = [
        _at(0, day.replace(hour=7)),
        _at(1, stamp),
        _at(2, day.replace(hour=6, minute=30, second=20)),
        _at(3, day.replace(hour=5)),
    ]
    payload = json.loads(traces[1].model_dump_json())
    with pytest.raises(ValidationError):
        Trace.model_validate({**payload, "created_at": "2026-01-01T01:00:00-05:30:45"})
    read_back = Trace.model_validate_json(traces[1].model_dump_json())
    assert read_back.created_at.utcoffset() == timedelta(hours=-5, minutes=-30)
    _append(registry, traces)
    store = registry.operational.trace_store
    now = datetime(2030, 1, 1, tzinfo=UTC)

    small, _ = collect_candidates(store, after=None, page_size=2, now=now)
    one_page, _ = collect_candidates(store, after=None, page_size=500, now=now)

    # trace-0002 (06:30:20Z) is within a minute of the offset-with-seconds row
    # and is the one that is skipped; one page reads all four.
    assert [t.trace_id for t in small] == ["trace-0003", "trace-0001", "trace-0000"]
    assert sorted(t.trace_id for t in one_page) == [f"trace-000{n}" for n in range(4)]


def test_a_small_page_over_naive_and_aware_stamps_does_not_raise(registry) -> None:
    """With ``page_size=2`` the lower bound of a full page is a ``min`` over
    stamps of both kinds; a naive one does not compare with an aware one."""
    stamps = [
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 2),  # noqa: DTZ001
        datetime(2026, 1, 1, 3, tzinfo=UTC),
        datetime(2026, 1, 1, 4),  # noqa: DTZ001
    ]
    for n, moment in enumerate(stamps):
        registry.operational.trace_store.append(_at(n, moment))

    found, _ = collect_candidates(
        registry.operational.trace_store,
        after=None,
        page_size=2,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert [t.trace_id for t in found] == [f"trace-{n:04d}" for n in range(4)]


def test_a_stored_stamp_past_the_representable_edge_does_not_stop_the_pass(
    registry,
) -> None:
    """``datetime.min`` read in ``+05:30`` is before the earliest UTC instant
    ``datetime`` can name, so converting it raised ``OverflowError`` out of the
    walk, on every pass, for a row nothing can remove."""
    edge = datetime.min.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    traces = [
        _at(0, edge),
        _at(1, datetime(2026, 8, 1, 1, tzinfo=UTC)),
        _at(2, datetime(2026, 8, 1, 2, tzinfo=UTC)),
    ]
    _append_as_an_earlier_build(registry, traces)

    found, _ = collect_candidates(
        registry.operational.trace_store,
        after=None,
        page_size=2,
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert [t.trace_id for t in found] == ["trace-0000", "trace-0001", "trace-0002"]


def test_a_saved_cursor_past_the_representable_edge_degrades_instead_of_raising(
    watermark_path,
) -> None:
    """A corrupt cursor degrades to a re-scan; an extreme one must not be the
    exception that escapes ``_load``."""
    watermark_path.write_text(
        json.dumps(
            {
                "version": 1,
                "cursor": {
                    "created_at": "0001-01-01T00:00:00+05:30",
                    "trace_id": "a",
                },
            }
        )
    )

    cursor = TraceEmbedWatermark(watermark_path).cursor

    assert cursor == TraceCursor(
        created_at=datetime.min.replace(tzinfo=UTC), trace_id="a"
    )
