"""The deterministic precedent distiller: each rule, positive and negative."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.precedent_candidates import recovering_trace
from trellis.learning.precedent_distill import (
    DISTILLER_VERSION,
    RULE_FAILED_OUTCOME,
    RULE_FAILED_STEP_AVOID,
    RULE_FAILED_STEP_DO,
    SKIP_ERROR_WITHOUT_RECOVERY,
    SKIP_NO_SIGNAL,
    SKIP_OUTCOME_WITHOUT_SUMMARY,
    SKIP_RESTATES_INTENT,
    SKIP_SMOOTH_RUN,
    distill_trace,
    distill_traces,
    restates_intent,
)
from trellis.learning.scoring import normalize_intent_family
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.precedent_record import (
    MAX_ACTION_CHARS,
    MAX_BECAUSE_CHARS,
    MAX_TRIGGER_CHARS,
    PrecedentRecord,
)
from trellis.schemas.trace import Outcome, Trace, TraceContext, TraceStep


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


def test_a_retry_loop_pairs_the_last_failure_once() -> None:
    steps = [_step(error="e1", n=1), _step(error="e2", n=2), _step(n=3)]
    result = distill_trace(_trace(steps, Outcome(status="success")))
    assert {r.evidence.step_indexes[0] for r in result.records} == {1}
    assert len(result.records) == 2


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
        [_step(error=long, big=long), _step(big=long)],
        Outcome(status="failure", summary=long),
    )
    records = distill_trace(trace).records
    assert records
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
