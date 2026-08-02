"""Tests for the MCP ``record_feedback`` tool.

The tool is the path Claude Code actually uses, so it owns two
guarantees the rest of the feedback machinery depends on:

* a *graded* signal reaches the EventLog (a hard-coded 1.0/0.0 gives the
  advisory generator no variance to work with), and
* every call also appends the durable ``pack_feedback.jsonl`` row, so a
  soft-failed emit can be replayed by ``trellis admin reconcile-feedback``.

``tests/unit/mcp/test_server.py::TestRecordFeedback`` keeps the original
boolean-surface assertions; this module covers the graded surface, the
JSONL parity and the degraded paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

from tests.unit.mcp.conftest import unwrap_tool
from trellis.feedback.recording import (
    load_feedback_log,
    reconcile_feedback_log_to_event_log,
)
from trellis.mcp.server import record_feedback as _record_feedback
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

record_feedback = unwrap_tool(_record_feedback)


def _log_dir(registry: StoreRegistry) -> Path:
    """Directory the reconcile CLI is pointed at for this registry."""
    assert registry.stores_dir is not None
    return registry.stores_dir / "feedback"


def _rows(registry: StoreRegistry) -> list[dict[str, Any]]:
    log_path = _log_dir(registry) / "pack_feedback.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _emit_boom(*args: Any, **kwargs: Any) -> None:
    """Stand-in for an EventLog whose backend is unavailable."""
    raise _SINK_DOWN


#: Module-level so the raise site stays a bare ``raise <var>``.
_SINK_DOWN = RuntimeError("event sink down")


def _payloads(registry: StoreRegistry) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in registry.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=10
        )
    ]


class TestGradedRating:
    """A real gradient, end to end: tool -> JSONL row -> event payload."""

    def test_rating_reaches_both_sinks(self, temp_registry: StoreRegistry) -> None:
        record_feedback(pack_id="pack_grade", rating=0.35)

        (row,) = _rows(temp_registry)
        assert row["rating"] == 0.35
        (payload,) = _payloads(temp_registry)
        assert payload["rating"] == 0.35
        assert payload["pack_id"] == "pack_grade"

    def test_low_rating_derives_failure(self, temp_registry: StoreRegistry) -> None:
        # Consumers read payload["success"] first and only fall back to
        # rating, so an omitted success must follow the grade — otherwise
        # the default True would mask every mediocre pack.
        result = record_feedback(pack_id="pack_meh", rating=0.2)

        assert "negative" in result
        (payload,) = _payloads(temp_registry)
        assert payload["success"] is False
        assert payload["outcome"] == "failure"
        assert payload["rating"] == 0.2

    def test_high_rating_derives_success(self, temp_registry: StoreRegistry) -> None:
        record_feedback(pack_id="pack_good", rating=0.9)

        (payload,) = _payloads(temp_registry)
        assert payload["success"] is True
        assert payload["rating"] == 0.9

    def test_explicit_success_overrides_threshold(
        self, temp_registry: StoreRegistry
    ) -> None:
        # An explicit claim from the caller wins; the grade is still kept.
        record_feedback(pack_id="pack_x", success=True, rating=0.2)

        (payload,) = _payloads(temp_registry)
        assert payload["success"] is True
        assert payload["rating"] == 0.2

    def test_ratings_vary_across_calls(self, temp_registry: StoreRegistry) -> None:
        # The defect this replaces made every event success=1.0; variance
        # across calls is the whole point.
        for i, grade in enumerate((0.1, 0.5, 0.95)):
            record_feedback(pack_id=f"pack_{i}", rating=grade)

        assert sorted(p["rating"] for p in _payloads(temp_registry)) == [0.1, 0.5, 0.95]

    def test_rating_out_of_range_raises_invalid_params(self) -> None:
        for bad in (-0.1, 1.5):
            with pytest.raises(McpError) as excinfo:
                record_feedback(pack_id="pack_1", rating=bad)
            assert excinfo.value.error.code == INVALID_PARAMS
            assert "rating must be between 0.0 and 1.0" in excinfo.value.error.message
            assert excinfo.value.error.data == {"field": "rating", "value": bad}


class TestBooleanFallback:
    """The pre-existing boolean surface keeps its exact semantics."""

    def test_success_true_is_rating_one(self, temp_registry: StoreRegistry) -> None:
        result = record_feedback(pack_id="pack_ok", success=True)

        assert "positive" in result
        (payload,) = _payloads(temp_registry)
        assert payload["success"] is True
        assert payload["rating"] == 1.0
        assert _rows(temp_registry)[0]["rating"] == 1.0

    def test_success_false_is_rating_zero(self, temp_registry: StoreRegistry) -> None:
        result = record_feedback(pack_id="pack_bad", success=False)

        assert "negative" in result
        (payload,) = _payloads(temp_registry)
        assert payload["success"] is False
        assert payload["rating"] == 0.0

    def test_neither_flag_defaults_to_success(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_default")

        (payload,) = _payloads(temp_registry)
        assert payload["success"] is True
        assert payload["rating"] == 1.0

    def test_trace_feedback_keeps_trace_entity(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback("trace_1", success=True)

        events = temp_registry.operational.event_log.get_events(entity_id="trace_1")
        assert len(events) == 1
        assert events[0].entity_type == "trace"
        assert _rows(temp_registry)[0]["run_id"] == "trace_1"


class TestJsonlParity:
    """Every call lands a row where reconciliation looks for it."""

    def test_row_is_written_where_reconcile_reads(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_r", rating=0.6, notes="half useful")

        signals = load_feedback_log(_log_dir(temp_registry))
        assert len(signals) == 1
        assert signals[0].rating == 0.6
        assert signals[0].metadata["notes"] == "half useful"

    def test_reconcile_sees_the_row_as_already_present(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(pack_id="pack_r2", rating=0.7)

        result = reconcile_feedback_log_to_event_log(
            _log_dir(temp_registry), temp_registry.operational.event_log
        )
        assert (result.scanned, result.already_present, result.emitted) == (1, 1, 0)

    def test_row_carries_pack_id_for_replay(self, temp_registry: StoreRegistry) -> None:
        record_feedback(pack_id="pack_r3", rating=0.4)

        assert _rows(temp_registry)[0]["metadata"]["pack_id"] == "pack_r3"


class TestFailingEventSink:
    """A sink outage degrades to the audit row; it never reaches the agent."""

    def test_emit_failure_does_not_raise_and_row_lands(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event_log = temp_registry.operational.event_log
        monkeypatch.setattr(event_log, "emit", _emit_boom)
        result = record_feedback(pack_id="pack_down", rating=0.8)

        assert "reconcile-feedback" in result
        assert _payloads(temp_registry) == []
        (row,) = _rows(temp_registry)
        assert row["rating"] == 0.8

    def test_dropped_event_is_recoverable_by_reconcile(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event_log = temp_registry.operational.event_log
        monkeypatch.setattr(event_log, "emit", _emit_boom)
        record_feedback(
            pack_id="pack_recover", rating=0.25, unhelpful_item_ids=["doc_noise"]
        )
        monkeypatch.undo()

        result = reconcile_feedback_log_to_event_log(_log_dir(temp_registry), event_log)
        assert (result.scanned, result.emitted, result.failed) == (1, 1, 0)
        (payload,) = _payloads(temp_registry)
        assert payload["rating"] == 0.25
        assert payload["unhelpful_item_ids"] == ["doc_noise"]
        # The pack association survives the replay, so the advisory /
        # effectiveness joins can still use the recovered event.
        assert payload["pack_id"] == "pack_recover"


class TestAttributionSurvives:
    """Element-level ids reach both the event payload and the JSONL row."""

    def test_ids_in_event_payload_and_jsonl(self, temp_registry: StoreRegistry) -> None:
        record_feedback(
            pack_id="pack_attr",
            rating=0.5,
            helpful_item_ids=["doc_a", "entity_b"],
            unhelpful_item_ids=["doc_noise"],
            followed_advisory_ids=["adv_1"],
        )

        (payload,) = _payloads(temp_registry)
        assert payload["helpful_item_ids"] == ["doc_a", "entity_b"]
        assert payload["unhelpful_item_ids"] == ["doc_noise"]
        assert payload["followed_advisory_ids"] == ["adv_1"]
        # Everything cited counts as served, so effectiveness can score
        # the unreferenced items too.
        assert payload["items_served"] == ["doc_a", "entity_b", "doc_noise"]

        (row,) = _rows(temp_registry)
        assert row["items_referenced"] == ["doc_a", "entity_b"]
        assert row["unhelpful_item_ids"] == ["doc_noise"]
        assert row["followed_advisory_ids"] == ["adv_1"]

    def test_trace_association_kept_when_both_ids_given(
        self, temp_registry: StoreRegistry
    ) -> None:
        record_feedback(trace_id="trace_z", pack_id="pack_z", success=True)

        (row,) = _rows(temp_registry)
        assert row["metadata"]["trace_id"] == "trace_z"
        (payload,) = _payloads(temp_registry)
        assert payload["pack_id"] == "pack_z"
