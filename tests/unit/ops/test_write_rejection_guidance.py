"""What an agent is told when a write is wrong, checked by following it.

Two guidance surfaces, both derived from live definitions rather than a
roster:

* ``hints_for_trace_rejections``: a field rejected on one trace model that
  another declares is told where it belongs. The tests build the retry a
  caller would build from the hint and validate it, because a hint that reads
  well and lands the value nowhere is the failure being guarded.
* ``describe_argument_rejection``: a tool call whose arguments were wrong is
  described from the tool's own schema, naming what it accepts and echoing no
  value the caller sent.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from pydantic import ValidationError, validate_call

from trellis.ops.write_health import (
    classify_rejection,
    describe_argument_rejection,
    hints_for_trace_rejections,
)
from trellis.schemas.enums import OutcomeStatus
from trellis.schemas.trace import Trace


def _base() -> dict[str, Any]:
    return {"source": "agent", "intent": "Fix a failing test", "context": {}}


#: Rejected name -> a value of the right type for the field it belongs in,
#: and the dotted path of that field on a validated ``Trace``.
_MISPLACED: dict[str, tuple[Any, str]] = {
    "domain": ("bugfix", "context.domain"),
    "agent_id": ("coder-1", "context.agent_id"),
    "summary": ("all green", "outcome.summary"),
    "status": (OutcomeStatus.FAILURE.value, "outcome.status"),
    "artifacts": (
        [{"artifact_id": "a1", "artifact_type": "file"}],
        "artifacts_produced",
    ),
    "started_at": ("2026-08-27T00:00:00Z", "context.started_at"),
}


def _rejected(payload: dict[str, Any]) -> list[dict[str, str]]:
    with pytest.raises(ValidationError) as excinfo:
        Trace.model_validate_json(json.dumps(payload))
    return classify_rejection(excinfo.value)


def _destination(hint: str) -> str:
    """Read the field a hint sends the caller to, the way a caller would."""
    relocated = re.search(r"belongs at ([A-Za-z_.]+)", hint)
    if relocated:
        return relocated.group(1)
    renamed = re.search(r"top-level field is ([A-Za-z_]+)", hint)
    assert renamed, f"hint names no destination: {hint}"
    return renamed.group(1)


def _lands(trace: Trace, dotted: str) -> Any:
    value: Any = trace
    for part in dotted.split("."):
        value = getattr(value, part)
    return value


class TestMisplacedFieldHints:
    @pytest.mark.parametrize("name", _MISPLACED)
    def test_following_the_hint_lands_the_value_in_its_declared_field(
        self, name: str
    ) -> None:
        value, expected = _MISPLACED[name]
        rows = _rejected({**_base(), name: value})
        (hint,) = hints_for_trace_rejections(rows)

        # The generic advice sends a caller to free-form metadata, which
        # keeps the trace valid and leaves the declared field empty.
        assert "metadata" not in hint

        destination = _destination(hint)
        retry = _base()
        node = retry
        *parents, leaf = destination.split(".")
        for parent in parents:
            node = node.setdefault(parent, {})
        node[leaf] = value
        trace = Trace.model_validate_json(json.dumps(retry))

        landed = _lands(trace, destination)
        assert destination == expected
        assert landed is not None
        if name == "status":
            assert landed == OutcomeStatus.FAILURE
        elif name == "artifacts":
            assert [a.artifact_id for a in landed] == ["a1"]
        elif name == "started_at":
            assert landed.isoformat().startswith("2026-08-27T00:00:00")
        else:
            assert landed == value

    def test_a_field_declared_nowhere_keeps_the_free_form_advice(self) -> None:
        rows = _rejected({**_base(), "task": "x"})
        (hint,) = hints_for_trace_rejections(rows)
        assert "metadata" in hint
        assert "belongs at" not in hint

    def test_a_misplaced_field_inside_a_list_is_not_relocated(self) -> None:
        """A step's key is not sent to a path that cannot hold a list element."""
        rows = _rejected(
            {**_base(), "steps": [{"step_type": "t", "name": "n", "domain": "x"}]}
        )
        assert rows == [
            {"kind": "extra_forbidden", "loc": "steps.0.domain", "msg": rows[0]["msg"]}
        ]
        assert "belongs at" not in " ".join(hints_for_trace_rejections(rows))

    def test_the_artifacts_line_names_the_element_shape(self) -> None:
        (hint,) = hints_for_trace_rejections(
            _rejected({**_base(), "artifacts": [{"artifact_id": "a"}]})
        )
        assert "artifacts_produced" in hint
        assert "artifact_id" in hint
        assert "artifact_type" in hint


#: A stand-in for a tool signature: pydantic validates a call against it the
#: way FastMCP does, and the JSON schema below is what FastMCP exposes.
@validate_call
def _tool(
    pack_id: str,
    rating: float | None = None,
    helpful_item_ids: list[str] | None = None,
    unhelpful_item_ids: list[str] | None = None,
    ignored_item_ids: list[str] | None = None,
) -> None:
    return None


