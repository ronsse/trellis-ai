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
from trellis.retrieve.metrics_timeseries import (
    METRIC_REFERENCE_RATE,
    compute_timeseries,
)
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
        # The row records "ungraded" rather than a fabricated 1.0; the
        # event payload derives the grade, so a replay reproduces it.
        assert _rows(temp_registry)[0]["rating"] is None

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
        row = _rows(temp_registry)[0]
        assert row["run_id"] == "trace_1"
        # Stamped even without a pack — it is what reconcile recovers
        # the trace association from after a soft-failed emit.
        assert row["metadata"]["trace_id"] == "trace_1"


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

    def test_trace_only_association_survives_replay(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Symmetric with the pack case: trace-level feedback replayed
        # after a soft-failed emit must come back reachable by entity,
        # not with entity_id=None and the association buried in run_id.
        event_log = temp_registry.operational.event_log
        monkeypatch.setattr(event_log, "emit", _emit_boom)
        record_feedback(trace_id="trace_only_1", rating=0.9)
        monkeypatch.undo()

        result = reconcile_feedback_log_to_event_log(_log_dir(temp_registry), event_log)
        assert (result.scanned, result.emitted, result.failed) == (1, 1, 0)
        (event,) = event_log.get_events(entity_id="trace_only_1")
        assert event.entity_type == "trace"
        assert event.payload["rating"] == 0.9


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
        # items_served stays empty: the cited ids are what the agent
        # *referenced*, not what the pack *contained*. Unioning them
        # would report a 100% reference rate for every graded pack.
        # Empty is falsy, so the read side keeps falling back to the
        # joined PACK_ASSEMBLED injected_item_ids (see
        # ``test_reference_rate_uses_pack_contents_not_citations``).
        assert payload["items_served"] == []

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

    def test_reference_rate_uses_pack_contents_not_citations(
        self, temp_registry: StoreRegistry
    ) -> None:
        """Citing two of ten served items is a 20% reference rate, not 100%.

        Synthesizing ``items_served`` from the cited ids would make the
        denominator equal the numerator for every attributed pack, so the
        Memory Explorer's reference-rate chart would read a hard 1.0 on
        exactly the calls this tool exists to encourage.
        """
        event_log = temp_registry.operational.event_log
        served = [f"doc_{i}" for i in range(10)]
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack_rr",
            entity_type="pack",
            payload={"intent": "t", "injected_item_ids": served},
        )

        record_feedback(
            pack_id="pack_rr", rating=0.6, helpful_item_ids=["doc_0", "doc_1"]
        )

        result = compute_timeseries(event_log, metric=METRIC_REFERENCE_RATE, days=1)
        (point,) = result.series[0].points
        assert point.value == pytest.approx(0.2)


