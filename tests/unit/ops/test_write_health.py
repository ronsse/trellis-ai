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
from typing import get_args
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.ops.write_health import (
    _COLLISION_RATE_WARN,
    _MIN_ATTEMPTS_FOR_COLLISION_RATE,
    _REPEAT_COLLISION_WARN,
    RECOVERY_WINDOW_SECONDS,
    RejectionKind,
    RejectionRecoveryTracker,
    ToolWriteStats,
    classify_rejection,
    hints_for_trace_rejections,
    normalize_loc,
    record_write_recovery,
    record_write_rejection,
    resolve_trace_loc,
    summarize_backend_health,
    summarize_serve_attribution,
    summarize_write_health,
)
from trellis.schemas import trace as trace_schemas
from trellis.schemas.enums import OutcomeStatus
from trellis.schemas.trace import EvidenceRef, Outcome, Trace, TraceStep
from trellis.stores.base.event_log import Event, EventLog, EventType
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

    @pytest.mark.parametrize(
        "key",
        [
            "detail",
            "output",
            "observation",
            "action",
            "description",
            "summary",
            "outcome",
            "content",
            "text",
        ],
    )
    def test_narration_key_hint_names_the_relocation(self, key: str) -> None:
        """Every narration-family key gets the specific relocation hint.

        The generic hint ("put free-form values in steps[].args or
        steps[].result") states the shape but not which of the two, and
        not that a failure has its own home -- this is too vague to fix
        on the first retry.
        """
        rows = [{"kind": "extra_forbidden", "loc": f"steps.0.{key}", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert "steps[].result" in hint
        assert "steps[].error" in hint
        assert "steps[].args" in hint

    def test_index_key_hint_says_to_drop_it(self) -> None:
        rows = [{"kind": "extra_forbidden", "loc": "steps.0.index", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert "steps[].index" in hint
        assert "ordered by position" in hint

    def test_unrelated_unknown_key_keeps_the_generic_hint(self) -> None:
        """A step key outside the narration family is not relocated by guess.

        ``step_id`` is a real production shape (#472's corpus) that is not
        narration and not ``index`` -- it must keep naming ``TraceStep`` and
        its fields rather than being swept into either special case.
        """
        rows = [{"kind": "extra_forbidden", "loc": "steps.0.step_id", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert "TraceStep accepts only" in hint
        assert "steps[].error" not in hint

    def test_string_result_hint_contains_a_worked_example(self) -> None:
        rows = [{"kind": "type", "loc": "steps.0.result", "msg": ""}]
        (hint,) = hints_for_trace_rejections(rows)
        assert '{"summary"' in hint


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
        # Three identical rejections, but on nine attempts: under the floor
        # a collision is not rated, so only the overall rate warns.
        assert report.repeated_collisions == []
        assert report.status == "warn"
        assert not any(reason.startswith("repeated") for reason in report.reasons)
        assert any("rejection rate" in reason for reason in report.reasons)

    def test_accepted_by_operation_breaks_down_the_fan_out(
        self, tmp_path: Path
    ) -> None:
        """One ``save_experience`` call fans out to several commands.

        ``accepted`` alone cannot answer "how many traces were saved" --
        ``accepted_by_operation`` keys the same total by ``Operation`` name
        so a reader can.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            "mutation_executor",
            payload={
                "requested_by": "mcp:save_experience",
                "operation": "trace.ingest",
            },
        )
        for _ in range(2):
            event_log.emit(
                EventType.MUTATION_EXECUTED,
                "mutation_executor",
                payload={
                    "requested_by": "mcp:save_experience",
                    "operation": "entity.create",
                },
            )
        report = summarize_write_health(event_log, days=1)
        stats = report.by_tool["mcp:save_experience"]
        assert stats.accepted == 3
        assert stats.accepted_by_operation == {"trace.ingest": 1, "entity.create": 2}

    def test_accepted_by_operation_buckets_a_missing_operation(
        self, tmp_path: Path
    ) -> None:
        """A tool whose event carries no ``operation`` still gets one bucket."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            "mutation_executor",
            payload={"requested_by": "mcp:save_memory"},
        )
        report = summarize_write_health(event_log, days=1)
        stats = report.by_tool["mcp:save_memory"]
        assert stats.accepted == 1
        assert stats.accepted_by_operation == {"(unknown)": 1}

    def test_tool_write_stats_round_trips_through_json(self) -> None:
        """``accepted_by_operation`` survives a dump/validate cycle untouched.

        ``extra="forbid"`` means a field pydantic cannot round-trip raises
        on ``model_validate``, not just a silent drop -- this is the check
        that would catch that.
        """
        stats = ToolWriteStats(
            accepted=3,
            boundary_rejected=1,
            executor_rejected=0,
            accepted_by_operation={"trace.ingest": 1, "entity.create": 2},
        )
        dumped = json.loads(json.dumps(stats.model_dump(mode="json")))
        restored = ToolWriteStats.model_validate(dumped)
        assert restored == stats

    def test_one_mistake_across_steps_is_one_attempt(self, tmp_path: Path) -> None:
        """Indices are collapsed, and one rejected call is one attempt.

        ``boundary_kinds`` still counts error elements (a reader can see how
        many steps were wrong). The collision count does not: one trace that
        used ``action`` in four steps is one rejected attempt, below the
        repeat threshold however many steps it got wrong.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_accepted_traces(event_log, 30)
        _seed_step_rejections(event_log, attempts=1, elements=4)
        report = summarize_write_health(event_log, days=1)
        assert report.boundary_kinds == {"extra_forbidden@steps[].result": 4}
        assert report.boundary_rejected == 1
        assert report.repeated_collisions == []
        assert report.status == "ok"

    def test_collision_count_is_attempts_not_error_elements(
        self, tmp_path: Path
    ) -> None:
        """Three rejected calls of eight bad steps each is 3, not 24."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_accepted_traces(event_log, 30)
        _seed_step_rejections(event_log, attempts=_REPEAT_COLLISION_WARN, elements=8)
        report = summarize_write_health(event_log, days=1)
        assert report.boundary_kinds == {
            "extra_forbidden@steps[].result": 8 * _REPEAT_COLLISION_WARN
        }
        (row,) = report.repeated_collisions
        assert row["count"] == _REPEAT_COLLISION_WARN
        assert row["tool"] == "mcp:save_experience"
        assert row["tool_attempts"] == 30 + _REPEAT_COLLISION_WARN
        assert row["rate"] == round(
            _REPEAT_COLLISION_WARN / (30 + _REPEAT_COLLISION_WARN), 4
        )

    def test_two_elements_at_one_loc_in_one_call_are_one_attempt_even_at_scale(
        self, tmp_path: Path
    ) -> None:
        """Two bad-step calls of 30 elements each must not reach the threshold."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_accepted_traces(event_log, 30)
        _seed_step_rejections(
            event_log, attempts=_REPEAT_COLLISION_WARN - 1, elements=30
        )
        report = summarize_write_health(event_log, days=1)
        assert report.repeated_collisions == []

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


#: The two ``RejectionKind`` values that are conditions of a *file* rather
#: than of a payload.
_FILE_CONDITION_KINDS = frozenset({"config_unreadable", "stale_write"})

#: Every other kind, spelled out rather than derived as "the enum minus the
#: file ones". Derived, the two rosters would sum to the enum for *any* enum
#: and the partition check below would be satisfied by construction — a new
#: file condition added later would join the payload arm silently, which is
#: #485 recurring. Spelled out, adding any kind fails until someone puts it
#: in an arm on purpose.
_PAYLOAD_KINDS = frozenset(
    {
        "extra_forbidden",
        "enum",
        "missing",
        "json_invalid",
        "type",
        "value",
        "empty_required",
        "dangling_reference",
        "other",
    }
)

#: A distinct ``loc`` per kind, so a reason that hard-codes one cannot pass.
_LOC_FOR_KIND = {
    "config_unreadable": "advisories.json",
    "stale_write": "policies.json",
    "json_invalid": "",
}


def _seed_accepted_traces(
    event_log: EventLog, calls: int, *, fan_out: int = 0, tool: str = "save_experience"
) -> None:
    """``calls`` accepted trace ingests, each with ``fan_out`` extracted commands."""
    for _ in range(calls):
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            "mutation_executor",
            payload={"requested_by": f"mcp:{tool}", "operation": "trace.ingest"},
        )
        for _ in range(fan_out):
            event_log.emit(
                EventType.MUTATION_EXECUTED,
                "mutation_executor",
                payload={"requested_by": f"mcp:{tool}", "operation": "entity.create"},
            )


def _seed_step_rejections(
    event_log: EventLog,
    *,
    attempts: int,
    elements: int,
    tool: str = "save_experience",
    recovery_window_s: int | None = None,
) -> None:
    """``attempts`` rejected calls, each listing ``elements`` bad steps."""
    for _ in range(attempts):
        payload: dict[str, object] = {
            "tool": tool,
            "stage": "boundary",
            "rejections": [
                {"kind": "extra_forbidden", "loc": f"steps.{i}.result", "msg": ""}
                for i in range(elements)
            ],
            "hints": [],
        }
        if recovery_window_s is not None:
            payload["recovery_window_s"] = recovery_window_s
        event_log.emit(EventType.WRITE_REJECTED, f"mcp:{tool}", payload=payload)


def _seed_collision(
    event_log: EventLog, kind: str, loc: str, *, count: int, accepted: int = 0
) -> None:
    """``count`` boundary rejections of one ``(kind, loc)`` pair.

    ``accepted`` seeds that many accepted writes on the same surface, which a
    payload kind needs to be rated at all.
    """
    for _ in range(accepted):
        event_log.emit(
            EventType.MUTATION_EXECUTED,
            "mutation_executor",
            payload={"requested_by": "cli:curate-advisories"},
        )
    for _ in range(count):
        event_log.emit(
            EventType.WRITE_REJECTED,
            "cli:curate-advisories",
            payload={
                "tool": "cli:curate-advisories",
                "stage": "boundary",
                "rejections": [{"kind": kind, "loc": loc, "msg": ""}],
            },
        )


def _recurrence_reasons(event_log: EventLog) -> list[str]:
    """Just the ``repeated …`` lines, in report order."""
    report = summarize_write_health(event_log, days=1)
    return [r for r in report.reasons if r.startswith("repeated ")]


def _explanation(reason: str) -> str:
    """The half after the em dash — the part that does the routing."""
    _, sep, tail = reason.partition(" — ")
    assert sep, f"no explanation in {reason!r}"
    return tail


class TestRecurrenceExplanationRoutesByKind:
    """#485. ``repeated_collisions`` tells an operator which fix to reach for.

    A single sentence — *"same mistake recurring means the schema and its
    docs/skill disagree"* — was appended to every kind. That is sound for a
    payload-schema rejection (#472/#473) and false for a file condition,
    where no schema, doc or skill is implicated and no caller can act.

    Every kind is exercised, not only the two file ones: a mutant that
    returns one sentence for everything is invisible to a fixture that only
    covers the arm it happens to return.
    """

    def _reason_for(self, tmp_path: Path, kind: str) -> str:
        event_log = SQLiteEventLog(tmp_path / f"{kind}.db")
        _seed_collision(
            event_log,
            kind,
            _LOC_FOR_KIND.get(kind, f"payload.{kind}"),
            count=_REPEAT_COLLISION_WARN,
            accepted=20,
        )
        reasons = _recurrence_reasons(event_log)
        assert len(reasons) == 1, reasons
        return reasons[0]

    def test_every_kind_is_covered_by_the_partition(self) -> None:
        """Both rosters are checked against the enum, not declared.

        Every guard in this class divides the kinds into "file" and
        "payload". A superset check would pass for any enum that still
        contains the two file kinds, so a kind added later would join the
        payload arm silently and its sentence would go unexamined — the
        defect this class exists to fix, recurring one release on. Equality
        is what makes "lands in an arm or fails the suite" true: a new kind
        fails here until it is classified, and a renamed or removed one
        fails here too.
        """
        assert set(get_args(RejectionKind)) == _FILE_CONDITION_KINDS | _PAYLOAD_KINDS
        assert not (_FILE_CONDITION_KINDS & _PAYLOAD_KINDS)

    def test_every_kind_gets_a_recurrence_reason(self, tmp_path: Path) -> None:
        """No kind may fall through to no line at all."""
        for kind in get_args(RejectionKind):
            reason = self._reason_for(tmp_path, kind)
            assert kind in reason, reason
            assert _explanation(reason), reason

    def test_the_shared_format_still_names_kind_loc_and_count(
        self, tmp_path: Path
    ) -> None:
        """Count is read off the row, not off the warn threshold.

        Seeded one above ``_REPEAT_COLLISION_WARN`` so a reason that
        rendered the constant instead of the count cannot pass.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_collision(
            event_log,
            "extra_forbidden",
            "outcome.artifacts",
            count=_REPEAT_COLLISION_WARN + 1,
            accepted=20,
        )
        (reason,) = _recurrence_reasons(event_log)
        assert "extra_forbidden" in reason
        assert "at outcome.artifacts" in reason
        assert f"x{_REPEAT_COLLISION_WARN + 1}" in reason

    def test_an_empty_loc_still_reads_as_the_payload(self, tmp_path: Path) -> None:
        """Unchanged by #485, pinned because the f-string moved."""
        reason = self._reason_for(tmp_path, "json_invalid")
        assert "at (payload)" in reason

    def test_payload_kinds_name_possible_causes_and_assert_none(
        self, tmp_path: Path
    ) -> None:
        """A rejection row cannot say the schema and its docs disagree.

        The same count comes from a schema narrower than its docs, docs that
        misdescribe it, an unhelpful hint and a caller's own bad template.
        The reason names those and says it cannot tell which.
        """
        for kind in sorted(_PAYLOAD_KINDS):
            reason = self._reason_for(tmp_path, kind)
            assert reason.startswith("repeated rejection:"), reason
            explanation = _explanation(reason)
            assert "cause not established" in explanation, reason
            for possibility in ("schema", "docs or skill", "hint", "callers"):
                assert possibility in explanation, (possibility, reason)
            for assertion in ("disagree", "means", "because"):
                assert assertion not in reason, (assertion, reason)

    def test_a_payload_reason_carries_the_tool_and_its_rate(
        self, tmp_path: Path
    ) -> None:
        reason = self._reason_for(tmp_path, "extra_forbidden")
        assert " on cli:curate-advisories (13% of 23 attempts) — " in reason, reason

    def test_file_conditions_never_blame_a_schema_doc_or_skill(
        self, tmp_path: Path
    ) -> None:
        """Both halves of the old line were wrong, prefix included."""
        for kind in sorted(_FILE_CONDITION_KINDS):
            reason = self._reason_for(tmp_path, kind)
            lowered = reason.lower()
            for word in ("schema", "doc", "skill", "mistake"):
                assert word not in lowered, (kind, word, reason)

    def test_the_two_file_conditions_do_not_share_a_sentence(
        self, tmp_path: Path
    ) -> None:
        """One needs a human with ``mv``; the other needs two writers apart.

        Folding them into a single "this is a file problem" line would fix
        the wrong-routing complaint while still routing two different
        repairs to one answer.
        """
        explanations = {
            kind: _explanation(self._reason_for(tmp_path, kind))
            for kind in sorted(_FILE_CONDITION_KINDS)
        }
        assert len(set(explanations.values())) == len(_FILE_CONDITION_KINDS)

    def test_config_unreadable_points_at_the_damaged_file(self, tmp_path: Path) -> None:
        reason = self._reason_for(tmp_path, "config_unreadable")
        assert reason.startswith("repeated unreadable config:")
        assert "advisories.json" in reason
        explanation = _explanation(reason).lower()
        assert "file" in explanation
        # The repair is a human's, and saying so is the whole routing.
        assert "unrepaired" in explanation

    def test_stale_write_points_at_the_two_writers(self, tmp_path: Path) -> None:
        """The kind the counter reads *best*, and it was labelled worst.

        ``RejectionKind``'s own comment says a stale write needs nobody at
        all unless it recurs — and recurrence is exactly when this line
        fires, so this is the one row carrying real signal.
        """
        reason = self._reason_for(tmp_path, "stale_write")
        assert reason.startswith("repeated writer collision:")
        explanation = _explanation(reason).lower()
        assert "writers" in explanation
        assert "safe" in explanation

    def test_two_kinds_at_one_loc_get_their_own_lines(self, tmp_path: Path) -> None:
        """The live shape from #485's reproduction: one file, both kinds."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        for kind in ("config_unreadable", "stale_write"):
            _seed_collision(
                event_log, kind, "advisories.json", count=_REPEAT_COLLISION_WARN
            )
        reasons = _recurrence_reasons(event_log)
        assert len(reasons) == 2
        assert all("advisories.json" in r for r in reasons)
        assert len({_explanation(r) for r in reasons}) == 2


class TestCollisionIsRatedAgainstItsTool:
    """The threshold has a denominator, and a floor below which it is silent.

    An absolute count of three crosses on volume alone: a busy, healthy tool
    sits at ``warn`` for good. These pin the rating, the floor, the strict
    inequality at the threshold, per-tool separation and the fan-out
    denominator, each against a seed that a count-only rule would fail.
    """

    def _repeated(self, event_log: EventLog) -> list[dict[str, object]]:
        return summarize_write_health(event_log, days=1).repeated_collisions

    def test_a_busy_healthy_tool_does_not_warn_on_volume(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_accepted_traces(event_log, 400)
        _seed_step_rejections(event_log, attempts=_REPEAT_COLLISION_WARN, elements=1)
        report = summarize_write_health(event_log, days=1)
        assert report.repeated_collisions == []
        assert report.status == "ok"

    def test_below_the_attempt_floor_it_does_not_warn(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        accepted = _MIN_ATTEMPTS_FOR_COLLISION_RATE - _REPEAT_COLLISION_WARN - 1
        _seed_accepted_traces(event_log, accepted)
        _seed_step_rejections(event_log, attempts=_REPEAT_COLLISION_WARN, elements=1)
        # One attempt short of the floor, at a rate far above the threshold.
        assert not self._repeated(event_log)

    def test_at_the_floor_a_high_rate_warns(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        accepted = _MIN_ATTEMPTS_FOR_COLLISION_RATE - _REPEAT_COLLISION_WARN
        _seed_accepted_traces(event_log, accepted)
        _seed_step_rejections(event_log, attempts=_REPEAT_COLLISION_WARN, elements=1)
        (row,) = self._repeated(event_log)
        assert row["tool_attempts"] == _MIN_ATTEMPTS_FOR_COLLISION_RATE

    def test_the_rate_threshold_is_strict(self, tmp_path: Path) -> None:
        """Exactly at the threshold is quiet; one attempt fewer is not."""
        at_threshold = round(_REPEAT_COLLISION_WARN / _COLLISION_RATE_WARN)
        for attempts, warns in ((at_threshold, False), (at_threshold - 1, True)):
            event_log = SQLiteEventLog(tmp_path / f"events-{attempts}.db")
            _seed_accepted_traces(event_log, attempts - _REPEAT_COLLISION_WARN)
            _seed_step_rejections(
                event_log, attempts=_REPEAT_COLLISION_WARN, elements=1
            )
            assert bool(self._repeated(event_log)) is warns, attempts

    def test_fewer_than_the_repeat_count_never_warns_at_any_rate(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_accepted_traces(event_log, 20)
        _seed_step_rejections(
            event_log, attempts=_REPEAT_COLLISION_WARN - 1, elements=1
        )
        assert not self._repeated(event_log)

    def test_a_fan_out_does_not_dilute_the_denominator(self, tmp_path: Path) -> None:
        """One accepted trace is one attempt, however many commands it spawned.

        Dividing by every executed command would read 3 of 115 here and
        stay silent; the call-level count is 3 of 15.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_accepted_traces(event_log, 12, fan_out=8)
        _seed_step_rejections(event_log, attempts=_REPEAT_COLLISION_WARN, elements=1)
        (row,) = self._repeated(event_log)
        assert row["tool_attempts"] == 12 + _REPEAT_COLLISION_WARN

    def test_each_tool_is_rated_on_its_own_attempts(self, tmp_path: Path) -> None:
        """The same ``(kind, loc)`` on a busy tool does not hide a quiet one."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_accepted_traces(event_log, 400)
        _seed_step_rejections(event_log, attempts=3, elements=1)
        _seed_accepted_traces(event_log, 12, tool="save_knowledge")
        for _ in range(3):
            event_log.emit(
                EventType.WRITE_REJECTED,
                "mcp:save_knowledge",
                payload={
                    "tool": "save_knowledge",
                    "stage": "boundary",
                    "rejections": [
                        {"kind": "extra_forbidden", "loc": "steps.0.result", "msg": ""}
                    ],
                },
            )
        (row,) = self._repeated(event_log)
        assert row["tool"] == "mcp:save_knowledge"

    def test_a_surface_with_no_accepted_side_is_not_rated(self, tmp_path: Path) -> None:
        """``record_feedback`` bypasses the executor: its rate would read 100%."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_step_rejections(
            event_log,
            attempts=_MIN_ATTEMPTS_FOR_COLLISION_RATE * 2,
            elements=1,
            tool="record_feedback",
        )
        report = summarize_write_health(event_log, days=1)
        assert report.repeated_collisions == []
        # Still visible where it was before.
        assert report.by_tool["mcp:record_feedback"].boundary_rejected == (
            _MIN_ATTEMPTS_FOR_COLLISION_RATE * 2
        )

    def test_a_file_condition_is_warned_on_attempts_alone(self, tmp_path: Path) -> None:
        """Recurrence is its whole signal, and its surface has no accepted side."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_collision(
            event_log, "stale_write", "policies.json", count=_REPEAT_COLLISION_WARN
        )
        (row,) = self._repeated(event_log)
        assert (row["count"], row["rate"], row["tool_attempts"]) == (
            _REPEAT_COLLISION_WARN,
            None,
            None,
        )


def _seed_recovery(
    event_log: EventLog, *, tool: str = "save_experience", attempts: int
) -> None:
    event_log.emit(
        EventType.WRITE_RECOVERED,
        f"mcp:{tool}",
        payload={
            "tool": tool,
            "rejected_attempts": attempts,
            "window_s": RECOVERY_WINDOW_SECONDS,
        },
    )


class TestRejectionRecoveryTracker:
    """In-memory pairing of a rejection with the acceptance that follows."""

    @staticmethod
    def _tracker() -> tuple[RejectionRecoveryTracker, list[float]]:
        now = [1000.0]
        return RejectionRecoveryTracker(clock=lambda: now[0]), now

    def test_an_acceptance_inside_the_window_recovers_the_rejection(self) -> None:
        tracker, now = self._tracker()
        tracker.note_rejection("mcp:save_experience")
        now[0] += RECOVERY_WINDOW_SECONDS
        assert tracker.claim_recovery("mcp:save_experience") == 1

    def test_one_second_past_the_window_recovers_nothing(self) -> None:
        tracker, now = self._tracker()
        tracker.note_rejection("mcp:save_experience")
        now[0] += RECOVERY_WINDOW_SECONDS + 1
        assert tracker.claim_recovery("mcp:save_experience") == 0

    def test_the_window_runs_from_the_latest_rejection(self) -> None:
        """A caller that keeps failing is still inside one retry loop."""
        tracker, now = self._tracker()
        tracker.note_rejection("mcp:save_experience")
        now[0] += RECOVERY_WINDOW_SECONDS - 1
        tracker.note_rejection("mcp:save_experience")
        now[0] += RECOVERY_WINDOW_SECONDS - 1
        assert tracker.claim_recovery("mcp:save_experience") == 2

    def test_an_acceptance_consumes_what_it_recovers(self) -> None:
        tracker, _ = self._tracker()
        tracker.note_rejection("mcp:save_experience")
        assert tracker.claim_recovery("mcp:save_experience") == 1
        assert tracker.claim_recovery("mcp:save_experience") == 0

    def test_another_tools_acceptance_recovers_nothing(self) -> None:
        tracker, _ = self._tracker()
        tracker.note_rejection("mcp:save_experience")
        assert tracker.claim_recovery("mcp:save_memory") == 0
        assert tracker.claim_recovery("mcp:save_experience") == 1

    def test_an_acceptance_with_no_rejection_recovers_nothing(self) -> None:
        tracker, _ = self._tracker()
        assert tracker.claim_recovery("mcp:save_experience") == 0

    def test_it_holds_a_tool_a_count_and_a_time_and_nothing_else(self) -> None:
        tracker, _ = self._tracker()
        tracker.note_rejection("mcp:save_experience")
        ((label, (count, at)),) = tracker._pending.items()
        assert (label, count, at) == ("mcp:save_experience", 1, 1000.0)


class TestRecoveryEvents:
    def test_a_tracked_rejection_says_so_and_is_noted(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        tracker = RejectionRecoveryTracker()
        record_write_rejection(
            event_log,
            tool="save_experience",
            rejections=[{"kind": "missing", "loc": "intent", "msg": ""}],
            recovery=tracker,
        )
        (event,) = event_log.get_events(event_type=EventType.WRITE_REJECTED, limit=5)
        assert event.payload["recovery_window_s"] == RECOVERY_WINDOW_SECONDS
        assert tracker.claim_recovery("mcp:save_experience") == 1

    def test_without_a_tracker_the_event_keeps_its_old_shape(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        record_write_rejection(event_log, tool="save_experience", error=ValueError("x"))
        (event,) = event_log.get_events(event_type=EventType.WRITE_REJECTED, limit=5)
        assert "recovery_window_s" not in event.payload

    def test_a_rejection_that_never_reached_the_log_is_not_noted(self) -> None:
        """Recovered must not outnumber tracked."""
        event_log = MagicMock(spec=EventLog)
        event_log.emit.side_effect = RuntimeError("event store down")
        tracker = RejectionRecoveryTracker()
        record_write_rejection(
            event_log, tool="save_experience", error=ValueError("x"), recovery=tracker
        )
        assert tracker.claim_recovery("mcp:save_experience") == 0

    def test_a_recovery_event_carries_a_tool_and_two_integers(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        record_write_recovery(
            event_log, tool="save_experience", rejected_attempts=2, window_seconds=120
        )
        (event,) = event_log.get_events(event_type=EventType.WRITE_RECOVERED, limit=5)
        assert event.source == "mcp:save_experience"
        assert event.payload == {
            "tool": "save_experience",
            "rejected_attempts": 2,
            "window_s": 120,
        }

    def test_a_failing_log_does_not_raise_out_of_a_recovery(self) -> None:
        event_log = MagicMock(spec=EventLog)
        event_log.emit.side_effect = RuntimeError("event store down")
        record_write_recovery(
            event_log, tool="save_experience", rejected_attempts=1, window_seconds=120
        )
        record_write_recovery(
            None, tool="save_experience", rejected_attempts=1, window_seconds=120
        )


class TestRecoveryReport:
    def _summarize(self, event_log: EventLog):
        return summarize_write_health(event_log, days=1).recovery

    def test_a_window_of_old_rows_is_not_measured_rather_than_zero(
        self, tmp_path: Path
    ) -> None:
        """Rows written before tracking lack ``recovery_window_s``."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_step_rejections(event_log, attempts=4, elements=1)
        recovery = self._summarize(event_log)
        assert recovery.recovered_share is None
        assert recovery.tracked_rejections == 0
        assert recovery.note == ""

    def test_the_share_is_recovered_over_tracked(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_step_rejections(
            event_log, attempts=4, elements=1, recovery_window_s=RECOVERY_WINDOW_SECONDS
        )
        _seed_recovery(event_log, attempts=3)
        recovery = self._summarize(event_log)
        assert (recovery.tracked_rejections, recovery.recovered_rejections) == (4, 3)
        assert recovery.recovered_share == 0.75
        assert recovery.window_seconds == RECOVERY_WINDOW_SECONDS
        assert recovery.by_tool["mcp:save_experience"].share == 0.75

    def test_tracking_with_nothing_recovered_reads_zero_not_none(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_step_rejections(
            event_log, attempts=2, elements=1, recovery_window_s=RECOVERY_WINDOW_SECONDS
        )
        assert self._summarize(event_log).recovered_share == 0.0

    def test_old_rows_stay_out_of_the_denominator(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_step_rejections(event_log, attempts=6, elements=1)
        _seed_step_rejections(
            event_log, attempts=2, elements=1, recovery_window_s=RECOVERY_WINDOW_SECONDS
        )
        _seed_recovery(event_log, attempts=1)
        recovery = self._summarize(event_log)
        assert recovery.tracked_rejections == 2
        assert recovery.recovered_share == 0.5

    def test_the_share_never_passes_one(self, tmp_path: Path) -> None:
        """A rejection just before the window still has its recovery inside it."""
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_step_rejections(
            event_log, attempts=1, elements=1, recovery_window_s=RECOVERY_WINDOW_SECONDS
        )
        _seed_recovery(event_log, attempts=5)
        assert self._summarize(event_log).recovered_share == 1.0

    def test_recoveries_with_nothing_tracked_report_no_share(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_recovery(event_log, attempts=2)
        assert self._summarize(event_log).recovered_share is None

    def test_the_approximation_is_stated_beside_the_number(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_step_rejections(
            event_log, attempts=1, elements=1, recovery_window_s=RECOVERY_WINDOW_SECONDS
        )
        note = self._summarize(event_log).note
        assert note.startswith("approximate:")
        assert f"{RECOVERY_WINDOW_SECONDS}s" in note
        assert "no per-caller token" in note

    def test_a_malformed_recovery_event_is_skipped_not_fatal(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_step_rejections(
            event_log, attempts=1, elements=1, recovery_window_s=RECOVERY_WINDOW_SECONDS
        )
        for bad in ("two", True, -1, None):
            event_log.emit(
                EventType.WRITE_RECOVERED,
                "mcp:save_experience",
                payload={"tool": "save_experience", "rejected_attempts": bad},
            )
        assert self._summarize(event_log).recovered_share == 0.0

    def test_recovery_never_changes_the_status(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        _seed_accepted_traces(event_log, 50)
        _seed_step_rejections(
            event_log, attempts=1, elements=1, recovery_window_s=RECOVERY_WINDOW_SECONDS
        )
        assert summarize_write_health(event_log, days=1).status == "ok"


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


class TestFailedStrategies:
    """``PACK_ASSEMBLED.strategy_failures`` tallied off the attribution scan."""

    def test_failures_tallied_per_strategy_with_withheld_packs(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        now = datetime.now(tz=UTC).replace(microsecond=0)
        withheld = {
            HOLDOUT_KEY: True,
            "strategy_failures": [{"strategy": "semantic"}, {"strategy": "graph"}],
        }
        served = {"strategy_failures": [{"strategy": "semantic"}]}
        for hours_ago, payload in ((3, withheld), (2, {}), (1, served)):
            event_log.append(
                Event(
                    event_type=EventType.PACK_ASSEMBLED,
                    source="pack_builder",
                    occurred_at=now - timedelta(hours=hours_ago),
                    payload=payload,
                )
            )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.failed_strategies.packs == 2
        assert report.failed_strategies.by_strategy == {"semantic": 2, "graph": 1}
        assert report.failed_strategies.latest_at == now - timedelta(hours=1)

    @pytest.mark.parametrize(
        ("value", "packs", "by_strategy"),
        [
            ([], 1, {"semantic": 1}),
            (None, 1, {"semantic": 1}),
            ("semantic", 1, {"semantic": 1}),
            ({"strategy": "graph"}, 1, {"semantic": 1}),
            ([None, "graph"], 2, {"semantic": 1, "unknown": 2}),
        ],
    )
    def test_an_empty_or_malformed_entry_never_fails_the_read(
        self,
        tmp_path: Path,
        value: object,
        packs: int,
        by_strategy: dict[str, int],
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={"strategy_failures": value},
        )
        event_log.emit(EventType.PACK_ASSEMBLED, "pack_builder", payload={})
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={"strategy_failures": [{"strategy": "semantic"}]},
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.failed_strategies.packs == packs
        assert report.failed_strategies.by_strategy == by_strategy

    def test_failure_outside_window_is_not_counted(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.append(
            Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="pack_builder",
                occurred_at=datetime.now(tz=UTC) - timedelta(days=10),
                payload={"strategy_failures": [{"strategy": "semantic"}]},
            )
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.failed_strategies.packs == 0
        assert report.failed_strategies.by_strategy == {}
        assert report.failed_strategies.latest_at is None


class TestMalformedRows:
    """``PACK_ASSEMBLED.malformed_row_skips`` tallied off the attribution scan."""

    def test_skips_tallied_per_strategy_with_withheld_packs(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        now = datetime.now(tz=UTC).replace(microsecond=0)
        withheld = {HOLDOUT_KEY: True, "malformed_row_skips": {"semantic": 2}}
        served = {"malformed_row_skips": {"semantic": 1, "graph": 3}}
        for hours_ago, payload in ((3, withheld), (2, {}), (1, served)):
            event_log.append(
                Event(
                    event_type=EventType.PACK_ASSEMBLED,
                    source="pack_builder",
                    occurred_at=now - timedelta(hours=hours_ago),
                    payload=payload,
                )
            )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.malformed_rows.packs == 2
        assert report.malformed_rows.by_strategy == {"semantic": 3, "graph": 3}
        assert report.malformed_rows.latest_at == now - timedelta(hours=1)

    @pytest.mark.parametrize(
        "value",
        [{}, None, [], "semantic", 3, {"semantic": 0}, {"semantic": -1}],
    )
    def test_no_key_or_an_empty_one_counts_nothing(
        self, tmp_path: Path, value: object
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={"malformed_row_skips": value},
        )
        event_log.emit(EventType.PACK_ASSEMBLED, "pack_builder", payload={})

        report = summarize_serve_attribution(event_log, days=1)

        assert report.malformed_rows.packs == 0
        assert report.malformed_rows.by_strategy == {}
        assert report.malformed_rows.latest_at is None

    def test_a_malformed_entry_is_dropped_and_the_rest_counts(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={
                "malformed_row_skips": {
                    "semantic": 2,
                    "graph": True,
                    "keyword": "many",
                    "other": 1.5,
                }
            },
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.malformed_rows.packs == 1
        assert report.malformed_rows.by_strategy == {"semantic": 2}

    def test_a_skip_outside_the_window_is_not_counted(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.append(
            Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="pack_builder",
                occurred_at=datetime.now(tz=UTC) - timedelta(days=10),
                payload={"malformed_row_skips": {"semantic": 1}},
            )
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.malformed_rows.packs == 0
        assert report.malformed_rows.latest_at is None

    def test_a_skipping_pack_warns_by_name_and_count(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={"malformed_row_skips": {"semantic": 2}},
        )

        report = summarize_backend_health(event_log, days=1)

        assert report.status == "warn"
        assert any(
            "skipped malformed retrieval rows (semantic: 2)" in reason
            for reason in report.reasons
        )
        assert report.model_dump(mode="json")["serve"]["malformed_rows"] == {
            "packs": 1,
            "by_strategy": {"semantic": 2},
            "latest_at": report.serve.malformed_rows.latest_at.isoformat().replace(
                "+00:00", "Z"
            ),
        }

    def test_packs_with_no_skip_add_no_reason(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={
                "injected_items": [{"item_id": "a"}],
                "malformed_row_skips": {},
            },
        )
        event_log.emit(EventType.PACK_ASSEMBLED, "pack_builder", payload={})

        report = summarize_backend_health(event_log, days=1)

        assert report.status == "ok"
        assert report.reasons == []

    def test_a_malformed_entry_never_fails_health(self, tmp_path: Path) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={"malformed_row_skips": ["not", "a", "dict"]},
        )

        report = summarize_backend_health(event_log, days=1)

        assert not any("malformed" in reason for reason in report.reasons)


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

    def test_failed_strategy_pack_warns_without_exception_text(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={
                "strategy_failures": [
                    {
                        "strategy": "semantic",
                        "error_class": "RuntimeError",
                        "message": "secret/path/or.dsn=synthetic-never-real",
                    }
                ]
            },
        )

        report = summarize_backend_health(event_log, days=1)

        assert report.status == "warn"
        assert any("semantic" in reason for reason in report.reasons)
        assert any("failed retrieval strategy" in reason for reason in report.reasons)
        dumped = json.dumps(report.model_dump(mode="json"))
        assert "secret/path/or.dsn" not in dumped
        assert "RuntimeError" not in dumped
        for reason in report.reasons:
            assert "secret/path/or.dsn" not in reason
            assert "RuntimeError" not in reason

    def test_only_clean_packs_add_no_failed_strategy_reason(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            payload={
                "injected_items": [{"item_id": "a"}],
                "strategy_failures": [],
            },
        )
        event_log.emit(EventType.PACK_ASSEMBLED, "pack_builder", payload={})

        report = summarize_backend_health(event_log, days=1)

        assert report.status == "ok"
        assert report.reasons == []


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


class TestStrayCitationsOnTheHealthSurface:
    """Citing is not joining, and the gap between them is #574.

    ``pack_attribution_rate`` answers "did the caller name any item?". It
    is satisfied by a citation the learning join then drops, so on the
    reference deployment it reads 69/74 while 14 of the 699 ids those
    callers named reached nothing. The stray block sits beside it and
    measures the second question.
    """

    @staticmethod
    def _emit_pack(event_log: EventLog, *, pack_id: str, served: list[str]) -> None:
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            entity_id=pack_id,
            entity_type="pack",
            payload={
                "injected_item_ids": served,
                "injected_items": [{"item_id": i} for i in served],
            },
        )

    def test_stray_block_is_computed_beside_the_citation_rate(
        self, tmp_path: Path
    ) -> None:
        event_log = SQLiteEventLog(tmp_path / "events.db")
        self._emit_pack(event_log, pack_id="p1", served=["trace:a", "doc:b"])
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={
                "pack_id": "p1",
                "helpful_item_ids": ["trace:a"],
                "unhelpful_item_ids": ["entity:doc:b", "ghost"],
            },
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.cited_ids == 3
        assert report.stray_cited_ids == 2
        assert report.stray_citation_rate == pytest.approx(round(2 / 3, 4))
        assert report.packs_with_stray_citations == 1
        assert report.stray_citations_by_namespace == {"entity": 1, "(none)": 1}
        assert report.stray_citations_by_shape == {"prefix_added": 1, "foreign": 1}

    def test_pack_attribution_rate_cannot_see_a_stray(self, tmp_path: Path) -> None:
        """Why a new field was needed rather than a stricter old one.

        Two deployments, identical on every pre-existing field: one whose
        caller cited what it was served, one whose caller cited nothing the
        pack carried. ``pack_attribution_rate`` is 1.0 for both — the loss
        is invisible to it by construction, so tightening it would have
        redefined a metric DoD-3 reads rather than measured a new fact.
        """
        reports = []
        for name, cited in (("clean", ["trace:a"]), ("all-stray", ["ghost"])):
            event_log = SQLiteEventLog(tmp_path / f"{name}.db")
            self._emit_pack(event_log, pack_id="p1", served=["trace:a"])
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                "mcp:record_feedback",
                payload={"pack_id": "p1", "helpful_item_ids": cited},
            )
            reports.append(summarize_serve_attribution(event_log, days=1))

        clean, all_stray = reports
        for report in reports:
            assert report.pack_targeted_feedback == 1
            assert report.pack_targeted_attributed == 1
            assert report.pack_attribution_rate == pytest.approx(1.0)
            assert report.attribution_rate == pytest.approx(1.0)
        # Only the new block separates them.
        assert clean.stray_cited_ids == 0
        assert clean.stray_citation_rate == pytest.approx(0.0)
        assert all_stray.stray_cited_ids == 1
        assert all_stray.stray_citation_rate == pytest.approx(1.0)

    def test_a_pack_outside_the_window_is_skipped_whole(self, tmp_path: Path) -> None:
        """An unresolvable pack is missing evidence, not a stray.

        The feedback scan and the pack scan have the same window but not
        the same population — a pack assembled before it, or lost to the
        scan limit, resolves to nothing. Counting its citations would make
        the stray rate rise as *coverage* falls, which is a metric wired to
        the wrong quantity.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"pack_id": "gone", "helpful_item_ids": ["trace:a", "trace:b"]},
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.cited_ids == 0
        assert report.stray_cited_ids == 0
        assert report.stray_citation_rate == pytest.approx(0.0)
        assert report.packs_with_stray_citations == 0
        # The pre-existing surface still counts it, correctly: the caller
        # did name a pack and did cite items.
        assert report.pack_targeted_feedback == 1
        assert report.pack_attribution_rate == pytest.approx(1.0)

    def test_sectioned_pack_is_not_reported_as_a_total_loss(
        self, tmp_path: Path
    ) -> None:
        """``build_sectioned`` writes neither served-item key.

        Its citations resolve to an empty served set, which means "no
        record of what was served" and never "served nothing" — the
        difference between one unmeasurable pack and a 100% stray rate on a
        surface that is working.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            entity_id="p-sectioned",
            entity_type="sectioned_pack",
            payload={"sections": 2},
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"pack_id": "p-sectioned", "helpful_item_ids": ["trace:a"]},
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.cited_ids == 0
        assert report.stray_cited_ids == 0
        assert report.stray_citation_rate == pytest.approx(0.0)

    @pytest.mark.parametrize(
        ("payload", "shape"),
        [
            ({"injected_item_ids": ["trace:a"]}, "ids-only"),
            ({"injected_items": [{"item_id": "trace:a"}]}, "rows-only"),
            (
                {
                    "injected_item_ids": ["trace:a"],
                    "injected_items": [{"item_id": "trace:a"}],
                },
                "both",
            ),
        ],
    )
    def test_both_served_item_keys_are_honoured(
        self, tmp_path: Path, payload: dict, shape: str
    ) -> None:
        """The surface reads served membership through the canonical reader.

        Production writes both keys and they have never disagreed — 219 of
        219 flat packs, measured 2026-09-16 — so a surface reading only one
        of them is correct today and silently wrong the day a writer stops
        filling it. What is pinned is that the choice is not made here.
        """
        event_log = SQLiteEventLog(tmp_path / f"{shape}.db")
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            "pack_builder",
            entity_id="p1",
            entity_type="pack",
            payload=payload,
        )
        event_log.emit(
            EventType.FEEDBACK_RECORDED,
            "mcp:record_feedback",
            payload={"pack_id": "p1", "helpful_item_ids": ["trace:a", "ghost"]},
        )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.cited_ids == 2
        assert report.stray_cited_ids == 1
        assert report.stray_citations_by_shape == {"foreign": 1}

    def test_the_unit_is_per_citation_event_not_per_distinct_id(
        self, tmp_path: Path
    ) -> None:
        """Two graders naming the same bad id is two lost verdicts.

        ``PackValueReport.cited_ids_not_served`` counts per (pack, distinct
        id) and this counts per (event, id); they agree at 14 on the
        reference deployment only because its one double-graded pack
        carries no stray. Pinning the unit here stops that coincidence from
        being read as a shared definition.
        """
        event_log = SQLiteEventLog(tmp_path / "events.db")
        self._emit_pack(event_log, pack_id="p1", served=["trace:a"])
        for _ in range(2):
            event_log.emit(
                EventType.FEEDBACK_RECORDED,
                "mcp:record_feedback",
                payload={"pack_id": "p1", "helpful_item_ids": ["ghost"]},
            )

        report = summarize_serve_attribution(event_log, days=1)

        assert report.cited_ids == 2
        assert report.stray_cited_ids == 2
        assert report.stray_citations_by_shape == {"foreign": 2}
        # One pack, graded twice.
        assert report.packs_with_stray_citations == 1
