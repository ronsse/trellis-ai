"""A capped analyzer report must say it was capped (#374).

``scan_events`` is unit-tested in ``tests/unit/stores/test_event_scan.py``.
This file pins the property at the level an operator actually reads: each
analyzer's *output*. Two things must hold for every one of them, and both
are asserted in the same test so neither can be quietly dropped:

1. The numbers are computed from the **newest** events in the window.
2. The report **says** its window was cut short.

Every assertion has a paired negative case — an untruncated run of the
same analyzer — because a disclosure that is always on is exactly as
useless as one that is always off, and this repo's recurring defect is a
measurement wired to a constant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.extract.telemetry import (
    analyze_extraction_validation,
    analyze_extractor_fallbacks,
)
from trellis.ops.capture_coverage import summarize_capture_coverage
from trellis.ops.write_health import (
    summarize_backend_health,
    summarize_serve_attribution,
    summarize_write_health,
)
from trellis.retrieve import evaluate as evaluate_module
from trellis.retrieve.evaluate import analyze_dimension_predictiveness
from trellis.retrieve.metrics_timeseries import (
    METRIC_NOISE_TAG_VOLUME,
    compute_timeseries,
)
from trellis.retrieve.pack_sections import analyze_pack_sections
from trellis.retrieve.pack_value import summarize_pack_value
from trellis.retrieve.telemetry import analyze_pack_telemetry
from trellis.retrieve.token_usage import analyze_token_usage
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def log(tmp_path: Path) -> SQLiteEventLog:
    return SQLiteEventLog(tmp_path / "events.db")


def _at(minutes_ago: int) -> datetime:
    return datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)


def _append(
    log: SQLiteEventLog,
    event_type: EventType,
    *,
    minutes_ago: int,
    payload: dict[str, object] | None = None,
    source: str = "test",
    entity_id: str | None = None,
) -> None:
    log.append(
        Event(
            event_type=event_type,
            source=source,
            entity_id=entity_id,
            occurred_at=_at(minutes_ago),
            payload=payload or {},
        )
    )


# ---------------------------------------------------------------------------
# write_health — the surface that is supposed to notice today's outage
# ---------------------------------------------------------------------------


def _seed_writes(log: SQLiteEventLog, *, old: int, new_tool: str) -> None:
    """``old`` stale accepted writes, then one rejection from a new tool."""
    for index in range(old):
        _append(
            log,
            EventType.MUTATION_EXECUTED,
            minutes_ago=1000 - index,
            payload={"requested_by": "mcp:stale_tool"},
        )
    _append(
        log,
        EventType.WRITE_REJECTED,
        minutes_ago=1,
        payload={"tool": new_tool, "rejections": [{"kind": "enum", "loc": "source"}]},
    )


def test_write_health_capped_report_says_so_and_keeps_the_newest(
    log: SQLiteEventLog,
) -> None:
    """The motivating incident: a new rejection behind a wall of old accepts.

    ``MUTATION_EXECUTED`` is the highest-volume event type any analyzer
    reads (1,473 in 30 days on the reference deployment), so it is the one
    that reaches the cap first and buries everything newer.
    """
    _seed_writes(log, old=6, new_tool="save_experience")
    report = summarize_write_health(log, days=30, limit=3)

    assert report.scan.truncated is True
    assert report.status == "warn"
    assert any("TRUNCATED" in reason for reason in report.reasons)
    # Summed across all three reads: 6 accepts (3 kept, 3 dropped) plus
    # the single boundary rejection, which was never at risk of the cap.
    assert report.scan.scanned == 4
    assert report.scan.matched == 7
    assert report.scan.dropped == 3
    # The rejection is one minute old; under an ascending cap it was the
    # first row dropped and this surface did not exist in the report.
    assert "mcp:save_experience" in report.by_tool


def test_write_health_uncapped_report_makes_no_truncation_claim(
    log: SQLiteEventLog,
) -> None:
    _seed_writes(log, old=6, new_tool="save_experience")
    report = summarize_write_health(log, days=30, limit=100)

    assert report.scan.truncated is False
    assert report.scan.note == ""
    assert not any("TRUNCATED" in reason for reason in report.reasons)
    assert "mcp:save_experience" in report.by_tool


def test_serve_attribution_reports_its_own_coverage(log: SQLiteEventLog) -> None:
    for index in range(6):
        _append(
            log,
            EventType.PACK_ASSEMBLED,
            minutes_ago=100 - index,
            entity_id=f"pack_{index}",
            payload={"injected_items": [{"item_id": "a"}]},
        )
    capped = summarize_serve_attribution(log, days=30, limit=3)
    clean = summarize_serve_attribution(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert capped.packs == 3
    assert clean.scan.truncated is False
    assert clean.packs == 6


def test_backend_health_never_reports_ok_over_a_shortened_window(
    log: SQLiteEventLog,
) -> None:
    """A clean ``ok`` on a truncated scan is the failure #374 describes."""
    for index in range(6):
        _append(
            log,
            EventType.MUTATION_EXECUTED,
            minutes_ago=100 - index,
            payload={"requested_by": "mcp:save_memory"},
        )
    capped = summarize_backend_health(log, days=30, limit=3)
    clean = summarize_backend_health(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert capped.status == "warn"
    assert clean.scan.truncated is False
    assert clean.status == "ok"


# ---------------------------------------------------------------------------
# capture_coverage — last_sweep_at is the field truncation lied to
# ---------------------------------------------------------------------------


def test_capture_coverage_last_sweep_is_the_newest_sweep(
    log: SQLiteEventLog,
) -> None:
    """Under an ascending cap ``last_sweep_at`` became the newest sweep of
    the *oldest* slice — a plausible timestamp for a pipeline that has
    since stopped, which is precisely the state this module distinguishes.
    """
    for index in range(6):
        _append(
            log,
            EventType.CAPTURE_SWEEP_COMPLETED,
            minutes_ago=600 - index * 100,
            source="worker:session-capture",
            payload={"sessions_triggered": 1, "sessions_with_memory": 1},
        )
    capped = summarize_capture_coverage(log, days=30, limit=3)
    clean = summarize_capture_coverage(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert any("TRUNCATED" in note for note in capped.notes)
    assert clean.scan.truncated is False
    assert not any("TRUNCATED" in note for note in clean.notes)
    # Both agree on when capture last ran, because the cap drops the old end.
    assert capped.last_sweep_at == clean.last_sweep_at


# ---------------------------------------------------------------------------
# metrics_timeseries — a truncated series goes flat, not empty
# ---------------------------------------------------------------------------


def test_timeseries_reports_its_scan_coverage(log: SQLiteEventLog) -> None:
    for index in range(6):
        _append(
            log,
            EventType.TAGS_REFRESHED,
            minutes_ago=100 - index,
            payload={"after": {"signal_quality": "noise"}},
        )
    capped = compute_timeseries(log, metric=METRIC_NOISE_TAG_VOLUME, days=30, limit=3)
    clean = compute_timeseries(log, metric=METRIC_NOISE_TAG_VOLUME, days=30, limit=100)

    assert capped.scan.truncated is True
    assert capped.scan.covered_since != ""
    assert clean.scan.truncated is False
    assert clean.scan.covered_since == ""


# ---------------------------------------------------------------------------
# pack_value — the truncation note leads the caveats
# ---------------------------------------------------------------------------


def test_pack_value_puts_truncation_first_among_its_notes(
    log: SQLiteEventLog,
) -> None:
    """If the window is not the window, nothing below it means what it says."""
    for index in range(6):
        pack_id = f"pack_{index}"
        _append(
            log,
            EventType.PACK_ASSEMBLED,
            minutes_ago=100 - index,
            entity_id=pack_id,
            payload={
                "injected_items": [
                    {
                        "item_id": f"{pack_id}_a",
                        "estimated_tokens": 100,
                        "strategy_source": "semantic",
                        "item_type": "vector",
                    }
                ]
            },
        )
        _append(
            log,
            EventType.FEEDBACK_RECORDED,
            minutes_ago=99 - index,
            payload={"pack_id": pack_id, "helpful_item_ids": [f"{pack_id}_a"]},
        )
    capped = summarize_pack_value(log, days=30, limit=3)
    clean = summarize_pack_value(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert "TRUNCATED" in capped.notes[1]
    assert clean.scan.truncated is False
    assert not any("TRUNCATED" in note for note in clean.notes)


def test_backend_health_states_truncation_once(log: SQLiteEventLog) -> None:
    """Two capped reads must not produce two overlapping truncation lines.

    The write section carries its own note so it reads correctly on its
    own; the composed report supersedes it with the merged one rather than
    printing both.
    """
    for index in range(6):
        _append(
            log,
            EventType.MUTATION_EXECUTED,
            minutes_ago=100 - index,
            payload={"requested_by": "mcp:save_memory"},
        )
        _append(
            log,
            EventType.PACK_ASSEMBLED,
            minutes_ago=100 - index,
            entity_id=f"pack_{index}",
            payload={"injected_items": [{"item_id": "a"}]},
        )
    report = summarize_backend_health(log, days=30, limit=3)

    truncation_lines = [r for r in report.reasons if r.startswith("TRUNCATED:")]
    assert len(truncation_lines) == 1
    # And it leads, so a reader who stops at the first line learns the
    # window is short before reading any count.
    assert report.reasons[0] is truncation_lines[0]
    assert "mutation.executed" in truncation_lines[0]
    assert "pack.assembled" in truncation_lines[0]


# ---------------------------------------------------------------------------
# The pack / extract analyzers — the tail of #374
# ---------------------------------------------------------------------------
#
# These five took the ascending default until now. Unlike the ops analyzers
# above, none of them is health-facing, and on the reference deployment none
# is close to its cap: the worst single-type share measured over 30 days is
# 43 of 5,000 (0.9%). What made them worth fixing is the direction, not the
# distance — every one is a wrong-end read waiting for volume, and the two
# below with order-sensitive output would have inverted silently.


def _seed_packs(log: SQLiteEventLog, count: int, *, sectioned: bool = False) -> None:
    """``count`` PACK_ASSEMBLED events, oldest first, each identifiable."""
    for index in range(count):
        payload: dict[str, object] = {
            "injected_items": [{"item_id": f"item_{index}", "strategy": "keyword"}],
            "rejected_items": [],
            "budget": {"max_items_hit": False, "max_tokens_hit": False},
        }
        if sectioned:
            payload["sections"] = [
                {
                    "name": f"section_{index}",
                    "items_count": 1,
                    "item_ids": [f"i{index}"],
                }
            ]
        _append(
            log,
            EventType.PACK_ASSEMBLED,
            minutes_ago=100 - index,
            entity_id=f"pack_{index}",
            payload=payload,
        )


def test_pack_telemetry_capped_report_says_so_and_keeps_the_newest(
    log: SQLiteEventLog,
) -> None:
    _seed_packs(log, 6)
    capped = analyze_pack_telemetry(log, days=30, limit=3)
    clean = analyze_pack_telemetry(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert capped.scan.scanned == 3
    assert capped.scan.matched == 6
    assert capped.scan.dropped == 3
    assert any("TRUNCATED" in note for note in capped.notes)
    assert capped.total_packs == 3

    assert clean.scan.truncated is False
    assert clean.scan.note == ""
    assert not any("TRUNCATED" in note for note in clean.notes)
    assert clean.total_packs == 6


def test_pack_sections_capped_report_keeps_the_newest_sections(
    log: SQLiteEventLog,
) -> None:
    """Section names are per-pack here, so *which* packs survived is legible
    in the output rather than only in the counts."""
    _seed_packs(log, 6, sectioned=True)
    capped = analyze_pack_sections(log, days=30, limit=3)
    clean = analyze_pack_sections(log, days=30, limit=100)

    assert capped.scan.truncated is True
    names = {stats.name for stats in capped.section_stats}
    assert names == {"section_3", "section_4", "section_5"}

    assert clean.scan.truncated is False
    assert len({stats.name for stats in clean.section_stats}) == 6


def test_token_usage_over_budget_list_holds_the_newest_overruns(
    log: SQLiteEventLog,
) -> None:
    """``over_budget`` is the one output whose *contents* the cap chose.

    It is appended in iteration order and returned whole, so an ascending
    truncation handed an operator the oldest budget overruns and hid
    today's — the inversion, not merely a smaller sample.
    """
    for index in range(6):
        _append(
            log,
            EventType.TOKEN_TRACKED,
            minutes_ago=100 - index,
            payload={
                "layer": "mcp",
                "operation": f"op_{index}",
                "response_tokens": 500,
                "budget_tokens": 100,
            },
        )
    capped = analyze_token_usage(log, days=30, limit=3)
    clean = analyze_token_usage(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert [row["operation"] for row in capped.over_budget] == ["op_3", "op_4", "op_5"]
    # Still chronological within the slice that was read.
    assert [row["occurred_at"] for row in capped.over_budget] == sorted(
        row["occurred_at"] for row in capped.over_budget
    )

    assert clean.scan.truncated is False
    assert len(clean.over_budget) == 6


def test_extractor_fallbacks_merges_both_reads_coverage(
    log: SQLiteEventLog,
) -> None:
    """The rate is a ratio between two independently capped reads, so the
    coverage carried is the merge — a report where *either* read truncated
    is comparing differently-sized slices."""
    for index in range(6):
        _append(
            log,
            EventType.EXTRACTION_DISPATCHED,
            minutes_ago=100 - index,
            payload={"source_hint": f"src_{index}"},
        )
    _append(
        log,
        EventType.EXTRACTOR_FALLBACK,
        minutes_ago=1,
        payload={"reason": "empty_result", "source_hint": "src_5"},
    )
    capped = analyze_extractor_fallbacks(log, days=30, limit=3)
    clean = analyze_extractor_fallbacks(log, days=30, limit=100)

    # The fallback read (1 event) never hit its cap; the dispatch read did.
    assert capped.scan.truncated is True
    assert capped.total_dispatches == 3
    assert capped.total_fallbacks == 1
    assert any("TRUNCATED" in note for note in capped.notes)
    # The newest three dispatches survived. Under the ascending read the
    # surviving set was {src_0, src_1, src_2} — and src_5, the source the
    # fresh fallback names, was not among them.
    assert {stats.source_hint for stats in capped.per_source} == {
        "src_3",
        "src_4",
        "src_5",
    }

    assert clean.scan.truncated is False
    assert clean.total_dispatches == 6


def test_extraction_validation_merges_both_reads_coverage(
    log: SQLiteEventLog,
) -> None:
    for index in range(6):
        _append(
            log,
            EventType.EXTRACTION_DISPATCHED,
            minutes_ago=100 - index,
            payload={"source_hint": f"src_{index}"},
        )
    _append(
        log,
        EventType.EXTRACTION_REJECTED,
        minutes_ago=1,
        payload={
            "source_hint": "src_5",
            "extractor_used": "json_rules",
            "findings": [{"code": "missing_field"}],
        },
    )
    capped = analyze_extraction_validation(log, days=30, limit=3)
    clean = analyze_extraction_validation(log, days=30, limit=100)

    assert capped.scan.truncated is True
    assert capped.total_dispatches == 3
    assert capped.total_rejected == 1
    assert any("TRUNCATED" in note for note in capped.notes)

    assert clean.scan.truncated is False
    assert clean.total_dispatches == 6


# ---------------------------------------------------------------------------
# The one ordering assumption in the set — pinned, not assumed
# ---------------------------------------------------------------------------


def _score_pack(log: SQLiteEventLog, pack_id: str, *, minutes_ago: int, score: float):
    _append(
        log,
        EventType.PACK_QUALITY_SCORED,
        minutes_ago=minutes_ago,
        entity_id=pack_id,
        payload={
            "pack_id": pack_id,
            "dimensions": {"breadth": score},
            "weighted_score": score,
        },
    )


def test_predictiveness_last_write_still_means_last_by_arrival(
    log: SQLiteEventLog,
) -> None:
    """``scan_events`` reverses for exactly this reason.

    A **guard, not a witness**: this passed before the conversion too,
    because the ascending read gave last-by-arrival as well. What it pins
    is the next change — ``pack_scores`` and ``pack_success`` are both
    last-write-wins over a dict keyed by ``pack_id``, and "last" means
    last *by arrival*, so a future substitution that propagates descending
    order instead of reversing it would silently hand back the *first*
    verdict. This is the ``_response_token_join`` shape #389 found, and
    the mirror of the first-wins-means-freshest sites (``learning/
    cooldown.py``, ``trellis_api/routes/admin.py``) that must be left on
    ``order="desc"`` and never routed through this helper.
    """
    # Same pack scored twice: the newer score is 0.9, the older 0.1.
    _score_pack(log, "pack_a", minutes_ago=50, score=0.1)
    _score_pack(log, "pack_a", minutes_ago=10, score=0.9)
    # Same pack graded twice: the newer verdict is failure.
    _append(
        log,
        EventType.FEEDBACK_RECORDED,
        minutes_ago=40,
        payload={"pack_id": "pack_a", "success": True},
    )
    _append(
        log,
        EventType.FEEDBACK_RECORDED,
        minutes_ago=5,
        payload={"pack_id": "pack_a", "success": False},
    )

    report = analyze_dimension_predictiveness(log, days=30)

    assert report.scan.truncated is False
    assert report.total_matched_feedback == 1
    # The newest verdict won: one matched pack, and it failed.
    assert report.overall_success_rate == 0.0


def test_predictiveness_capped_read_keeps_the_newest_pairs(
    log: SQLiteEventLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncation drops matched *pairs* faster than it drops events: the
    join only sees a pack whose quality event and whose feedback event both
    survived their own cap."""
    for index in range(6):
        _score_pack(log, f"pack_{index}", minutes_ago=100 - index, score=0.5)
        _append(
            log,
            EventType.FEEDBACK_RECORDED,
            minutes_ago=100 - index,
            payload={"pack_id": f"pack_{index}", "success": True},
        )
    monkeypatch.setattr(evaluate_module, "_PREDICTIVENESS_EVENT_LIMIT", 3)
    capped = analyze_dimension_predictiveness(log, days=30)
    monkeypatch.setattr(evaluate_module, "_PREDICTIVENESS_EVENT_LIMIT", 100)
    clean = analyze_dimension_predictiveness(log, days=30)

    assert capped.scan.truncated is True
    assert capped.total_packs_scored == 3
    assert any("TRUNCATED" in note for note in capped.notes)

    assert clean.scan.truncated is False
    assert clean.total_packs_scored == 6