class TestPackAttributionRequirement:
    """The default-off enforcement knob (``TRELLIS_REQUIRE_PACK_ATTRIBUTION``).

    Shipped **off**: today's behaviour is unchanged and an uncited
    pack-targeted call still records a rating. A cross-lab panel split on
    whether refusal should be the default, so the default stays the
    operator's call — see
    :data:`trellis.core.write_config.REQUIRE_PACK_ATTRIBUTION_FLAG`.

    What the tests pin either way is the fail-open boundary. Enforcement
    may only fire when the pack demonstrably served ids the caller could
    have cited; every other case has to let the call through, because
    refusing someone for not citing ids nobody can produce converts a
    recorded rating into a lost one.
    """

    def _serve_pack(
        self, registry: StoreRegistry, pack_id: str, item_ids: list[str]
    ) -> None:
        registry.operational.event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={"intent": "t", "injected_item_ids": item_ids},
        )

    def test_off_by_default_uncited_pack_feedback_is_recorded(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Explicit: a developer's shell must not decide what "default" means.
        monkeypatch.delenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", raising=False)
        self._serve_pack(temp_registry, "pack_a", ["doc_1", "doc_2"])

        result = record_feedback(pack_id="pack_a", rating=0.4)

        assert "Feedback recorded" in result
        (payload,) = _payloads(temp_registry)
        assert payload["pack_id"] == "pack_a"
        assert payload["helpful_item_ids"] == []

    def test_enforced_rejects_and_hands_back_the_served_ids(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        self._serve_pack(temp_registry, "pack_a", ["doc_1", "doc_2"])

        with pytest.raises(McpError) as excinfo:
            record_feedback(pack_id="pack_a", rating=0.4)

        assert excinfo.value.error.code == INVALID_PARAMS
        data = excinfo.value.error.data
        assert isinstance(data, dict)
        # The ids come from the pack's own event, so the retry is a
        # selection among what was served rather than a recollection.
        assert data["item_ids"] == ["doc_1", "doc_2"]
        assert data["pack_id"] == "pack_a"
        # Neither sink was written: the refusal precedes the record, so a
        # retry cannot double-count the same grade.
        assert _payloads(temp_registry) == []
        assert _rows(temp_registry) == []

    def test_enforced_call_is_recorded_once_cited(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        self._serve_pack(temp_registry, "pack_a", ["doc_1", "doc_2"])

        record_feedback(pack_id="pack_a", rating=0.4, helpful_item_ids=["doc_1"])

        (payload,) = _payloads(temp_registry)
        assert payload["helpful_item_ids"] == ["doc_1"]

    def test_a_pack_that_missed_is_cited_as_unhelpful(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "None of it helped" is a citation, not an exemption.

        The escape hatch from the requirement is the more valuable of the
        two signals, not a cheaper one — there is no flag that means
        "I looked and decline to say".
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        self._serve_pack(temp_registry, "pack_a", ["doc_1", "doc_2"])

        record_feedback(
            pack_id="pack_a", rating=0.0, unhelpful_item_ids=["doc_1", "doc_2"]
        )

        (payload,) = _payloads(temp_registry)
        assert payload["unhelpful_item_ids"] == ["doc_1", "doc_2"]

    def test_followed_advisory_satisfies_the_requirement(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        self._serve_pack(temp_registry, "pack_a", ["doc_1"])

        record_feedback(pack_id="pack_a", rating=0.7, followed_advisory_ids=["adv_1"])

        (payload,) = _payloads(temp_registry)
        assert payload["followed_advisory_ids"] == ["adv_1"]

    def test_trace_level_feedback_is_never_rejected(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Grading work that no pack informed stays a first-class signal.

        This is the case the deployment's unattributed feedback is
        overwhelmingly made of, and it is honest: there is no pack, so
        there is nothing to cite. Enforcement must not reach it.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")

        result = record_feedback(trace_id="trace_1", rating=0.9)

        assert "Feedback recorded" in result
        (payload,) = _payloads(temp_registry)
        assert "pack_id" not in payload
        assert payload["helpful_item_ids"] == []

    def test_unknown_pack_fails_open(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No PACK_ASSEMBLED event means no ids to offer — let it through."""
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")

        result = record_feedback(pack_id="pack_missing", rating=0.4)

        assert "Feedback recorded" in result
        assert len(_payloads(temp_registry)) == 1

    def test_sectioned_pack_fails_open(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``build_sectioned`` emits no per-item rows, so nothing is citable."""
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        temp_registry.operational.event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id="pack_sectioned",
            entity_type="pack",
            payload={"intent": "t", "section_count": 2},
        )

        result = record_feedback(pack_id="pack_sectioned", rating=0.4)

        assert "Feedback recorded" in result

    def test_rejection_is_recorded_as_boundary_telemetry(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refusal has to be visible in ``trellis analyze health`` (#297)."""
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        self._serve_pack(temp_registry, "pack_a", ["doc_1"])

        with pytest.raises(McpError):
            record_feedback(pack_id="pack_a", rating=0.4)

        rejections = temp_registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=10
        )
        assert len(rejections) == 1
        assert rejections[0].payload["tool"] == "record_feedback"
