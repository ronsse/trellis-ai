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
from trellis.feedback.attribution import (
    lookup_pack_bodied_item_ids,
    payload_is_attributed,
)
from trellis.feedback.recording import (
    load_feedback_log,
    reconcile_feedback_log_to_event_log,
)
from trellis.mcp.server import record_feedback as _record_feedback
from trellis.retrieve.disclosure import DisclosureConfig
from trellis.retrieve.metrics_timeseries import (
    METRIC_REFERENCE_RATE,
    compute_timeseries,
)
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SearchStrategy
from trellis.schemas.pack import PackBudget, PackItem
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


class _FixedStrategy(SearchStrategy):
    """Returns a fixed candidate list, so the pack shape is the variable."""

    def __init__(self, items: list[PackItem]) -> None:
        self._items = items

    @property
    def name(self) -> str:
        return "keyword"

    def search(
        self,
        intent: str,
        domain: str | None = None,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[PackItem]:
        return [item.model_copy() for item in self._items[:limit]]


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


class TestIgnoredVerdict:
    """The third verdict (#550): read, and not used.

    Its whole value is that it is a claim a caller can make about the
    *majority* of a pack — measured, 49.8% of bodied servings carry no
    verdict at all — without inventing the judgement ``unhelpful`` makes.
    """

    def test_reaches_both_sinks(self, temp_registry: StoreRegistry) -> None:
        record_feedback(
            pack_id="pack_i",
            rating=0.5,
            helpful_item_ids=["doc_a"],
            ignored_item_ids=["doc_b", "doc_c"],
        )

        (payload,) = _payloads(temp_registry)
        assert payload["helpful_item_ids"] == ["doc_a"]
        assert payload["ignored_item_ids"] == ["doc_b", "doc_c"]

        (row,) = _rows(temp_registry)
        assert row["ignored_item_ids"] == ["doc_b", "doc_c"]

    def test_absent_when_not_supplied(self, temp_registry: StoreRegistry) -> None:
        """Absent is "no third verdict given", not "considered and ignored none"."""
        record_feedback(pack_id="pack_i", rating=0.5, helpful_item_ids=["doc_a"])

        (payload,) = _payloads(temp_registry)
        assert "ignored_item_ids" not in payload

    def test_it_is_not_folded_into_the_graded_verdicts(
        self, temp_registry: StoreRegistry
    ) -> None:
        """An ignored id must never reach the two the learning join grades.

        ``learning.pack_observations`` reads helpful/unhelpful; letting
        an ignored id leak into either would turn "I did not use this"
        into a graded outcome nobody claimed.
        """
        record_feedback(pack_id="pack_i", rating=0.5, ignored_item_ids=["doc_b"])

        (payload,) = _payloads(temp_registry)
        assert payload["helpful_item_ids"] == []
        # ``unhelpful_item_ids`` is omitted rather than empty by the same
        # convention the third verdict follows, so its absence — not an
        # empty list — is what "nothing was graded unhelpful" looks like.
        assert "unhelpful_item_ids" not in payload
        assert payload["ignored_item_ids"] == ["doc_b"]


class TestIgnoredDoesNotSatisfyPackAttribution:
    """An ignored-only call is honest, complete — and joins to nothing.

    The pack gate's trigger is deliberately the three keys
    ``payload_is_attributed`` reads, so the MCP boundary and
    ``analyze health`` cannot disagree about what "attributed" means.
    Adding the third verdict to one side only is the exact defect this
    change is otherwise built to avoid.
    """

    def _serve_pack(self, registry: StoreRegistry, pack_id: str) -> None:
        registry.operational.event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload={"intent": "t", "injected_item_ids": ["doc_1", "doc_2"]},
        )

    def test_ignored_only_is_still_rejected_when_enforced(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        monkeypatch.delenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", raising=False)
        self._serve_pack(temp_registry, "pack_a")

        with pytest.raises(McpError) as excinfo:
            record_feedback(
                pack_id="pack_a", rating=0.4, ignored_item_ids=["doc_1", "doc_2"]
            )

        assert excinfo.value.error.code == INVALID_PARAMS

    def test_the_refusal_says_why_rather_than_claiming_nothing_was_cited(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller who did the work deserves the real reason.

        "none cited" is false here and reads as a bug in the tool; the
        honest answer is that non-use contributes no rows to the join.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        monkeypatch.delenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", raising=False)
        self._serve_pack(temp_registry, "pack_a")

        with pytest.raises(McpError) as excinfo:
            record_feedback(pack_id="pack_a", rating=0.4, ignored_item_ids=["doc_1"])

        message = excinfo.value.error.message
        assert "ignored" in message
        assert "none cited" not in message

        (rejection,) = temp_registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=10
        )
        assert rejection.payload["tool"] == "record_feedback"

    def test_off_by_default_an_ignored_only_call_is_recorded(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", raising=False)
        monkeypatch.delenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", raising=False)
        self._serve_pack(temp_registry, "pack_a")

        result = record_feedback(
            pack_id="pack_a", rating=0.4, ignored_item_ids=["doc_1"]
        )

        assert "Feedback recorded" in result

    @pytest.mark.parametrize("subset", range(16))
    def test_gate_and_predicate_agree_on_every_combination(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        subset: int,
    ) -> None:
        """Derived agreement across all 16 shapes of the four id fields.

        The boundary refuses exactly when ``payload_is_attributed`` says
        the recorded event would be unattributed. Pinning the two against
        each other — rather than listing the cases each should accept —
        is what makes it impossible to teach one of them about
        ``ignored_item_ids`` and not the other.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        monkeypatch.delenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", raising=False)
        self._serve_pack(temp_registry, "pack_a")

        kwargs: dict[str, list[str]] = {}
        for bit, field in enumerate(
            (
                "helpful_item_ids",
                "unhelpful_item_ids",
                "followed_advisory_ids",
                "ignored_item_ids",
            )
        ):
            if subset & (1 << bit):
                kwargs[field] = ["doc_1"]

        try:
            record_feedback(pack_id="pack_a", rating=0.4, **kwargs)
        except McpError:
            recorded = None
        else:
            (recorded,) = _payloads(temp_registry)

        if recorded is None:
            # Rejected — so the event the call *would* have written must
            # be one the predicate calls unattributed.
            assert payload_is_attributed(kwargs) is False
        else:
            assert payload_is_attributed(recorded) is True


class TestBodiedAttributionRequirement:
    """``TRELLIS_REQUIRE_BODIED_ATTRIBUTION`` — a verdict per *shown* item.

    A different question from the pack gate. That one asks whether the
    feedback can join at all; this one asks whether every item the pack
    put an excerpt in front of the caller came back with a verdict.

    Ships **off**, and the arithmetic is why: graders already volunteer
    ~8.9 verdicts per pack, the median pack bodies 13 items, so full
    coverage is a ~46% increase on what the surface has ever produced.
    The failure mode of asking too much of a grading surface is the
    surface going quiet, and a lost rating is worse than an incomplete
    one.

    Pointers are outside the ask by construction — a caller shown a
    one-line label has no basis for a verdict — and every unknown
    narrows the ask to nothing rather than widening it.
    """

    def _serve(
        self,
        registry: StoreRegistry,
        pack_id: str,
        served: list[str],
        pointers: list[str] | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "intent": "t",
            "injected_item_ids": served,
            **extra,
        }
        if pointers is not None:
            payload["disclosure"] = {"mode": "on", "pointer_item_ids": pointers}
        registry.operational.event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="test",
            entity_id=pack_id,
            entity_type="pack",
            payload=payload,
        )

    def test_off_by_default_a_partial_verdict_is_recorded(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Explicit: a developer's shell must not decide what "default" means.
        monkeypatch.delenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", raising=False)
        self._serve(temp_registry, "pack_b", ["doc_1", "doc_2", "doc_3"], ["doc_3"])

        result = record_feedback(
            pack_id="pack_b", rating=0.5, helpful_item_ids=["doc_1"]
        )

        assert "Feedback recorded" in result

    def test_enforced_hands_back_only_the_unjudged_bodies(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        self._serve(
            temp_registry,
            "pack_b",
            ["doc_1", "doc_2", "doc_3", "doc_4"],
            ["doc_4"],
        )

        with pytest.raises(McpError) as excinfo:
            record_feedback(pack_id="pack_b", rating=0.5, helpful_item_ids=["doc_1"])

        data = excinfo.value.error.data
        assert isinstance(data, dict)
        # Not the served list and not the bodied list — the ids still
        # owed, so the retry is a list of things to do rather than a
        # list to diff.
        assert data["item_ids"] == ["doc_2", "doc_3"]
        assert data["bodied_item_count"] == 3
        assert data["fields"] == [
            "helpful_item_ids",
            "unhelpful_item_ids",
            "ignored_item_ids",
        ]
        assert _payloads(temp_registry) == []
        assert _rows(temp_registry) == []

    def test_a_followed_advisory_cannot_discharge_an_item_verdict(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An advisory is not a pack item, so it is absent from the fields.

        Offering it as a way to satisfy this gate would let a caller
        close out a twelve-item pack by naming one advisory.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        self._serve(temp_registry, "pack_b", ["doc_1"], [])

        with pytest.raises(McpError) as excinfo:
            record_feedback(
                pack_id="pack_b", rating=0.5, followed_advisory_ids=["adv_1"]
            )

        data = excinfo.value.error.data
        assert isinstance(data, dict)
        assert data["item_ids"] == ["doc_1"]
        assert "followed_advisory_ids" not in data["fields"]

    @pytest.mark.parametrize(
        "field", ["helpful_item_ids", "unhelpful_item_ids", "ignored_item_ids"]
    )
    def test_any_of_the_three_verdicts_discharges_an_item(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
    ) -> None:
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        monkeypatch.delenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", raising=False)
        self._serve(temp_registry, "pack_b", ["doc_1", "doc_2"], ["doc_2"])

        result = record_feedback(pack_id="pack_b", rating=0.5, **{field: ["doc_1"]})

        assert "Feedback recorded" in result

    def test_pointers_are_not_asked_about(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The half the caller never saw is outside the requirement.

        Bodied items draw a verdict at 0.502 against a pointer's 0.256
        and a *helpful* verdict at 0.143 against 0.023 — asking for the
        pointer half spends the ask where the caller has nothing to say.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        self._serve(
            temp_registry,
            "pack_b",
            ["doc_1", "doc_2", "doc_3"],
            ["doc_2", "doc_3"],
        )

        result = record_feedback(
            pack_id="pack_b", rating=0.5, helpful_item_ids=["doc_1"]
        )

        assert "Feedback recorded" in result

    def test_a_pack_with_no_disclosure_record_asks_for_every_item(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No graduation ran, so every served item carried a body."""
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        self._serve(temp_registry, "pack_b", ["doc_1", "doc_2"])

        with pytest.raises(McpError) as excinfo:
            record_feedback(pack_id="pack_b", rating=0.5, helpful_item_ids=["doc_1"])

        data = excinfo.value.error.data
        assert isinstance(data, dict)
        assert data["item_ids"] == ["doc_2"]

    @pytest.mark.parametrize(
        ("pack_id", "served", "pointers", "extra"),
        [
            ("pack_index", ["doc_1", "doc_2"], [], {"index_mode": True}),
            ("pack_sectioned", [], None, {"section_count": 2}),
            ("pack_broken", ["doc_1"], None, {"disclosure": "off"}),
        ],
    )
    def test_every_unknown_fails_open(
        self,
        temp_registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
        pack_id: str,
        served: list[str],
        pointers: list[str] | None,
        extra: dict[str, Any],
    ) -> None:
        """Refusing for an item the caller was never shown is the costly error.

        An index pack (all pointers), a sectioned pack (no per-item rows
        at all), and a disclosure record this build does not recognise
        each yield no bodied ids — and a gate that cannot be computed
        must not be enforced.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        monkeypatch.delenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", raising=False)
        self._serve(temp_registry, pack_id, served, pointers, **extra)

        result = record_feedback(pack_id=pack_id, rating=0.5)

        assert "Feedback recorded" in result

    def test_unknown_pack_fails_open(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        monkeypatch.delenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", raising=False)

        result = record_feedback(pack_id="pack_missing", rating=0.5)

        assert "Feedback recorded" in result

    def test_trace_level_feedback_is_never_rejected(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")

        result = record_feedback(trace_id="trace_1", rating=0.9)

        assert "Feedback recorded" in result

    def test_rejection_is_recorded_as_boundary_telemetry(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refusal has to be visible in ``trellis analyze health`` (#297)."""
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        self._serve(temp_registry, "pack_b", ["doc_1", "doc_2"], [])

        with pytest.raises(McpError):
            record_feedback(pack_id="pack_b", rating=0.5, helpful_item_ids=["doc_1"])

        (rejection,) = temp_registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=10
        )
        assert rejection.payload["tool"] == "record_feedback"

    def test_the_bodied_gate_runs_first(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With both on and nothing cited, the caller gets the useful message.

        Both gates fire on an uncited call. The bodied refusal names the
        specific ids still owed; the pack refusal only says that
        *something* must be cited. Ordering is the whole difference
        between a retry the caller can execute and one they have to
        reconstruct.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        self._serve(temp_registry, "pack_b", ["doc_1", "doc_2", "doc_3"], ["doc_3"])

        with pytest.raises(McpError) as excinfo:
            record_feedback(pack_id="pack_b", rating=0.5)

        data = excinfo.value.error.data
        assert isinstance(data, dict)
        assert data["item_ids"] == ["doc_1", "doc_2"]
        assert data["fields"] == [
            "helpful_item_ids",
            "unhelpful_item_ids",
            "ignored_item_ids",
        ]

    def test_both_gates_are_satisfiable_together(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The strictest posture must have a call that passes it.

        An ignored verdict covers the body, a helpful citation makes it
        joinable — so the two requirements compose rather than deadlock.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        monkeypatch.setenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", "1")
        self._serve(temp_registry, "pack_b", ["doc_1", "doc_2", "doc_3"], ["doc_3"])

        result = record_feedback(
            pack_id="pack_b",
            rating=0.5,
            helpful_item_ids=["doc_1"],
            ignored_item_ids=["doc_2"],
        )

        assert "Feedback recorded" in result
        (payload,) = _payloads(temp_registry)
        assert payload["ignored_item_ids"] == ["doc_2"]

    def test_a_real_pack_build_defines_what_is_asked_for(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end through the writer, not a hand-written payload.

        Every other test here encodes the bodied/pointer split in its own
        fixture, which keeps passing if ``PackBuilder`` moves the key
        (#325/#326). This one asks a real build and asserts the gate
        requests exactly the ids that build gave a body to.
        """
        monkeypatch.setenv("TRELLIS_REQUIRE_BODIED_ATTRIBUTION", "1")
        monkeypatch.delenv("TRELLIS_REQUIRE_PACK_ATTRIBUTION", raising=False)
        event_log = temp_registry.operational.event_log
        body = "the northwind loader retries on a stale manifest " * 8
        candidates = [
            PackItem(
                item_id=f"doc:{index:03d}",
                item_type="document",
                excerpt=body,
                relevance_score=1.0 - index * 0.01,
                estimated_tokens=60,
            )
            for index in range(5)
        ]
        builder = PackBuilder(
            strategies=[_FixedStrategy(candidates)],
            event_log=event_log,
            disclosure=DisclosureConfig(body_items=2),
        )
        pack = builder.build(intent="loader retries", budget=PackBudget(max_items=10))
        bodied = lookup_pack_bodied_item_ids(event_log, pack.pack_id)
        assert len(bodied) == 2

        with pytest.raises(McpError) as excinfo:
            record_feedback(pack_id=pack.pack_id, rating=0.5)

        data = excinfo.value.error.data
        assert isinstance(data, dict)
        assert data["item_ids"] == bodied
