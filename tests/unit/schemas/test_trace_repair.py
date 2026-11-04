"""The two trace-shape repairs: what they fix, and what they leave alone.

Every case that must stay rejected is asserted through the real ``Trace``
validation, not only through the repair's return value, because the repair is
a pre-validation step and "left unrepaired" means nothing until validation
agrees.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from trellis.schemas.trace import Trace, TraceContext
from trellis.schemas.trace_repair import (
    REPAIR_RULES,
    RULE_MOVED_TO_METADATA,
    RULE_STR_TO_SUMMARY,
    describe_repairs,
    repair_trace_payload,
    validate_trace_json,
    validate_trace_payload,
)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "agent",
        "intent": "fix the parser",
        "context": {"agent_id": "a", "domain": "bugfix"},
        "steps": [
            {"step_type": "tool_call", "name": "run_tests", "result": {"summary": "ok"}}
        ],
    }
    base.update(overrides)
    return base


def _error_locs(exc: ValidationError) -> list[tuple[Any, ...]]:
    return [tuple(e["loc"]) for e in exc.errors(include_input=False)]


class TestStringResult:
    def test_string_becomes_a_summary_object(self) -> None:
        data = _payload(
            steps=[{"step_type": "tool_call", "name": "t", "result": "ran tests"}]
        )

        trace, repairs = validate_trace_payload(data)

        assert trace.steps[0].result == {"summary": "ran tests"}
        assert repairs == [{"path": "steps[0].result", "rule": RULE_STR_TO_SUMMARY}]

    def test_every_string_step_is_repaired_with_its_own_index(self) -> None:
        steps = [
            {"step_type": "t", "name": "a", "result": "one"},
            {"step_type": "t", "name": "b", "result": {"summary": "kept"}},
            {"step_type": "t", "name": "c", "result": "three"},
        ]

        trace, repairs = validate_trace_payload(_payload(steps=steps))

        assert [s.result for s in trace.steps] == [
            {"summary": "one"},
            {"summary": "kept"},
            {"summary": "three"},
        ]
        assert [r["path"] for r in repairs] == ["steps[0].result", "steps[2].result"]

    @pytest.mark.parametrize("bad", [7, 1.5, True, ["a"], None])
    def test_a_non_string_non_object_result_stays_rejected(self, bad: object) -> None:
        data = _payload(steps=[{"step_type": "t", "name": "a", "result": bad}])

        payload, repairs = repair_trace_payload(data)

        assert repairs == []
        assert payload is data
        with pytest.raises(ValidationError) as excinfo:
            validate_trace_payload(data)
        assert ("steps", 0, "result") in _error_locs(excinfo.value)

    def test_args_are_not_repaired(self) -> None:
        data = _payload(steps=[{"step_type": "t", "name": "a", "args": "ls -la"}])

        _, repairs = repair_trace_payload(data)

        assert repairs == []
        with pytest.raises(ValidationError):
            validate_trace_payload(data)


class TestContextRepo:
    def test_repo_moves_to_metadata_and_metadata_is_created(self) -> None:
        data = _payload(context={"domain": "d", "repo": "my/repo"})

        trace, repairs = validate_trace_payload(data)

        assert trace.metadata == {"repo": "my/repo"}
        assert "repo" not in trace.context.model_dump()
        assert repairs == [{"path": "context.repo", "rule": RULE_MOVED_TO_METADATA}]

    def test_existing_metadata_is_kept(self) -> None:
        data = _payload(context={"repo": "my/repo"}, metadata={"ticket": "T-1"})

        trace, _ = validate_trace_payload(data)

        assert trace.metadata == {"ticket": "T-1", "repo": "my/repo"}

    def test_the_same_value_already_in_metadata_is_not_a_conflict(self) -> None:
        data = _payload(context={"repo": "my/repo"}, metadata={"repo": "my/repo"})

        trace, repairs = validate_trace_payload(data)

        assert trace.metadata == {"repo": "my/repo"}
        assert [r["rule"] for r in repairs] == [RULE_MOVED_TO_METADATA]

    def test_a_different_value_in_metadata_stays_rejected_and_is_not_overwritten(
        self,
    ) -> None:
        data = _payload(context={"repo": "other/repo"}, metadata={"repo": "my/repo"})

        payload, repairs = repair_trace_payload(data)

        assert repairs == []
        assert payload is data
        assert data["metadata"] == {"repo": "my/repo"}
        with pytest.raises(ValidationError) as excinfo:
            validate_trace_payload(data)
        assert _error_locs(excinfo.value) == [("context", "repo")]

    def test_a_same_looking_value_of_another_type_is_a_conflict(self) -> None:
        data = _payload(context={"repo": 1}, metadata={"repo": True})

        _, repairs = repair_trace_payload(data)

        assert repairs == []

    @pytest.mark.parametrize("metadata", [None, "text", ["a"]])
    def test_a_metadata_that_is_not_an_object_stays_rejected(
        self, metadata: object
    ) -> None:
        data = _payload(context={"repo": "my/repo"}, metadata=metadata)

        _, repairs = repair_trace_payload(data)

        assert repairs == []
        with pytest.raises(ValidationError):
            validate_trace_payload(data)

    def test_a_repo_field_that_becomes_real_is_never_moved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``TraceContext`` ever declares ``repo``, the key is valid and stays."""
        monkeypatch.setitem(TraceContext.model_fields, "repo", object())
        data = _payload(context={"repo": "my/repo"})

        payload, repairs = repair_trace_payload(data)

        assert repairs == []
        assert payload is data


