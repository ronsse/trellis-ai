"""Tests for improvement-metrics time-series aggregation.

Seeds a SQLite EventLog in ``tmp_path`` with explicit ``occurred_at``
timestamps so bucket boundaries are deterministic, then asserts exact
per-bucket values, group_by correctness, and empty-store behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.retrieve.metrics_timeseries import (
    METRIC_ADVISORY_FITNESS,
    METRIC_NOISE_TAG_VOLUME,
    METRIC_PACK_SUCCESS_RATE,
    METRIC_PARAMETER_PROMOTIONS,
    METRIC_REFERENCE_RATE,
    VALID_METRICS,
    compute_timeseries,
)
from trellis.stores.base.event_log import Event, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _day(offset_days: int, hour: int = 12) -> datetime:
    """A UTC timestamp ``offset_days`` ago at a fixed hour (mid-bucket)."""
    return datetime.now(tz=UTC) - timedelta(days=offset_days, hours=-hour + 12)


def _emit_pack(
    log: SQLiteEventLog,
    *,
    pack_id: str,
    occurred_at: datetime,
    domain: str | None = None,
    injected: list[str] | None = None,
) -> None:
    payload: dict = {"injected_item_ids": injected or []}
    if domain is not None:
        payload["domain"] = domain
    log.append(
        Event(
            event_type=EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            occurred_at=occurred_at,
            payload=payload,
        )
    )


def _emit_feedback(
    log: SQLiteEventLog,
    *,
    pack_id: str,
    occurred_at: datetime,
    success: bool,
    served: list[str] | None = None,
    referenced: list[str] | None = None,
    intent_family: str | None = None,
) -> None:
    payload: dict = {"pack_id": pack_id, "success": success}
    if served is not None:
        payload["items_served"] = served
    if referenced is not None:
        payload["helpful_item_ids"] = referenced
    if intent_family is not None:
        payload["intent_family"] = intent_family
    log.append(
        Event(
            event_type=EventType.FEEDBACK_RECORDED,
            source="test",
            entity_id=pack_id,
            entity_type="feedback",
            occurred_at=occurred_at,
            payload=payload,
        )
    )


# ---------------------------------------------------------------------------
# Empty store
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metric", VALID_METRICS)
def test_empty_store_empty_series(event_log, metric):
    result = compute_timeseries(event_log, metric=metric, days=30)
    assert result.metric == metric
    assert result.bucket == "day"
    assert result.series == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_metric_raises(event_log):
    with pytest.raises(ValueError, match="Unknown metric"):
        compute_timeseries(event_log, metric="not_a_metric", days=30)


def test_unknown_group_by_raises(event_log):
    with pytest.raises(ValueError, match="Unknown group_by"):
        compute_timeseries(
            event_log, metric=METRIC_PACK_SUCCESS_RATE, group_by="nope"
        )


def test_non_positive_days_raises(event_log):
    with pytest.raises(ValueError, match="days must be positive"):
        compute_timeseries(event_log, metric=METRIC_PACK_SUCCESS_RATE, days=0)


# ---------------------------------------------------------------------------
# pack_success_rate
# ---------------------------------------------------------------------------


def test_pack_success_rate_exact_buckets(event_log):
    # Day -1: 2 packs, 1 success -> 0.5. Day -3: 1 pack, 1 success -> 1.0.
    d1, d3 = _day(1), _day(3)
    _emit_pack(event_log, pack_id="p1", occurred_at=d1)
    _emit_pack(event_log, pack_id="p2", occurred_at=d1)
    _emit_pack(event_log, pack_id="p3", occurred_at=d3)
    _emit_feedback(event_log, pack_id="p1", occurred_at=d1, success=True)
    _emit_feedback(event_log, pack_id="p2", occurred_at=d1, success=False)
    _emit_feedback(event_log, pack_id="p3", occurred_at=d3, success=True)

    result = compute_timeseries(
        event_log, metric=METRIC_PACK_SUCCESS_RATE, days=30
    )
    assert len(result.series) == 1
    points = {p.bucket_start: p for p in result.series[0].points}
    assert points[d1.date().isoformat()].value == 0.5
    assert points[d1.date().isoformat()].sample_count == 2
    assert points[d3.date().isoformat()].value == 1.0
    assert points[d3.date().isoformat()].sample_count == 1


def test_pack_success_rate_omits_empty_buckets(event_log):
    d1 = _day(1)
    _emit_pack(event_log, pack_id="p1", occurred_at=d1)
    _emit_feedback(event_log, pack_id="p1", occurred_at=d1, success=True)
    result = compute_timeseries(
        event_log, metric=METRIC_PACK_SUCCESS_RATE, days=30
    )
    # Only the one populated day appears — no zero-filled gaps.
    assert len(result.series[0].points) == 1


def test_pack_success_rate_feedback_without_pack_skipped(event_log):
    # Feedback whose pack_id has no PACK_ASSEMBLED is not counted.
    _emit_feedback(event_log, pack_id="orphan", occurred_at=_day(1), success=True)
    result = compute_timeseries(
        event_log, metric=METRIC_PACK_SUCCESS_RATE, days=30
    )
    assert result.series == []


# ---------------------------------------------------------------------------
# reference_rate
# ---------------------------------------------------------------------------


def test_reference_rate_pooled_fraction(event_log):
    # Pooled: (1 + 2) referenced / (4 + 4) served = 3/8 = 0.375.
    d1 = _day(1)
    _emit_pack(event_log, pack_id="p1", occurred_at=d1)
    _emit_pack(event_log, pack_id="p2", occurred_at=d1)
    _emit_feedback(
        event_log,
        pack_id="p1",
        occurred_at=d1,
        success=True,
        served=["a", "b", "c", "d"],
        referenced=["a"],
    )
    _emit_feedback(
        event_log,
        pack_id="p2",
        occurred_at=d1,
        success=False,
        served=["e", "f", "g", "h"],
        referenced=["e", "f"],
    )
    result = compute_timeseries(event_log, metric=METRIC_REFERENCE_RATE, days=30)
    point = result.series[0].points[0]
    assert point.value == 0.375
    assert point.sample_count == 2


def test_reference_rate_falls_back_to_injected_items(event_log):
    # Feedback omits items_served; falls back to PACK_ASSEMBLED injected_item_ids.
    d1 = _day(1)
    _emit_pack(
        event_log, pack_id="p1", occurred_at=d1, injected=["a", "b"]
    )
    _emit_feedback(
        event_log, pack_id="p1", occurred_at=d1, success=True, referenced=["a"]
    )
    result = compute_timeseries(event_log, metric=METRIC_REFERENCE_RATE, days=30)
    assert result.series[0].points[0].value == 0.5


# ---------------------------------------------------------------------------
# group_by correctness
# ---------------------------------------------------------------------------


def test_group_by_domain(event_log):
    d1 = _day(1)
    _emit_pack(event_log, pack_id="p1", occurred_at=d1, domain="alpha")
    _emit_pack(event_log, pack_id="p2", occurred_at=d1, domain="beta")
    _emit_feedback(event_log, pack_id="p1", occurred_at=d1, success=True)
    _emit_feedback(event_log, pack_id="p2", occurred_at=d1, success=False)

    result = compute_timeseries(
        event_log, metric=METRIC_PACK_SUCCESS_RATE, days=30, group_by="domain"
    )
    by_group = {s.group_key: s for s in result.series}
    assert set(by_group) == {"alpha", "beta"}
    assert by_group["alpha"].points[0].value == 1.0
    assert by_group["beta"].points[0].value == 0.0


def test_group_by_intent_family_from_feedback(event_log):
    d1 = _day(1)
    _emit_pack(event_log, pack_id="p1", occurred_at=d1)
    _emit_feedback(
        event_log,
        pack_id="p1",
        occurred_at=d1,
        success=True,
        intent_family="debug",
    )
    result = compute_timeseries(
        event_log,
        metric=METRIC_PACK_SUCCESS_RATE,
        days=30,
        group_by="intent_family",
    )
    assert result.series[0].group_key == "debug"


def test_group_by_none_collapses_to_all(event_log):
    d1 = _day(1)
    _emit_pack(event_log, pack_id="p1", occurred_at=d1, domain="alpha")
    _emit_pack(event_log, pack_id="p2", occurred_at=d1, domain="beta")
    _emit_feedback(event_log, pack_id="p1", occurred_at=d1, success=True)
    _emit_feedback(event_log, pack_id="p2", occurred_at=d1, success=True)
    result = compute_timeseries(
        event_log, metric=METRIC_PACK_SUCCESS_RATE, days=30, group_by="none"
    )
    assert len(result.series) == 1
    assert result.series[0].group_key == "all"
    assert result.series[0].points[0].value == 1.0
    assert result.series[0].points[0].sample_count == 2


# ---------------------------------------------------------------------------
# advisory_fitness
# ---------------------------------------------------------------------------


def test_advisory_fitness_mean_confidence_and_suppressed_count(event_log):
    d1 = _day(1)
    for conf, adv in ((0.2, "adv-1"), (0.4, "adv-2")):
        event_log.append(
            Event(
                event_type=EventType.ADVISORY_SUPPRESSED,
                source="test",
                entity_id=adv,
                occurred_at=d1,
                payload={"advisory_id": adv, "new_confidence": conf},
            )
        )
    # A restore event contributes to the mean but not the suppressed count.
    event_log.append(
        Event(
            event_type=EventType.ADVISORY_RESTORED,
            source="test",
            entity_id="adv-3",
            occurred_at=d1,
            payload={"advisory_id": "adv-3", "new_confidence": 0.9},
        )
    )
    result = compute_timeseries(
        event_log, metric=METRIC_ADVISORY_FITNESS, days=30
    )
    point = result.series[0].points[0]
    # mean(0.2, 0.4, 0.9) = 0.5
    assert point.value == 0.5
    # Two ADVISORY_SUPPRESSED events.
    assert point.sample_count == 2


# ---------------------------------------------------------------------------
# noise_tag_volume
# ---------------------------------------------------------------------------


def test_noise_tag_volume_counts_only_noise_transitions(event_log):
    d1 = _day(1)
    # Two items flipped to noise, one flipped to standard (ignored).
    for item_id, sq in (("i1", "noise"), ("i2", "noise"), ("i3", "standard")):
        event_log.append(
            Event(
                event_type=EventType.TAGS_REFRESHED,
                source="test",
                entity_id=item_id,
                occurred_at=d1,
                payload={"item_id": item_id, "after": {"signal_quality": sq}},
            )
        )
    result = compute_timeseries(
        event_log, metric=METRIC_NOISE_TAG_VOLUME, days=30
    )
    point = result.series[0].points[0]
    assert point.value == 2.0
    assert point.sample_count == 2


def test_noise_tag_volume_nested_content_tags_shape(event_log):
    d1 = _day(1)
    event_log.append(
        Event(
            event_type=EventType.TAGS_REFRESHED,
            source="test",
            entity_id="i1",
            occurred_at=d1,
            payload={
                "item_id": "i1",
                "after": {"content_tags": {"signal_quality": "noise"}},
            },
        )
    )
    result = compute_timeseries(
        event_log, metric=METRIC_NOISE_TAG_VOLUME, days=30
    )
    assert result.series[0].points[0].value == 1.0


# ---------------------------------------------------------------------------
# parameter_promotions
# ---------------------------------------------------------------------------


def test_parameter_promotions_grouped_by_event_type(event_log):
    d1 = _day(1)
    event_log.append(
        Event(
            event_type=EventType.PARAMS_UPDATED,
            source="test",
            entity_id="v1",
            occurred_at=d1,
            payload={"params_version": "v1"},
        )
    )
    event_log.append(
        Event(
            event_type=EventType.PARAMS_UPDATED,
            source="test",
            entity_id="v2",
            occurred_at=d1,
            payload={"params_version": "v2"},
        )
    )
    event_log.append(
        Event(
            event_type=EventType.TUNER_PROPOSAL_REJECTED,
            source="test",
            entity_id="prop-1",
            occurred_at=d1,
            payload={},
        )
    )
    result = compute_timeseries(
        event_log, metric=METRIC_PARAMETER_PROMOTIONS, days=30
    )
    by_group = {s.group_key: s for s in result.series}
    assert by_group[EventType.PARAMS_UPDATED.value].points[0].value == 2.0
    assert by_group[EventType.TUNER_PROPOSAL_REJECTED.value].points[0].value == 1.0


# ---------------------------------------------------------------------------
# window filtering
# ---------------------------------------------------------------------------


def test_days_window_excludes_old_events(event_log):
    old = _day(40)
    recent = _day(2)
    _emit_pack(event_log, pack_id="old", occurred_at=old)
    _emit_pack(event_log, pack_id="recent", occurred_at=recent)
    _emit_feedback(event_log, pack_id="old", occurred_at=old, success=True)
    _emit_feedback(event_log, pack_id="recent", occurred_at=recent, success=True)

    result = compute_timeseries(
        event_log, metric=METRIC_PACK_SUCCESS_RATE, days=7
    )
    all_buckets = [p.bucket_start for s in result.series for p in s.points]
    assert recent.date().isoformat() in all_buckets
    assert old.date().isoformat() not in all_buckets
