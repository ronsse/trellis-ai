"""Tests for :mod:`trellis.retrieve.token_usage` aggregation logic.

``analyze_token_usage`` reads ``TOKEN_TRACKED`` events from the EventLog
and produces a :class:`TokenUsageReport` with totals, per-layer
breakdown, top-10 operations by total tokens, and over-budget incidents.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trellis.retrieve.token_usage import TokenUsageReport, analyze_token_usage
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _emit_token_event(
    log: SQLiteEventLog,
    *,
    layer: str,
    operation: str,
    response_tokens: int,
    budget_tokens: int | None = None,
) -> None:
    payload: dict[str, object] = {
        "layer": layer,
        "operation": operation,
        "response_tokens": response_tokens,
    }
    if budget_tokens is not None:
        payload["budget_tokens"] = budget_tokens
    log.emit(
        EventType.TOKEN_TRACKED,
        source="test",
        payload=payload,
    )


# --------------------------------------------------------------------------
# Empty / boundary
# --------------------------------------------------------------------------


def test_empty_log_returns_zeros(event_log: SQLiteEventLog) -> None:
    report = analyze_token_usage(event_log)
    assert isinstance(report, TokenUsageReport)
    assert report.total_responses == 0
    assert report.total_tokens == 0
    assert report.avg_tokens_per_response == 0.0
    assert report.by_layer == {}
    assert report.by_operation == []
    assert report.over_budget == []


def test_only_non_token_events_are_ignored(event_log: SQLiteEventLog) -> None:
    # Emit a non-token event; the analyser should not pick it up.
    event_log.emit(
        EventType.PACK_ASSEMBLED,
        source="test",
        entity_id="pack-1",
        payload={"layer": "cli", "response_tokens": 9999},
    )
    report = analyze_token_usage(event_log)
    assert report.total_responses == 0
    assert report.total_tokens == 0


# --------------------------------------------------------------------------
# Single-event aggregation
# --------------------------------------------------------------------------


def test_single_event_populates_totals(event_log: SQLiteEventLog) -> None:
    _emit_token_event(
        event_log,
        layer="cli",
        operation="trace.ingest",
        response_tokens=120,
    )
    report = analyze_token_usage(event_log)

    assert report.total_responses == 1
    assert report.total_tokens == 120
    assert report.avg_tokens_per_response == pytest.approx(120.0)
    assert report.by_layer == {
        "cli": {"count": 1, "total_tokens": 120, "avg_tokens": 120.0}
    }
    [op] = report.by_operation
    assert op["layer"] == "cli"
    assert op["operation"] == "trace.ingest"
    assert op["count"] == 1
    assert op["total_tokens"] == 120
    assert op["avg_tokens"] == pytest.approx(120.0)
    assert report.over_budget == []


# --------------------------------------------------------------------------
# Cumulative tracking across multiple events
# --------------------------------------------------------------------------


def test_cumulative_aggregation_across_events(event_log: SQLiteEventLog) -> None:
    """Multiple events accumulate per-layer and per-op counts and totals."""
    _emit_token_event(event_log, layer="cli", operation="op_a", response_tokens=100)
    _emit_token_event(event_log, layer="cli", operation="op_a", response_tokens=200)
    _emit_token_event(event_log, layer="mcp", operation="op_b", response_tokens=50)

    report = analyze_token_usage(event_log)
    assert report.total_responses == 3
    assert report.total_tokens == 350
    assert report.avg_tokens_per_response == pytest.approx(350 / 3)

    cli_stats = report.by_layer["cli"]
    assert cli_stats["count"] == 2
    assert cli_stats["total_tokens"] == 300
    assert cli_stats["avg_tokens"] == 150.0

    mcp_stats = report.by_layer["mcp"]
    assert mcp_stats["count"] == 1
    assert mcp_stats["total_tokens"] == 50
    assert mcp_stats["avg_tokens"] == 50.0


def test_by_operation_sorted_by_total_tokens_desc(event_log: SQLiteEventLog) -> None:
    """Top-10 operations are sorted by ``total_tokens`` descending."""
    _emit_token_event(event_log, layer="cli", operation="small", response_tokens=10)
    _emit_token_event(event_log, layer="cli", operation="big", response_tokens=1000)
    _emit_token_event(event_log, layer="cli", operation="medium", response_tokens=100)

    report = analyze_token_usage(event_log)
    operations = [op["operation"] for op in report.by_operation]
    assert operations == ["big", "medium", "small"]


def test_by_operation_top_10_cap(event_log: SQLiteEventLog) -> None:
    """Only the top-10 operations by total tokens are returned."""
    for i in range(15):
        _emit_token_event(
            event_log,
            layer="cli",
            operation=f"op_{i:02d}",
            response_tokens=10 * (i + 1),
        )
    report = analyze_token_usage(event_log)
    assert len(report.by_operation) == 10
    # The biggest 10 are op_05..op_14 (10*6..10*15).
    expected_ops = [f"op_{i:02d}" for i in range(14, 4, -1)]
    actual_ops = [op["operation"] for op in report.by_operation]
    assert actual_ops == expected_ops


# --------------------------------------------------------------------------
# Budget exhaustion / over-budget detection
# --------------------------------------------------------------------------


def test_over_budget_detected_when_response_exceeds_budget(
    event_log: SQLiteEventLog,
) -> None:
    _emit_token_event(
        event_log,
        layer="cli",
        operation="bloated",
        response_tokens=2000,
        budget_tokens=1000,
    )
    report = analyze_token_usage(event_log)
    assert len(report.over_budget) == 1
    incident = report.over_budget[0]
    assert incident["layer"] == "cli"
    assert incident["operation"] == "bloated"
    assert incident["response_tokens"] == 2000
    assert incident["budget_tokens"] == 1000
    # ISO timestamp in the incident record.
    assert isinstance(incident["occurred_at"], str)


def test_under_budget_does_not_trigger_alert(event_log: SQLiteEventLog) -> None:
    _emit_token_event(
        event_log,
        layer="cli",
        operation="thrifty",
        response_tokens=500,
        budget_tokens=1000,
    )
    report = analyze_token_usage(event_log)
    assert report.over_budget == []


def test_at_budget_boundary_is_not_over_budget(event_log: SQLiteEventLog) -> None:
    """``response_tokens == budget_tokens`` is a strict ``>`` comparison."""
    _emit_token_event(
        event_log,
        layer="cli",
        operation="exact",
        response_tokens=1000,
        budget_tokens=1000,
    )
    report = analyze_token_usage(event_log)
    assert report.over_budget == []


def test_no_budget_set_means_no_overage_check(event_log: SQLiteEventLog) -> None:
    """Events without ``budget_tokens`` cannot be over budget."""
    _emit_token_event(
        event_log,
        layer="cli",
        operation="unbounded",
        response_tokens=1_000_000,
        budget_tokens=None,
    )
    report = analyze_token_usage(event_log)
    assert report.over_budget == []


# --------------------------------------------------------------------------
# Time-window filtering
# --------------------------------------------------------------------------


def test_days_window_filters_old_events(event_log: SQLiteEventLog) -> None:
    """Events older than ``days`` are excluded by ``since`` filter."""
    # Recent event — counted.
    _emit_token_event(event_log, layer="cli", operation="recent", response_tokens=100)

    # Backdate one event to 30 days ago by emitting raw to the underlying
    # store.  We use ``append`` directly with an explicit ``occurred_at``.
    from trellis.stores.base.event_log import Event

    old_event = Event(
        event_type=EventType.TOKEN_TRACKED,
        source="test",
        occurred_at=datetime.now(tz=UTC) - timedelta(days=30),
        payload={"layer": "cli", "operation": "old", "response_tokens": 9999},
    )
    event_log.append(old_event)

    report = analyze_token_usage(event_log, days=7)
    # Only the recent event should be tallied.
    assert report.total_responses == 1
    assert report.total_tokens == 100
    [op] = report.by_operation
    assert op["operation"] == "recent"


# --------------------------------------------------------------------------
# Defensive: missing keys default sensibly
# --------------------------------------------------------------------------


def test_missing_layer_and_operation_default_to_unknown(
    event_log: SQLiteEventLog,
) -> None:
    """A token event with only ``response_tokens`` lands in ``unknown:unknown``."""
    event_log.emit(
        EventType.TOKEN_TRACKED,
        source="test",
        payload={"response_tokens": 42},
    )
    report = analyze_token_usage(event_log)
    assert report.total_tokens == 42
    assert "unknown" in report.by_layer
    assert report.by_layer["unknown"]["total_tokens"] == 42
    [op] = report.by_operation
    assert op["layer"] == "unknown"
    assert op["operation"] == "unknown"


def test_missing_response_tokens_treated_as_zero(event_log: SQLiteEventLog) -> None:
    event_log.emit(
        EventType.TOKEN_TRACKED,
        source="test",
        payload={"layer": "cli", "operation": "noop"},
    )
    report = analyze_token_usage(event_log)
    # Counted as a response but with zero tokens.
    assert report.total_responses == 1
    assert report.total_tokens == 0
    assert report.avg_tokens_per_response == 0.0