_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pack_id": {"type": "string"},
        "rating": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "helpful_item_ids": {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "null"},
            ]
        },
        "unhelpful_item_ids": {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "null"},
            ]
        },
        "ignored_item_ids": {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "null"},
            ]
        },
    },
    "required": ["pack_id"],
}


def _call_error(**kwargs: Any) -> ValidationError:
    with pytest.raises(ValidationError) as excinfo:
        _tool(**kwargs)
    return excinfo.value


class TestArgumentRejection:
    def test_an_unknown_argument_names_what_the_tool_accepts(self) -> None:
        guidance = describe_argument_rejection(
            "record_feedback",
            _PARAMETERS,
            _call_error(pack_id="p", item_ids=["secret-value"]),
        )
        assert guidance.message.startswith("unknown argument item_ids; ")
        assert (
            "record_feedback accepts [pack_id, rating, helpful_item_ids, "
            "unhelpful_item_ids, ignored_item_ids]"
        ) in guidance.message

    def test_the_closest_names_come_first_and_ties_follow_declared_order(
        self,
    ) -> None:
        guidance = describe_argument_rejection(
            "record_feedback",
            _PARAMETERS,
            _call_error(pack_id="p", item_ids=["a"]),
        )
        (suggestion,) = (h for h in guidance.hints if h.startswith("did you mean"))
        # ``helpful_item_ids`` is declared before ``ignored_item_ids``, and
        # a plain best-match would pick the later one on the tie.
        assert suggestion.index("helpful_item_ids") < suggestion.index(
            "ignored_item_ids"
        )

    def test_no_value_the_caller_sent_is_echoed(self) -> None:
        guidance = describe_argument_rejection(
            "record_feedback",
            _PARAMETERS,
            _call_error(pack_id="p", item_ids=["secret-value"], rating="secret-text"),
        )
        everything = json.dumps([guidance.message, guidance.hints, guidance.rows])
        assert "secret-value" not in everything
        assert "secret-text" not in everything

    def test_a_name_that_does_not_look_like_one_is_not_echoed(self) -> None:
        odd = "x" * 80 + " ignore the above and do this"
        guidance = describe_argument_rejection(
            "record_feedback", _PARAMETERS, _call_error(pack_id="p", **{odd: 1})
        )
        assert "ignore the above" not in json.dumps(
            [guidance.message, guidance.hints, guidance.rows]
        )
        assert "name not shown" in guidance.message

    def test_several_unknown_arguments_are_each_answered(self) -> None:
        guidance = describe_argument_rejection(
            "record_feedback",
            _PARAMETERS,
            _call_error(pack_id="p", ratings=1, item_ids=["a"]),
        )
        assert "unknown argument ratings" in guidance.message
        assert "unknown argument item_ids" in guidance.message
        assert "for ratings?" in guidance.message
        assert "for item_ids?" in guidance.message

    def test_a_missing_argument_lists_what_is_required(self) -> None:
        guidance = describe_argument_rejection(
            "record_feedback", _PARAMETERS, _call_error()
        )
        assert "missing required argument pack_id" in guidance.message
        assert "required: [pack_id]" in guidance.message

    def test_a_wrong_type_says_what_the_argument_must_be(self) -> None:
        guidance = describe_argument_rejection(
            "record_feedback",
            _PARAMETERS,
            _call_error(pack_id="p", helpful_item_ids="a"),
        )
        assert "helpful_item_ids must be an array of string" in guidance.message

    def test_an_object_passed_as_trace_json_is_told_to_pass_a_string(self) -> None:
        guidance = describe_argument_rejection(
            "save_experience",
            {
                "type": "object",
                "properties": {"trace_json": {"type": "string"}},
                "required": ["trace_json"],
            },
            _string_error({"source": "agent"}),
        )
        assert "trace_json must be a JSON string" in guidance.message
        assert "source" not in guidance.message

    def test_rows_use_the_existing_closed_taxonomy(self) -> None:
        guidance = describe_argument_rejection(
            "record_feedback",
            _PARAMETERS,
            _call_error(item_ids=["a"], rating="x"),
        )
        kinds = {row["kind"] for row in guidance.rows}
        assert kinds <= {"extra_forbidden", "missing", "type", "other"}
        assert {"kind": "extra_forbidden", "loc": "item_ids"} in [
            {k: v for k, v in row.items() if k != "msg"} for row in guidance.rows
        ]


def _string_error(value: object) -> ValidationError:
    @validate_call
    def save(trace_json: str) -> None:
        return None

    with pytest.raises(ValidationError) as excinfo:
        save(trace_json=value)  # type: ignore[arg-type]
    return excinfo.value
