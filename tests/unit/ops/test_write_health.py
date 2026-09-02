"""Write-boundary rejection telemetry and health aggregation tests.

The classifier cases reproduce the exact rejection shapes the 2026-08-07
recall-gap study counted in production transcripts — real pydantic errors
from real payload mistakes, not synthetic error dicts.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from trellis.ops.write_health import (
    classify_rejection,
    hints_for_trace_rejections,
    normalize_loc,
    record_write_rejection,
    resolve_trace_loc,
    summarize_backend_health,
    summarize_serve_attribution,
    summarize_write_health,
)
from trellis.schemas import trace as trace_schemas
from trellis.schemas.enums import OutcomeStatus
from trellis.schemas.trace import EvidenceRef, Outcome, Trace, TraceStep
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.sqlite.event_log import SQLiteEventLog

#: Every model name in the trace schema. A hint may name only the model
#: that actually rejected, so the production-shape tests assert an exact
#: set — naming an extra one is the misdirection #472 is about. Read off
#: the module rather than listed, so a model added later is policed too.
_MODEL_NAMES = frozenset(
    name
    for name, obj in vars(trace_schemas).items()
    if isinstance(obj, type)
    and issubclass(obj, BaseModel)
    and obj.__module__ == trace_schemas.__name__
)

_FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "production_trace_rejections.json"
    ).read_text()
)
_SHAPES: list[dict] = _FIXTURE["shapes"]


def _models_named(text: str) -> set[str]:
    """Model names appearing in a hint, matched on word boundaries.

    Substring matching would be wrong in the one direction that matters:
    ``TraceStep`` contains ``Trace``, so a correct hint would read as
    also naming the model #472 says it must stop naming.
    """
    return {n for n in _MODEL_NAMES if re.search(rf"\b{n}\b", text)}


def _validation_error(payload: dict) -> ValidationError:
    with pytest.raises(ValidationError) as excinfo:
        Trace.model_validate(payload)
    return excinfo.value


class TestClassifyRejection:
    def test_source_enum_rejection(self) -> None:
        """`source: "claude-code"` — 4 occurrences in the study."""
        error = _validation_error(
            {"source": "claude-code", "intent": "x", "context": {"domain": "d"}}
        )
        rows = classify_rejection(error)
        assert {"kind": "enum", "loc": "source"}.items() <= rows[0].items()

    def test_outcome_artifacts_extra_forbidden(self) -> None:
        """`outcome.artifacts` — the most common single rejection."""
        error = _validation_error(
            {
                "source": "agent",
                "intent": "x",
                "outcome": {"status": "success", "artifacts": ["a"]},
                "context": {"domain": "d"},
            }
        )
        rows = classify_rejection(error)
        assert any(
            row["kind"] == "extra_forbidden" and row["loc"] == "outcome.artifacts"
            for row in rows
        )

    def test_trailing_comma_json(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Trace.model_validate_json('{"source": "agent",}')
        rows = classify_rejection(excinfo.value)
        assert rows[0]["kind"] == "json_invalid"

    def test_arbitrary_exception_is_total(self) -> None:
        rows = classify_rejection(RuntimeError("boom"))
        assert rows == [{"kind": "other", "loc": "", "msg": "boom"}]

    def test_multiple_errors_produce_multiple_rows(self) -> None:
        error = _validation_error(
            {
                "source": "claude-code",
                "intent": "x",
                "outcome": {"status": "success", "status_detail": "s"},
                "context": {"domain": "d"},
            }
        )
        locs = {row["loc"] for row in classify_rejection(error)}
        assert "source" in locs
        assert "outcome.status_detail" in locs


class TestHints:
    def test_artifacts_hint_names_the_real_field(self) -> None:
        rows = [{"kind": "extra_forbidden", "loc": "outcome.artifacts", "msg": ""}]
        hints = hints_for_trace_rejections(rows)
        assert any("artifacts_produced" in hint for hint in hints)

    def test_source_hint_lists_allowed_values(self) -> None:
        rows = [{"kind": "enum", "loc": "source", "msg": ""}]
        hints = hints_for_trace_rejections(rows)
        assert any("agent" in hint and "workflow" in hint for hint in hints)

    def test_context_extra_points_to_metadata(self) -> None:
        rows = [{"kind": "extra_forbidden", "loc": "context.machine", "msg": ""}]
        hints = hints_for_trace_rejections(rows)
        assert any("metadata" in hint for hint in hints)

    def test_hints_track_the_live_schema(self) -> None:
        """Field lists come from model_fields, so they can never drift."""
        rows = [{"kind": "extra_forbidden", "loc": "outcome.bogus", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert "metrics" in hint
        assert "summary" in hint

    def test_deduplicated_and_empty_for_unhinted_kinds(self) -> None:
        rows = [
            {"kind": "enum", "loc": "source", "msg": ""},
            {"kind": "enum", "loc": "source", "msg": ""},
            {"kind": "value", "loc": "intent", "msg": ""},
            {"kind": "other", "loc": "", "msg": ""},
        ]
        assert len(hints_for_trace_rejections(rows)) == 1

    def test_enum_hint_is_derived_for_any_enum_field(self) -> None:
        """``_enum_hint`` runs on every enum, not only the hand-written ``source``.

        ``enum@source`` short-circuits to a hand-written hint, so it is
        the one enum shape in the production corpus and the generated
        enum path had no coverage at all. ``outcome.status`` is the other
        enum reachable from ``Trace``: the values come from
        ``OutcomeStatus`` at call time, and the hint names the model that
        owns the field and no other.
        """
        rows = [{"kind": "enum", "loc": "outcome.status", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert "outcome.status must be one of" in hint
        for member in OutcomeStatus:
            assert member.value in hint
        assert _models_named(hint) <= {"Outcome"}

    def test_type_hint_on_a_list_field_describes_the_element_model(self) -> None:
        """A caller who sent ``steps`` as a scalar needs the element shape.

        Naming ``Trace`` alone is true and useless — the fix is to send an
        array of ``TraceStep`` objects, so the hint has to say what one
        looks like.
        """
        rows = [{"kind": "type", "loc": "steps", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert "array of TraceStep objects" in hint
        assert "step_type" in hint
        assert "name" in hint
        assert _models_named(hint) == {"Trace", "TraceStep"}

    def test_type_hint_on_a_scalar_names_the_scalar(self) -> None:
        rows = [{"kind": "type", "loc": "intent", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert "str" in hint
        assert _models_named(hint) == {"Trace"}

    def test_list_indices_collapse_to_one_hint(self) -> None:
        """One mistake made in eight steps is one problem, not eight."""
        rows = [
            {"kind": "extra_forbidden", "loc": f"steps.{i}.action", "msg": ""}
            for i in range(8)
        ]
        (hint,) = hints_for_trace_rejections(rows)
        assert "steps[].action" in hint


class TestResolveTraceLoc:
    """The walk that replaced the ``loc``-prefix roster (#472).

    A roster over prefixes cannot describe a model nobody added a branch
    for; a walk describes every model reachable from ``Trace``. These
    pin both halves — that it resolves, and that when it *cannot* it
    resolves to nothing rather than to something plausible.
    """

    def test_walks_through_a_list_into_the_element_model(self) -> None:
        target = resolve_trace_loc("steps.3.result")
        assert target is not None
        assert target.model is TraceStep
        assert target.field == "result"
        assert target.path == "steps[].result"

    def test_stops_on_the_list_element_when_the_loc_does(self) -> None:
        target = resolve_trace_loc("evidence_used.0")
        assert target is not None
        assert target.model is EvidenceRef
        assert target.field is None
        assert target.path == "evidence_used[]"

    def test_unwraps_optional_before_descending(self) -> None:
        target = resolve_trace_loc("outcome.summary")
        assert target is not None
        assert target.model is Outcome

    def test_undeclared_terminal_segment_still_names_its_owner(self) -> None:
        target = resolve_trace_loc("steps.0.action")
        assert target is not None
        assert target.model is TraceStep
        assert target.annotation is None

    @pytest.mark.parametrize(
        "loc",
        [
            "",
            "nope.deeper",  # undeclared segment that is not the last
            "metadata.anything",  # descends into a free-form dict
            "steps.result",  # a list indexed by a name
            "intent.0",  # an index into a scalar
        ],
    )
    def test_unresolvable_paths_resolve_to_nothing(self, loc: str) -> None:
        assert resolve_trace_loc(loc) is None

    def test_unresolvable_loc_names_no_model(self) -> None:
        """The acute risk is a confident hint for the wrong model.

        Silence was the old bug, but a plausible-looking wrong answer is
        worse than silence. An unresolvable path says so and names
        nothing.
        """
        rows = [{"kind": "extra_forbidden", "loc": "metadata.a.b", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert "does not resolve to a field" in hint
        assert not _models_named(hint)


class TestHintsAgainstProductionRejections:
    """The falsifiable criterion from #472, run over the real corpus.

    ``fixtures/production_trace_rejections.json`` holds every distinct
    ``(kind, loc)`` shape from the 318 ``save_experience`` rejections in
    the reference deployment's event log — shapes only, no payload
    content. Synthetic fixtures are exactly what let the roster version
    look covered while it was silent on 184 of those 318 and named
    ``Trace`` for the 97 that rejected inside ``TraceStep``.
    """

    def test_fixture_accounts_for_every_recorded_rejection(self) -> None:
        assert sum(s["count"] for s in _SHAPES) == _FIXTURE["total_rejections"]

    @pytest.mark.parametrize("shape", _SHAPES, ids=lambda s: f"{s['kind']}@{s['loc']}")
    def test_every_production_shape_yields_a_hint(self, shape: dict) -> None:
        row = {"kind": shape["kind"], "loc": shape["loc"], "msg": ""}
        assert hints_for_trace_rejections([row]), (
            f"no hint for {shape['kind']}@{shape['loc']} "
            f"({shape['count']} production rejections)"
        )

    @pytest.mark.parametrize("shape", _SHAPES, ids=lambda s: f"{s['kind']}@{s['loc']}")
    def test_every_production_shape_names_the_right_model(self, shape: dict) -> None:
        row = {"kind": shape["kind"], "loc": shape["loc"], "msg": ""}
        joined = " ".join(hints_for_trace_rejections([row]))
        expected = set(shape["expect_models"])
        named = _models_named(joined)
        assert named == expected, (
            f"{shape['kind']}@{shape['loc']} named {sorted(named)}, "
            f"expected {sorted(expected)}"
        )

    def test_full_corpus_coverage_is_total(self) -> None:
        """Reported in the PR as a number, so a regression is visible."""
        hinted = sum(
            s["count"]
            for s in _SHAPES
            if hints_for_trace_rejections(
                [{"kind": s["kind"], "loc": s["loc"], "msg": ""}]
            )
        )
        assert hinted == _FIXTURE["total_rejections"]


class TestRecordWriteRejection:
    def test_emits_event_with_taxonomy(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        error = _validation_error(
            {"source": "claude-code", "intent": "x", "context": {"domain": "d"}}
        )
        details = record_write_rejection(
            event_log, tool="save_experience", error=error, payload_chars=42
        )
        events = event_log.get_events(event_type=EventType.WRITE_REJECTED)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["tool"] == "save_experience"
        assert payload["stage"] == "boundary"
        assert payload["payload_chars"] == 42
        assert payload["rejections"][0]["kind"] == "enum"
        assert events[0].source == "mcp:save_experience"
        assert details["rejections"][0]["loc"] == "source"

    def test_fail_soft_when_emit_raises(self) -> None:
        event_log = MagicMock(spec=EventLog)
        event_log.emit.side_effect = RuntimeError("event store down")
        details = record_write_rejection(
            event_log, tool="save_experience", error=RuntimeError("bad")
        )
        assert details["rejections"][0]["kind"] == "other"

    def test_none_event_log_still_classifies(self) -> None:
        details = record_write_rejection(
            None,
            tool="save_memory",
            rejections=[{"kind": "empty_required", "loc": "content", "msg": ""}],
        )
        assert details["rejections"][0]["kind"] == "empty_required"


def _seed(event_log: SQLiteEventLog) -> None:
    """A window with accepts, executor rejects, and boundary rejects."""
    for _ in range(6):
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            "mutation_executor",
            payload={"requested_by": "mcp:save_experience", "status": "success"},
        )
    event_log.emit(
        EventType.MUTATION_REJECTED,
        "mutation_executor",
        payload={"requested_by": "mcp:save_knowledge", "reason": "policy_violation"},
    )
    for _ in range(3):
        event_log.emit(
            EventType.WRITE_REJECTED,
            "mcp:save_experience",
            payload={
                "tool": "save_experience",
                "stage": "boundary",
                "rejections": [
                    {"kind": "extra_forbidden", "loc": "outcome.artifacts", "msg": ""}
                ],
                "hints": [],
            },
        )


class TestSummarizeWriteHealth:
    def test_counts_rates_and_repeat_collision(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed(event_log)
        report = summarize_write_health(event_log, days=1)
        assert report.accepted == 6
        assert report.boundary_rejected == 3
        assert report.executor_rejected == 1
        assert report.attempts == 10
        assert report.rejection_rate == pytest.approx(0.4)
        assert report.by_tool["mcp:save_experience"].accepted == 6
        assert report.by_tool["mcp:save_experience"].boundary_rejected == 3
        assert report.boundary_kinds == {"extra_forbidden@outcome.artifacts": 3}
        assert report.executor_reasons == {"policy_violation": 1}
        assert report.repeated_collisions[0]["count"] == 3
        assert report.status == "warn"
        assert any("collision" in reason for reason in report.reasons)
        assert any("rejection rate" in reason for reason in report.reasons)

    def test_one_mistake_across_steps_is_one_collision(self, tmp_path: Path) -> None:
        """Indices are collapsed, so the operator sees the true count.

        Un-normalized, one payload that used ``action`` in four steps
        reported four ``steps.N.action`` collisions of one each — each
        below the repeat threshold, so it warned about nothing at all.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.WRITE_REJECTED,
            "mcp:save_experience",
            payload={
                "tool": "save_experience",
                "stage": "boundary",
                "rejections": [
                    {"kind": "extra_forbidden", "loc": f"steps.{i}.action", "msg": ""}
                    for i in range(4)
                ],
                "hints": [],
            },
        )
        report = summarize_write_health(event_log, days=1)
        assert report.boundary_kinds == {"extra_forbidden@steps[].action": 4}
        assert report.repeated_collisions == [
            {"kind": "extra_forbidden", "loc": "steps[].action", "count": 4}
        ]

    def test_empty_window_is_ok(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        report = summarize_write_health(event_log, days=1)
        assert report.attempts == 0
        assert report.status == "ok"

    def test_small_sample_does_not_warn_on_rate(self, tmp_path: Path) -> None:
        """1 rejection / 2 attempts is not a 50%-bad system."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            "mutation_executor",
            payload={"requested_by": "mcp:save_memory"},
        )
        event_log.emit(
            EventType.WRITE_REJECTED,
            "mcp:save_memory",
            payload={
                "tool": "save_memory",
                "stage": "boundary",
                "rejections": [{"kind": "empty_required", "loc": "content", "msg": ""}],
            },
        )
        report = summarize_write_health(event_log, days=1)
        assert report.status == "ok"

    def test_all_writes_failing_warns_even_below_min_attempts(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.WRITE_REJECTED,
            "mcp:save_experience",
            payload={
                "tool": "save_experience",
                "stage": "boundary",
                "rejections": [{"kind": "json_invalid", "loc": "", "msg": ""}],
            },
        )
        report = summarize_write_health(event_log, days=1)
        assert report.status == "warn"
        assert any("zero accepted" in reason for reason in report.reasons)

    def test_window_excludes_old_events(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed(event_log)
        old = datetime.now(tz=UTC) - timedelta(days=30)
        # get_events honours `since`; events above were just emitted, so a
        # window that started in the future must exclude everything.
        assert (
            len(event_log.get_events(event_type=EventType.WRITE_REJECTED, since=old))
            == 3
        )
        future_report = summarize_write_health(event_log, days=0)
        assert future_report.attempts == 0


class TestServeAttribution:
    def test_coverage_rates(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={"injected_items": [{"item_id": "a"}]},
        )
        event_log.emit(EventType.PACK_ASSEMBLED, "pack_builder", payload={})
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"helpful_item_ids": ["a"]},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"rating": 1.0},
        )
        report = summarize_serve_attribution(event_log, days=1)
        assert report.packs == 2
        assert report.packs_with_injected_items == 1
        assert report.injected_coverage == pytest.approx(0.5)
        assert report.feedback_events == 2
        assert report.feedback_attributed == 1
        assert report.attribution_rate == pytest.approx(0.5)

    def test_pack_targeted_split_isolates_the_citable_population(
        self, tmp_path: Path
    ) -> None:
        """The headline rate mixes two populations; the split separates them.

        Shaped after the reference deployment: most unattributed feedback
        grades work no pack informed (unjoinable by construction), and the
        callers who *did* name a pack cite at a much higher rate. One
        number over both is evidence about neither.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        # Two callers named a pack; one of them cited.
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"pack_id": "p1", "helpful_item_ids": ["a"]},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"pack_id": "p2", "rating": 0.2},
        )
        # Two graded a trace with no pack in hand — nothing to cite.
        for _ in range(2):
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                "mcp:record_feedback",
                payload={"rating": 1.0},
            )

        report = summarize_serve_attribution(event_log, days=1)

        # Unchanged: the headline still divides by every feedback event.
        assert report.feedback_events == 4
        assert report.feedback_attributed == 1
        assert report.attribution_rate == pytest.approx(0.25)
        # New: the population where citing was possible at all.
        assert report.pack_targeted_feedback == 2
        assert report.pack_targeted_attributed == 1
        assert report.pack_attribution_rate == pytest.approx(0.5)
        assert report.untargeted_feedback == 2

    def test_all_untargeted_reports_no_citation_evidence(self, tmp_path: Path) -> None:
        """No pack-targeted feedback is "no evidence", not "nobody cited"."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"rating": 1.0},
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.pack_targeted_feedback == 0
        assert report.pack_attribution_rate == pytest.approx(0.0)
        assert report.untargeted_feedback == 1

    def test_followed_advisory_counts_as_attribution(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"pack_id": "p1", "followed_advisory_ids": ["adv_1"]},
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.pack_targeted_attributed == 1
        assert report.pack_attribution_rate == pytest.approx(1.0)


class TestBackendHealth:
    def test_unattributed_feedback_warns(self, tmp_path: Path) -> None:
        """The starvation signature: feedback exists, none attributed."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"rating": 1.0},
        )
        report = summarize_backend_health(event_log, days=1)
        assert report.status == "warn"
        assert any("attribution" in reason for reason in report.reasons)
        # The two starvation causes call for opposite fixes, so the reason
        # has to say which one it is: no pack was ever named here.
        assert any("name no pack" in reason for reason in report.reasons)

    def test_uncited_pack_feedback_names_the_ergonomic_cause(
        self, tmp_path: Path
    ) -> None:
        """Same warn, different cause — a pack was named and not cited."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"pack_id": "p1", "rating": 0.4},
        )
        report = summarize_backend_health(event_log, days=1)
        assert report.status == "warn"
        assert any("cited nothing from it" in reason for reason in report.reasons)

    def test_low_injected_coverage_warns(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={"injected_items": [{"item_id": "a"}]},
        )
        for _ in range(2):
            event_log.emit(EventType.PACK_ASSEMBLED, "pack_builder", payload={})
        report = summarize_backend_health(event_log, days=1)
        assert report.status == "warn"
        assert any("injected_items" in reason for reason in report.reasons)

    def test_quiet_system_is_ok(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        report = summarize_backend_health(event_log, days=1)
        assert report.status == "ok"
        assert report.reasons == []
        assert report.model_dump()["write"]["attempts"] == 0


class TestRetrievalAvailabilityDisclosure:
    """#365 — untargeted feedback must not be read as measured non-retrieval.

    ``write.rejected`` records a write that fails at the boundary; nothing
    records a read that never arrives. So an agent that chose not to retrieve
    and an agent whose ``get_context`` died in transport produce byte-identical
    rows, and #344's reading of ``untargeted_feedback`` as retrieve-adoption
    rests on an assumption the report cannot check.
    """

    def test_note_is_attached_when_untargeted_feedback_exists(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"rating": 0.9, "success": True},
        )
        serve = summarize_serve_attribution(event_log, days=1)
        assert serve.untargeted_feedback == 1
        assert "UNMEASURED" in serve.retrieval_availability_note
        assert "#365" in serve.retrieval_availability_note
        assert serve.retrieval_availability_measured is False

    def test_note_is_absent_when_there_is_nothing_to_over_read(
        self, tmp_path: Path
    ) -> None:
        """The disclosure varies — it is not printed unconditionally.

        A caveat that always prints is one that always gets skipped. It is
        attached only when a number exists that could be over-read.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={
                "pack_id": "pack-1",
                "rating": 0.9,
                "helpful_item_ids": ["i1"],
            },
        )
        serve = summarize_serve_attribution(event_log, days=1)
        assert serve.untargeted_feedback == 0
        assert serve.retrieval_availability_note == ""

    def test_zero_attribution_reason_no_longer_asserts_non_retrieval(
        self, tmp_path: Path
    ) -> None:
        """The pre-#365 wording stated a conclusion the data cannot support."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        for _ in range(3):
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                "mcp:record_feedback",
                payload={"rating": 0.9, "success": True},
            )
        report = summarize_backend_health(event_log, days=1)
        reason = next(r for r in report.reasons if "attribution" in r)
        assert "failing unobserved" in reason
        assert "#365" in reason


class TestCaptureCoverageIsComposedIn:
    def test_absent_capture_data_does_not_warn(self, tmp_path: Path) -> None:
        """A store with no capture worker must not warn forever.

        ``unobserved`` is a fact, not a defect of this deployment, and a
        health surface that always warns is one nobody reads.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        report = summarize_backend_health(event_log, days=1)
        assert report.capture.state == "unobserved"
        assert report.status == "ok"

    def test_degraded_capture_warns(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.CAPTURE_SWEEP_COMPLETED,
            "worker:session-capture",
            payload={
                "dry_run": False,
                "sessions_seen": 40,
                "sessions_parsed": 40,
                "sessions_triggered": 0,
                "sessions_judge_unavailable": 40,
                "sessions_with_memory": 0,
            },
        )
        report = summarize_backend_health(event_log, days=1)
        assert report.capture.state == "degraded"
        assert report.status == "warn"
        assert any("adjudicated no sessions" in r for r in report.reasons)

    def test_healthy_capture_reports_a_rate_without_warning(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.CAPTURE_SWEEP_COMPLETED,
            "worker:session-capture",
            payload={
                "dry_run": False,
                "sessions_triggered": 10,
                "sessions_with_memory": 6,
            },
        )
        report = summarize_backend_health(event_log, days=1)
        assert report.capture.capture_rate == pytest.approx(0.6)
        assert report.status == "ok"


class TestNormalizeLoc:
    @pytest.mark.parametrize(
        ("loc", "expected"),
        [
            ("steps.0.result", "steps[].result"),
            ("steps.12.args.0", "steps[].args[]"),
            ("evidence_used.7", "evidence_used[]"),
            ("outcome.artifacts", "outcome.artifacts"),
            ("policies.json", "policies.json"),
            ("", ""),
            ("0", "0"),
        ],
    )
    def test_indices_collapse(self, loc: str, expected: str) -> None:
        assert normalize_loc(loc) == expected
