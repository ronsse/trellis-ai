"""The deterministic precedent distiller: each rule, positive and negative."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.precedent_candidates import recovering_trace
from trellis.learning.precedent_distill import (
    DISTILLER_VERSION,
    DROP_ENTITY_ID_OVERLONG,
    DROP_FAILED_APPROACH_OVER_CAP,
    RULE_FAILED_OUTCOME,
    RULE_FAILED_STEP_AVOID,
    RULE_FAILED_STEP_DO,
    SKIP_ERROR_WITHOUT_RECOVERY,
    SKIP_INVALID_RECORD,
    SKIP_NO_SIGNAL,
    SKIP_OUTCOME_WITHOUT_SUMMARY,
    SKIP_RESTATES_INTENT,
    SKIP_SMOOTH_RUN,
    SKIP_TRANSIENT_RETRY,
    distill_trace,
    distill_traces,
    restates_intent,
)
from trellis.learning.scoring import normalize_intent_family
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.precedent_record import (
    MAX_ACTION_CHARS,
    MAX_BECAUSE_CHARS,
    MAX_DOMAIN_CHARS,
    MAX_ID_CHARS,
    MAX_LIST_ITEMS,
    MAX_TRIGGER_CHARS,
    PrecedentRecord,
)
from trellis.schemas.trace import EvidenceRef, Outcome, Trace, TraceContext, TraceStep


def _trace(steps, outcome=None, intent="deploy the service") -> Trace:
    return Trace(
        trace_id="t1",
        source=TraceSource.AGENT,
        intent=intent,
        steps=steps,
        outcome=outcome,
        context=TraceContext(domain="platform"),
    )


def _step(name="run", error=None, step_type="tool_call", **args) -> TraceStep:
    return TraceStep(step_type=step_type, name=name, error=error, args=args)


def _reasons(trace: Trace) -> list[str]:
    return [s.reason for s in distill_trace(trace).skips]


def test_a_recovered_step_gives_an_avoid_and_a_do() -> None:
    result = distill_trace(recovering_trace())

    by_rule = {r.rule: r for r in result.records}
    assert set(by_rule) == {RULE_FAILED_STEP_AVOID, RULE_FAILED_STEP_DO}
    avoid, do = by_rule[RULE_FAILED_STEP_AVOID], by_rule[RULE_FAILED_STEP_DO]
    assert (avoid.action.kind, do.action.kind) == ("avoid", "do")
    assert "strict" in avoid.action.text
    assert "lenient" in do.action.text
    assert avoid.situation.trigger.startswith("schema mismatch")
    assert avoid.evidence.step_indexes == do.evidence.step_indexes == [0, 1]
    assert avoid.evidence.trace_ids == ["trace-recover"]
    assert avoid.evidence.feedback_ids == avoid.evidence.pack_ids == []
    assert (avoid.lifecycle, avoid.provenance_tier) == ("candidate", "deterministic")
    assert avoid.distiller_version == DISTILLER_VERSION


def test_an_error_with_no_later_success_of_the_same_step_gives_nothing() -> None:
    trace = _trace([_step("a", error="boom"), _step("b")], Outcome(status="success"))
    result = distill_trace(trace)
    assert result.records == ()
    assert _reasons(trace) == [SKIP_ERROR_WITHOUT_RECOVERY]


def _records(trace: Trace) -> dict[str, list[PrecedentRecord]]:
    by_rule: dict[str, list[PrecedentRecord]] = {}
    for record in distill_trace(trace).records:
        by_rule.setdefault(record.rule, []).append(record)
    return by_rule


def test_a_retry_with_identical_arguments_is_transient_and_gives_nothing() -> None:
    trace = _trace([_step(error="timeout", a=1), _step(a=1)], Outcome(status="success"))

    result = distill_trace(trace)

    # Never "avoid" the call that worked, and "retry it" is not reusable.
    assert result.records == ()
    assert [s.reason for s in result.skips] == [SKIP_TRANSIENT_RETRY]


def test_a_failure_with_other_arguments_avoids_it_and_uses_the_one_that_worked() -> (
    None
):
    trace = _trace(
        [_step(error="bad mode", a=1), _step(a=2)], Outcome(status="success")
    )

    by_rule = _records(trace)

    [avoid] = by_rule[RULE_FAILED_STEP_AVOID]
    [do] = by_rule[RULE_FAILED_STEP_DO]
    assert '"a": 1' in avoid.action.text
    assert '"a": 2' not in avoid.action.text
    assert '"a": 2' in do.action.text
    assert avoid.evidence.step_indexes == do.evidence.step_indexes == [0, 1]
    assert _reasons(trace) == []


def test_every_distinct_failed_approach_is_avoided_and_one_do_is_given() -> None:
    steps = [
        _step(error="e1", a=1),
        _step(error="e3", a=3),
        _step(error="e1 again", a=1),
        _step(a=2),
    ]

    by_rule = _records(_trace(steps, Outcome(status="success")))

    avoids = by_rule[RULE_FAILED_STEP_AVOID]
    [do] = by_rule[RULE_FAILED_STEP_DO]
    # a=1 is avoided once, from its latest failure; a=3 once.
    assert sorted(r.evidence.step_indexes for r in avoids) == [[1, 3], [2, 3]]
    assert {r.situation.trigger for r in avoids} == {"e3", "e1 again"}
    assert do.evidence.step_indexes == [1, 2, 3]
    assert do.situation.trigger == "e1 again"
    assert "failing once" not in do.action.text + do.because
    assert all('"a": 2' not in r.action.text for r in avoids)


def test_a_transient_failure_among_real_ones_is_not_avoided() -> None:
    steps = [_step(error="e1", a=1), _step(error="blip", a=2), _step(a=2)]

    by_rule = _records(_trace(steps, Outcome(status="success")))

    [avoid] = by_rule[RULE_FAILED_STEP_AVOID]
    assert '"a": 1' in avoid.action.text
    assert avoid.evidence.step_indexes == [0, 2]
    [do] = by_rule[RULE_FAILED_STEP_DO]
    assert do.evidence.step_indexes == [0, 2]


def test_interleaved_step_names_are_paired_by_kind() -> None:
    steps = [
        _step("load", error="e-load", a=1),
        _step("save", error="e-save", b=1),
        _step("load", a=2),
        _step("save", b=1),
    ]
    trace = _trace(steps, Outcome(status="success"))

    by_rule = _records(trace)

    [avoid] = by_rule[RULE_FAILED_STEP_AVOID]
    [do] = by_rule[RULE_FAILED_STEP_DO]
    assert "tool_call/load" in avoid.action.text
    assert avoid.evidence.step_indexes == do.evidence.step_indexes == [0, 2]
    assert _reasons(trace) == [SKIP_TRANSIENT_RETRY]


def test_arguments_that_differ_only_past_the_display_cut_are_told_apart() -> None:
    long = "x" * 300
    trace = _trace(
        [_step(error="boom", blob=long + "1"), _step(blob=long + "2")],
        Outcome(status="success"),
    )

    by_rule = _records(trace)
    [avoid], [do] = by_rule[RULE_FAILED_STEP_AVOID], by_rule[RULE_FAILED_STEP_DO]

    # Equality compares the whole arguments, so the avoid exists. The shortened
    # display of the two calls reads the same, so the avoid names where they differ.
    assert _reasons(trace) == []
    assert "(differs at 'blob')" in avoid.action.text
    assert "differs at" not in do.action.text


def test_the_first_differing_key_is_named_in_key_order() -> None:
    long = "y" * 300
    trace = _trace(
        [
            _step(error="boom", a=long, b=long + "1", c=long + "1"),
            _step(a=long, b=long + "2", c=long + "2"),
        ],
        Outcome(status="success"),
    )

    [avoid] = _records(trace)[RULE_FAILED_STEP_AVOID]

    assert "(differs at 'b')" in avoid.action.text


def test_a_key_present_on_one_side_only_is_the_difference() -> None:
    long = "z" * 300
    trace = _trace(
        [_step(error="boom", a=long, b=1), _step(a=long, c=1)],
        Outcome(status="success"),
    )

    # Same length, same shortened display: only the key sets differ.
    [avoid] = _records(trace)[RULE_FAILED_STEP_AVOID]
    assert "(differs at 'b')" in avoid.action.text


def test_a_longer_argument_set_reads_differently_without_a_hint() -> None:
    long = "z" * 300
    trace = _trace(
        [_step(error="boom", a=long), _step(a=long, extra=1)],
        Outcome(status="success"),
    )

    # The elision marker counts what was cut, so the two displays already differ.
    [avoid] = _records(trace)[RULE_FAILED_STEP_AVOID]
    assert "differs at" not in avoid.action.text


def test_arguments_that_read_differently_get_no_hint() -> None:
    [avoid] = _records(recovering_trace())[RULE_FAILED_STEP_AVOID]

    assert "differs at" not in avoid.action.text


def test_an_integral_float_is_the_same_argument_as_the_integer() -> None:
    same = _trace(
        [
            _step(error="boom", n=1.0, nested={"a": [2.0, {"b": 3.0}]}),
            _step(n=1, nested={"a": [2, {"b": 3}]}),
        ],
        Outcome(status="success"),
    )
    assert distill_trace(same).records == ()
    assert _reasons(same) == [SKIP_TRANSIENT_RETRY]

    # A boolean is not the number one, and 1.5 is not 1.
    for first, second in ((True, 1), (1.5, 1), (0.0, False)):
        trace = _trace(
            [_step(error="boom", n=first), _step(n=second)], Outcome(status="success")
        )
        assert RULE_FAILED_STEP_AVOID in _records(trace), (first, second)


def test_comparing_arguments_does_not_change_them() -> None:
    step = _step(error="boom", n=1.0, nested={"a": [2.0]})
    distill_trace(_trace([step, _step(n=1)], Outcome(status="success")))

    assert step.args == {"n": 1.0, "nested": {"a": [2.0]}}
    assert isinstance(step.args["n"], float)


def test_failures_that_never_worked_skip_once_per_step_kind() -> None:
    steps = [_step("a", error="1"), _step("a", error="2"), _step("b", error="3")]
    trace = _trace(steps, Outcome(status="success"))

    assert distill_trace(trace).records == ()
    assert _reasons(trace) == [SKIP_ERROR_WITHOUT_RECOVERY] * 2


def test_an_unknown_outcome_is_no_signal_not_a_smooth_run() -> None:
    assert _reasons(_trace([_step()], Outcome(status="unknown"))) == [SKIP_NO_SIGNAL]


def test_a_failed_outcome_with_a_summary_gives_an_avoid_with_the_summary() -> None:
    trace = _trace(
        [_step("migrate"), _step("verify")],
        Outcome(status=OutcomeStatus.FAILURE, summary="the lock never released"),
    )
    [record] = distill_trace(trace).records
    assert record.rule == RULE_FAILED_OUTCOME
    assert record.action.kind == "avoid"
    assert record.because == "the lock never released"
    assert "tool_call/migrate" in record.action.text
    assert record.confidence == 0.5


def test_a_partial_outcome_is_less_confident_than_a_failure() -> None:
    [record] = distill_trace(
        _trace([_step()], Outcome(status="partial", summary="half done"))
    ).records
    assert record.confidence == 0.4


def test_a_failed_outcome_without_a_summary_gives_nothing() -> None:
    trace = _trace([_step()], Outcome(status="failure"))
    assert distill_trace(trace).records == ()
    assert _reasons(trace) == [SKIP_OUTCOME_WITHOUT_SUMMARY]


def test_a_plain_success_gives_no_candidate() -> None:
    trace = _trace([_step("a"), _step("b")], Outcome(status="success", summary="ok"))
    assert distill_trace(trace).records == ()
    assert _reasons(trace) == [SKIP_SMOOTH_RUN]


def test_a_trace_with_no_outcome_and_no_error_is_no_signal() -> None:
    assert _reasons(_trace([_step()])) == [SKIP_NO_SIGNAL]


def test_a_candidate_that_only_restates_the_intent_is_dropped_with_a_reason() -> None:
    trace = _trace(
        [], Outcome(status="failure", summary="it broke"), intent="Deploy the service"
    )
    # No steps: the only text left for the action is the intent itself.
    assert restates_intent("deploy the service", "Deploy the service")
    assert distill_trace(trace).records == ()
    assert _reasons(trace) == [SKIP_RESTATES_INTENT]


def test_an_action_with_new_content_does_not_restate_the_intent() -> None:
    assert not restates_intent("Avoid tool_call/migrate", "deploy the service")
    assert not restates_intent("", "deploy the service")


def test_the_same_input_gives_the_same_ids_and_records() -> None:
    first = distill_trace(recovering_trace()).records
    second = distill_trace(recovering_trace()).records
    assert first == second
    assert len({r.candidate_id for r in first}) == len(first) == 2
    assert all(r.candidate_id == r.expected_candidate_id() for r in first)


def test_a_different_trace_or_version_changes_the_id() -> None:
    a = distill_trace(recovering_trace(trace_id="a")).records[0]
    b = distill_trace(recovering_trace(trace_id="b")).records[0]
    assert a.candidate_id != b.candidate_id
    bumped = a.model_copy(update={"distiller_version": "deterministic-99"})
    assert bumped.expected_candidate_id() != a.candidate_id


def test_the_intent_family_is_the_frozen_learning_key() -> None:
    trace = recovering_trace()
    [avoid, _do] = distill_trace(trace).records
    assert avoid.situation.intent_family == normalize_intent_family(intent=trace.intent)
    assert avoid.situation.domain == "data"


def test_every_text_field_is_bounded() -> None:
    long = "x " * 2000
    trace = _trace(
        [_step(error=long, big=long), _step(big=long + "!")],
        Outcome(status="failure", summary=long),
    )
    records = distill_trace(trace).records
    assert {r.rule for r in records} == {
        RULE_FAILED_STEP_AVOID,
        RULE_FAILED_STEP_DO,
        RULE_FAILED_OUTCOME,
    }
    for r in records:
        assert len(r.situation.trigger) <= MAX_TRIGGER_CHARS
        assert len(r.action.text) <= MAX_ACTION_CHARS
        assert len(r.because) <= MAX_BECAUSE_CHARS


def test_the_schema_refuses_an_unbounded_or_extra_field() -> None:
    [record] = distill_trace(
        _trace([_step()], Outcome(status="failure", summary="s"))
    ).records
    data = record.model_dump(mode="json")
    with pytest.raises(ValidationError):
        PrecedentRecord.model_validate(
            {**data, "because": "y" * (MAX_BECAUSE_CHARS + 1)}
        )
    with pytest.raises(ValidationError):
        PrecedentRecord.model_validate({**data, "surprise": 1})
    with pytest.raises(ValidationError):
        PrecedentRecord.model_validate({**data, "lifecycle": "promoted"})


def test_a_batch_tallies_rules_and_skips() -> None:
    tally = distill_traces(
        [
            recovering_trace(),
            _trace([_step()], Outcome(status="success")),
        ]
    )
    assert tally.traces_read == 2
    assert tally.by_rule[RULE_FAILED_STEP_AVOID] == 1
    assert tally.skips_by_reason[SKIP_SMOOTH_RUN] == 1


def test_every_field_copied_from_the_trace_is_bounded() -> None:
    trace = _trace(
        [_step()], Outcome(status="failure", summary="s"), intent="deploy the service"
    ).model_copy(
        update={
            "context": TraceContext(domain="d" * 130),
            "evidence_used": [
                EvidenceRef(evidence_id="ok-id"),
                EvidenceRef(evidence_id="e" * (MAX_ID_CHARS + 1)),
            ],
        }
    )

    [record] = distill_trace(trace).records

    assert record.situation.domain is not None
    assert len(record.situation.domain) <= MAX_DOMAIN_CHARS
    # An id cannot be shortened and stay an id, so an over-long one is left out.
    assert record.situation.entity_ids == ["ok-id"]


def test_a_trace_that_cannot_make_a_valid_record_is_a_skip_not_a_crash() -> None:
    bad = _trace([_step()], Outcome(status="failure", summary="s")).model_copy(
        update={"trace_id": "t" * (MAX_ID_CHARS + 1)}
    )
    good = _trace([_step()], Outcome(status="failure", summary="s"))

    result = distill_trace(bad)
    tally = distill_traces([bad, good, bad])

    assert result.records == ()
    assert [s.reason for s in result.skips] == [SKIP_INVALID_RECORD]
    assert tally.traces_read == 3
    assert tally.skips_by_reason[SKIP_INVALID_RECORD] == 2
    assert [r.evidence.trace_ids for r in tally.records] == [["t1"]]


def _nested(depth: int) -> dict:
    node: dict = {"leaf": 1}
    for _ in range(depth):
        node = {"k": node}
    return node


def _cycle() -> dict:
    node: dict = {}
    node["self"] = node
    return node


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"deep": _nested(5000)}, id="nested-too-deep"),
        pytest.param({"mixed": {1: "a", "b": 2}}, id="keys-that-do-not-sort"),
        pytest.param({"loop": _cycle()}, id="cycle"),
    ],
)
def test_arguments_that_cannot_be_compared_are_a_skip_not_an_exception(
    args: dict,
) -> None:
    bad = _trace(
        [
            TraceStep(step_type="tool_call", name="run", error="boom", args=args),
            TraceStep(step_type="tool_call", name="run", args=args),
        ],
        Outcome(status="success"),
    )
    good = _trace([_step()], Outcome(status="failure", summary="s"))

    result = distill_trace(bad)
    tally = distill_traces([bad, good])

    assert result.records == ()
    assert [s.reason for s in result.skips] == [SKIP_INVALID_RECORD]
    assert tally.traces_read == 2
    assert tally.skips_by_reason[SKIP_INVALID_RECORD] == 1
    assert [r.evidence.trace_ids for r in tally.records] == [["t1"]]


def test_an_unexpected_error_is_not_swallowed() -> None:
    # Only the errors comparing arguments can raise are caught; a bug is loud.
    class BoomError(Exception):
        pass

    class Exploding(dict):
        def items(self):
            raise BoomError

    trace = _trace(
        [
            TraceStep(
                step_type="tool_call", name="run", error="boom", args={"x": 1}
            ).model_copy(update={"args": Exploding(x=1)}),
            _step(x=2),
        ],
        Outcome(status="success"),
    )

    with pytest.raises(BoomError):
        distill_trace(trace)


def test_an_over_long_entity_id_is_left_out_and_counted() -> None:
    trace = _trace([_step()], Outcome(status="failure", summary="s")).model_copy(
        update={
            "evidence_used": [
                EvidenceRef(evidence_id="ok-id"),
                EvidenceRef(evidence_id="e" * (MAX_ID_CHARS + 1)),
                EvidenceRef(evidence_id="f" * (MAX_ID_CHARS + 5)),
            ]
        }
    )

    result = distill_trace(trace)
    tally = distill_traces([trace, trace])

    assert [r.situation.entity_ids for r in result.records] == [["ok-id"]]
    assert result.dropped == {DROP_ENTITY_ID_OVERLONG: 2}
    # The tally adds the traces' counts; a name that dropped nothing is absent.
    assert dict(tally.dropped) == {DROP_ENTITY_ID_OVERLONG: 4}


def test_a_trace_that_drops_nothing_reports_no_drop() -> None:
    assert distill_trace(recovering_trace()).dropped == {}
    assert dict(distill_traces([recovering_trace()]).dropped) == {}


def test_failed_approaches_beyond_the_cap_are_not_avoided_and_are_counted() -> None:
    extra = 4
    failed = MAX_LIST_ITEMS - 1 + extra
    steps = [_step(error=f"boom {i}", n=i) for i in range(failed)]
    steps.append(_step(n=failed))
    trace = _trace(steps, Outcome(status="success"))

    result = distill_trace(trace)
    avoids = [r for r in result.records if r.rule == RULE_FAILED_STEP_AVOID]

    assert len(avoids) == MAX_LIST_ITEMS - 1
    assert result.dropped == {DROP_FAILED_APPROACH_OVER_CAP: extra}
    # The latest failures are the ones kept, the earliest are the ones dropped.
    kept = sorted(i for r in avoids for i in r.evidence.step_indexes[:1])
    assert kept == list(range(extra, failed))