class TestMixedAndUntouched:
    def test_both_repairs_in_one_payload(self) -> None:
        data = _payload(
            context={"repo": "my/repo"},
            steps=[{"step_type": "t", "name": "a", "result": "done"}],
        )

        trace, repairs = validate_trace_payload(data)

        assert trace.steps[0].result == {"summary": "done"}
        assert trace.metadata == {"repo": "my/repo"}
        assert {r["rule"] for r in repairs} == set(REPAIR_RULES)

    def test_a_repairable_and_an_unrepairable_mistake_reject_for_the_latter_only(
        self,
    ) -> None:
        data = _payload(
            context={"repo": "my/repo", "colour": "blue"},
            steps=[
                {"step_type": "t", "name": "a", "result": "done"},
                {"step_type": "t", "name": "b", "result": 5},
            ],
        )

        with pytest.raises(ValidationError) as excinfo:
            validate_trace_payload(data)

        assert sorted(_error_locs(excinfo.value)) == [
            ("context", "colour"),
            ("steps", 1, "result"),
        ]

    def test_an_unrepairable_error_keeps_the_location_it_had_without_repair(
        self,
    ) -> None:
        """The repair shifts no index, so a remaining error names the same path."""
        steps = [
            {"step_type": "t", "name": "a", "result": "done"},
            {"step_type": "t", "name": "b", "bogus": 1},
        ]
        with pytest.raises(ValidationError) as repaired:
            validate_trace_payload(_payload(steps=steps))
        with pytest.raises(ValidationError) as unrepaired:
            Trace.model_validate(_payload(steps=steps[1:]))

        assert ("steps", 1, "bogus") in _error_locs(repaired.value)
        assert ("steps", 0, "bogus") in _error_locs(unrepaired.value)

    def test_a_clean_payload_is_returned_as_the_same_object(self) -> None:
        data = _payload()

        payload, repairs = repair_trace_payload(data)

        assert payload is data
        assert repairs == []

    def test_the_input_is_never_mutated(self) -> None:
        data = _payload(
            context={"repo": "my/repo"},
            steps=[{"step_type": "t", "name": "a", "result": "done"}],
        )
        before = copy.deepcopy(data)

        repair_trace_payload(data)

        assert data == before

    @pytest.mark.parametrize(
        "data", [None, "x", 3, [], {"steps": "no"}, {"context": 1}]
    )
    def test_non_trace_shapes_are_passed_through(self, data: object) -> None:
        payload, repairs = repair_trace_payload(data)

        assert payload is data
        assert repairs == []

    def test_no_other_stray_context_key_is_moved(self) -> None:
        data = _payload(context={"branch": "main"})

        _, repairs = repair_trace_payload(data)

        assert repairs == []
        with pytest.raises(ValidationError):
            validate_trace_payload(data)

    def test_the_stored_shape_has_no_new_field(self) -> None:
        assert "repo" not in TraceContext.model_fields
        assert "repo" not in Trace.model_fields


class TestJsonPath:
    def test_json_text_is_repaired(self) -> None:
        text = json.dumps(
            _payload(
                context={"repo": "my/repo"},
                steps=[{"step_type": "t", "name": "a", "result": "done"}],
            )
        )

        trace, repairs = validate_trace_json(text)

        assert trace.metadata == {"repo": "my/repo"}
        assert trace.steps[0].result == {"summary": "done"}
        assert len(repairs) == 2

    @pytest.mark.parametrize("text", ["not json", '{"source": "agent",}', "[1]", "7"])
    def test_text_that_is_not_a_json_object_fails_as_it_always_did(
        self, text: str
    ) -> None:
        with pytest.raises(ValidationError) as repaired:
            validate_trace_json(text)
        with pytest.raises(ValidationError) as plain:
            Trace.model_validate_json(text)

        assert repaired.value.errors(include_input=False) == plain.value.errors(
            include_input=False
        )

    def test_a_payload_needing_no_repair_fails_exactly_as_before(self) -> None:
        text = json.dumps(_payload(source="robot"))

        with pytest.raises(ValidationError) as repaired:
            validate_trace_json(text)
        with pytest.raises(ValidationError) as plain:
            Trace.model_validate_json(text)

        assert repaired.value.errors(include_input=False) == plain.value.errors(
            include_input=False
        )


class TestDescribeRepairs:
    def test_nothing_repaired_is_silent(self) -> None:
        assert describe_repairs([]) == ""

    def test_names_the_fix_for_each_rule_and_collapses_indices(self) -> None:
        line = describe_repairs(
            [
                {"path": "steps[0].result", "rule": RULE_STR_TO_SUMMARY},
                {"path": "steps[4].result", "rule": RULE_STR_TO_SUMMARY},
                {"path": "context.repo", "rule": RULE_MOVED_TO_METADATA},
            ]
        )

        assert "\n" not in line
        assert line.startswith("repaired 3 fields")
        assert "steps[].result x2" in line
        assert "context.repo" in line
        assert '{"summary"' in line
        assert "top-level metadata" in line

    def test_carries_no_value(self) -> None:
        data = _payload(
            context={"repo": "secret-org/secret-repo"},
            steps=[{"step_type": "t", "name": "a", "result": "token=hunter2"}],
        )

        _, repairs = repair_trace_payload(data)

        text = json.dumps(repairs) + describe_repairs(repairs)
        assert "hunter2" not in text
        assert "secret-org" not in text
