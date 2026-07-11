"""Tests for :mod:`trellis.retrieve.trellis_cost`."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.retrieve.token_tracker import track_token_usage
from trellis.retrieve.trellis_cost import TrellisCostReport, summarize_trellis_cost
from trellis.stores.sqlite.event_log import SQLiteEventLog


@pytest.fixture
def event_log(tmp_path: Path):
    log = SQLiteEventLog(tmp_path / "events.db")
    yield log
    log.close()


def _seed(log: SQLiteEventLog) -> None:
    for _ in range(20):
        track_token_usage(
            log, layer="mcp", operation="get_context", response_tokens=1500
        )
    for _ in range(8):
        track_token_usage(
            log, layer="mcp", operation="get_lessons", response_tokens=600
        )


class TestSummarizeTrellisCost:
    def test_empty_log_is_zero_cost(self, event_log, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        report = summarize_trellis_cost(event_log, days=7)
        assert isinstance(report, TrellisCostReport)
        assert report.overhead_events == 0
        assert report.overhead_tokens == 0
        assert report.overhead_dollars == 0.0
        assert report.by_operation == []

    def test_totals_and_dollars(self, event_log, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _seed(event_log)
        report = summarize_trellis_cost(event_log, days=7, model="claude-opus")
        assert report.overhead_events == 28
        assert report.overhead_tokens == 20 * 1500 + 8 * 600  # 34_800
        assert report.price_per_mtok == 15.0
        # 34_800 / 1e6 * 15 = 0.522
        assert report.overhead_dollars == pytest.approx(0.522)

    def test_per_operation_dollars(self, event_log, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _seed(event_log)
        report = summarize_trellis_cost(event_log, days=7, model="claude-sonnet")
        by_op = {op["operation"]: op for op in report.by_operation}
        assert by_op["get_context"]["tokens"] == 30_000
        assert by_op["get_context"]["calls"] == 20
        # 30_000 / 1e6 * 3 = 0.09
        assert by_op["get_context"]["dollars"] == pytest.approx(0.09)
        assert by_op["get_lessons"]["dollars"] == pytest.approx(0.0144)

    def test_price_override_flows_through(self, event_log, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _seed(event_log)
        report = summarize_trellis_cost(event_log, days=7, price_per_mtok=10.0)
        assert report.price_per_mtok == 10.0
        assert report.price_source == "explicit_override"
        assert report.overhead_dollars == pytest.approx(34_800 / 1e6 * 10.0)

    def test_report_is_json_serializable(self, event_log, monkeypatch):
        monkeypatch.delenv("TRELLIS_COST_PRICE_PER_MTOK", raising=False)
        _seed(event_log)
        payload = summarize_trellis_cost(event_log, days=7).model_dump()
        assert payload["estimator"] == "estimate_4_chars_per_token"
        assert set(payload["by_operation"][0]) == {
            "operation",
            "layer",
            "calls",
            "tokens",
            "dollars",
        }
