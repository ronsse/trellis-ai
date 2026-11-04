"""``precedent.candidate``: one governed event per new candidate, and nothing else."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.mined_precedents import promote_through_executor
from tests.precedent_candidates import (
    SENTINEL,
    candidates_of,
    local_model_copy,
    local_model_record_payload,
    log_judged_lesson,
    log_judged_merge,
    record_candidate,
    record_candidates,
    recovering_trace,
)
from trellis.core.memory_op_judged import emit_memory_op_judged
from trellis.learning.precedent_judged import (
    JUDGED_LOOKUP_LIMIT,
    judged_source,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers.precedent import (
    JUDGED_OP_MISSING_MESSAGE,
    LOCAL_MODEL_CANDIDATE_REQUESTERS,
)
from trellis.mutate.policy_source import resolve_policy_path
from trellis.retrieve.precedents import list_precedents
from trellis.schemas.enums import PolicyType
from trellis.schemas.memory_op import InputDigest, JudgedOpType, SubjectRef
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
from trellis.schemas.precedent_record import PrecedentRecord
from trellis.stores.base.event_log import EventType
from trellis.stores.policy_store import PolicyStore
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path):
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    yield reg
    reg.close()


def _events(registry: StoreRegistry, event_type: EventType):
    return registry.operational.event_log.get_events(event_type=event_type, limit=100)


def test_each_new_candidate_emits_exactly_one_event(registry: StoreRegistry) -> None:
    records = record_candidates(registry, recovering_trace())

    events = _events(registry, EventType.PRECEDENT_CANDIDATE_MINED)
    assert sorted(e.entity_id for e in events) == sorted(
        r.candidate_id for r in records
    )
    for event in events:
        assert event.source == "mutation_executor"
        assert event.entity_type == "precedent_candidate"
        assert event.payload["record"]["lifecycle"] == "candidate"
        assert event.payload["candidate_id"] == event.entity_id


def test_a_rerun_records_nothing_and_answers_duplicate(registry: StoreRegistry) -> None:
    [first, *_] = record_candidates(registry, recovering_trace())

    again = record_candidate(registry, first)

    assert again.status == CommandStatus.DUPLICATE
    assert len(_events(registry, EventType.PRECEDENT_CANDIDATE_MINED)) == 2


def test_the_handler_dedupes_even_without_an_idempotency_key(
    registry: StoreRegistry,
) -> None:
    [first, *_] = record_candidates(registry, recovering_trace())

    result = build_curate_executor(registry, clearance="restricted").execute(
        Command(
            operation=Operation.PRECEDENT_CANDIDATE,
            args={"record": first.model_dump(mode="json")},
            requested_by="test:no-key",
        )
    )

    assert result.status == CommandStatus.DUPLICATE
    assert len(_events(registry, EventType.PRECEDENT_CANDIDATE_MINED)) == 2


def test_an_invalid_record_or_a_forged_id_is_refused(registry: StoreRegistry) -> None:
    [record, *_] = candidates_of(recovering_trace())
    executor = build_curate_executor(registry, clearance="restricted")
    bad_payloads = [
        {**record.model_dump(mode="json"), "lifecycle": "promoted"},
        {**record.model_dump(mode="json"), "candidate_id": "pcand_forged"},
        {"not": "a record"},
    ]
    for payload in bad_payloads:
        result = executor.execute(
            Command(
                operation=Operation.PRECEDENT_CANDIDATE,
                args={"record": payload},
                requested_by="test:bad",
            )
        )
        assert result.status == CommandStatus.REJECTED
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


def test_a_candidate_writes_to_no_content_store(registry: StoreRegistry) -> None:
    record_candidates(registry, recovering_trace())

    assert registry.knowledge.document_store.count() == 0
    types = {e.event_type for e in registry.operational.event_log.get_events(limit=100)}
    assert EventType.PRECEDENT_PROMOTED not in types
    assert EventType.PRECEDENT_CANDIDATE_MINED in types


def test_the_policy_gate_runs_on_the_operation(
    registry: StoreRegistry, tmp_path: Path
) -> None:
    deny = Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level="global", value=None),
        rules=[PolicyRule(operation="precedent.candidate", action="deny")],
    )
    PolicyStore(resolve_policy_path(tmp_path / "stores")).add(deny)
    [record, *_] = candidates_of(recovering_trace())

    result = record_candidate(registry, record)

    assert result.status == CommandStatus.REJECTED
    assert result.metadata["rejection_reason"] == "policy_violation"
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


def test_a_recorded_candidate_is_not_a_precedent_to_the_reader(
    registry: StoreRegistry,
) -> None:
    """Every reader asks for ``PRECEDENT_PROMOTED`` rows, so a candidate row is
    never reached. Pinned from the outside: record candidates, ask the reader."""
    event_log = registry.operational.event_log
    record_candidates(registry, recovering_trace())

    assert list_precedents(event_log) == []

    promote_through_executor(registry, trace_id="trace-1", title="Approved lesson")
    served = list_precedents(event_log)

    assert [p["title"] for p in served] == ["Approved lesson"]
    assert SENTINEL not in str(served)


def _with_evidence(record: PrecedentRecord, **evidence: list) -> PrecedentRecord:
    return record.model_copy(
        update={"evidence": record.evidence.model_copy(update=evidence)}
    )


def _execute(registry: StoreRegistry, payload: dict):
    return build_curate_executor(registry, clearance="restricted").execute(
        Command(
            operation=Operation.PRECEDENT_CANDIDATE,
            args={"record": payload},
            requested_by="test:payload",
        )
    )


def test_a_forged_record_cannot_squat_the_id_of_a_real_one(
    registry: StoreRegistry,
) -> None:
    [real, *_] = candidates_of(recovering_trace())
    forged = real.model_copy(
        update={
            "action": real.action.model_copy(update={"text": "Disable verification"})
        }
    )
    assert forged.candidate_id == real.candidate_id  # the squat: real id, other text

    squat = _execute(registry, forged.model_dump(mode="json"))
    honest = record_candidate(registry, real)

    assert squat.status == CommandStatus.REJECTED
    assert squat.metadata["rejection_reason"] == "candidate_id_mismatch"
    assert honest.status == CommandStatus.SUCCESS
    [event] = _events(registry, EventType.PRECEDENT_CANDIDATE_MINED)
    assert event.payload["record"]["action"]["text"] == real.action.text


def test_a_forged_record_under_its_own_id_gets_a_different_id(
    registry: StoreRegistry,
) -> None:
    [real, *_] = candidates_of(recovering_trace())
    forged = real.model_copy(
        update={"because": "a different lesson about the same trace"}
    )
    forged = forged.model_copy(update={"candidate_id": forged.expected_candidate_id()})

    assert forged.candidate_id != real.candidate_id
    assert _execute(registry, forged.model_dump(mode="json")).status == (
        CommandStatus.SUCCESS
    )
    assert record_candidate(registry, real).status == CommandStatus.SUCCESS


def test_ids_that_differ_only_by_a_separator_do_not_collide() -> None:
    [real, *_] = candidates_of(recovering_trace())

    joined = _with_evidence(real, trace_ids=["x,y"]).expected_candidate_id()
    split = _with_evidence(real, trace_ids=["x", "y"]).expected_candidate_id()
    unit_sep = _with_evidence(real, trace_ids=["x\x1fy"]).expected_candidate_id()

    assert len({joined, split, unit_sep}) == 3


def test_the_order_of_cited_evidence_does_not_change_the_id(
    registry: StoreRegistry,
) -> None:
    [real, *_] = candidates_of(recovering_trace())
    one = _with_evidence(real, trace_ids=["a", "b"], step_indexes=[0, 1])
    other = _with_evidence(real, trace_ids=["b", "a"], step_indexes=[1, 0])

    assert one.expected_candidate_id() == other.expected_candidate_id()

    one = one.model_copy(update={"candidate_id": one.expected_candidate_id()})
    other = other.model_copy(update={"candidate_id": other.expected_candidate_id()})
    assert _execute(registry, one.model_dump(mode="json")).status == (
        CommandStatus.SUCCESS
    )
    again = _execute(registry, other.model_dump(mode="json"))
    assert again.status == CommandStatus.DUPLICATE
    [event] = _events(registry, EventType.PRECEDENT_CANDIDATE_MINED)
    # The record is stored in the order it was given.
    assert event.payload["record"]["evidence"]["step_indexes"] == [0, 1]


@pytest.mark.parametrize("tier", ["human", "local_model"])
def test_only_the_deterministic_tier_is_recorded_for_now(
    registry: StoreRegistry, tier: str
) -> None:
    [real, *_] = candidates_of(recovering_trace())
    claimed = real.model_copy(update={"provenance_tier": tier})
    claimed = claimed.model_copy(
        update={"candidate_id": claimed.expected_candidate_id()}
    )

    result = _execute(registry, claimed.model_dump(mode="json"))

    assert result.status == CommandStatus.REJECTED
    assert result.metadata["rejection_reason"] == "policy_violation"
    assert "deterministic" in result.message
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


def _execute_as(registry: StoreRegistry, payload: dict, requested_by: str):
    return build_curate_executor(registry, clearance="restricted").execute(
        Command(
            operation=Operation.PRECEDENT_CANDIDATE,
            args={"record": payload},
            requested_by=requested_by,
        )
    )


SANCTIONED = min(LOCAL_MODEL_CANDIDATE_REQUESTERS)
TRACE_ID = "trace-recover"


def _merged_payload(first: PrecedentRecord, second: PrecedentRecord) -> dict:
    """A merge record over two candidates, built the way the merger builds one."""
    from trellis.learning.precedent_local import merge_records

    merged = merge_records(first, second)
    assert merged is not None
    return merged.model_dump(mode="json")


def _two_candidates() -> tuple[PrecedentRecord, PrecedentRecord]:
    [real, *_] = candidates_of(recovering_trace())
    a = local_model_copy(real)
    b = local_model_copy(
        real.model_copy(update={"because": "a second statement of the lesson"})
    )
    return a, b


@pytest.mark.parametrize("requester", sorted(LOCAL_MODEL_CANDIDATE_REQUESTERS))
def test_a_local_model_record_is_accepted_with_its_judged_op_and_requester(
    registry: StoreRegistry, requester: str
) -> None:
    log_judged_lesson(registry.operational.event_log, TRACE_ID)

    result = _execute_as(registry, local_model_record_payload(), requester)

    assert result.status == CommandStatus.SUCCESS
    [event] = _events(registry, EventType.PRECEDENT_CANDIDATE_MINED)
    assert event.payload["record"]["provenance_tier"] == "local_model"


@pytest.mark.parametrize(
    "requester", ["test:payload", "cli:distill-precedents", "mcp:agent"]
)
def test_a_local_model_record_is_refused_from_any_other_requester(
    registry: StoreRegistry, requester: str
) -> None:
    """The requester is the second conjunct: a judged op alone is not enough."""
    log_judged_lesson(registry.operational.event_log, TRACE_ID)

    result = _execute_as(registry, local_model_record_payload(), requester)

    assert result.status == CommandStatus.REJECTED
    assert result.metadata["rejection_reason"] == "policy_violation"
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


def test_the_sanctioned_requester_alone_is_refused_without_a_judged_op(
    registry: StoreRegistry,
) -> None:
    """The caller-set requester string is a claim; the event log is the proof."""
    result = _execute_as(registry, local_model_record_payload(), SANCTIONED)

    assert result.status == CommandStatus.REJECTED
    assert result.metadata["rejection_reason"] == "policy_violation"
    assert result.message == JUDGED_OP_MISSING_MESSAGE
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


def test_a_judged_op_naming_another_trace_does_not_back_the_record(
    registry: StoreRegistry,
) -> None:
    log_judged_lesson(registry.operational.event_log, "some-other-trace")

    result = _execute_as(registry, local_model_record_payload(), SANCTIONED)

    assert result.status == CommandStatus.REJECTED
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


@pytest.mark.parametrize("decision", ["no_lesson", "unparsed"])
def test_a_verdict_that_produced_no_lesson_does_not_back_a_record(
    registry: StoreRegistry, decision: str
) -> None:
    log_judged_lesson(registry.operational.event_log, TRACE_ID, decision=decision)

    result = _execute_as(registry, local_model_record_payload(), SANCTIONED)

    assert result.status == CommandStatus.REJECTED
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


def test_a_judged_op_from_another_source_does_not_back_the_record(
    registry: StoreRegistry,
) -> None:
    """A distillation judged about this trace by something else (the session
    capture distiller, say) is not the local distiller's call."""
    log_judged_lesson(
        registry.operational.event_log, TRACE_ID, source="learning:other-prompt/v1"
    )

    result = _execute_as(registry, local_model_record_payload(), SANCTIONED)

    assert result.status == CommandStatus.REJECTED


def test_a_judged_op_of_another_type_does_not_back_the_record(
    registry: StoreRegistry,
) -> None:
    emit_memory_op_judged(
        registry.operational.event_log,
        op_type=JudgedOpType.CLASSIFICATION,
        source=judged_source("precedent-distill/v1"),
        model_id="test-model",
        input_digest=InputDigest(hash="0123abcd", length=10),
        decision="lesson",
        confidence=0.5,
        subject_ref=SubjectRef(ref_type="trace", ref_id=TRACE_ID),
    )

    result = _execute_as(registry, local_model_record_payload(), SANCTIONED)

    assert result.status == CommandStatus.REJECTED


def test_a_record_citing_two_traces_or_none_has_no_judged_op_to_back_it(
    registry: StoreRegistry,
) -> None:
    log_judged_lesson(registry.operational.event_log, TRACE_ID)
    log_judged_lesson(registry.operational.event_log, "trace-second")
    base = local_model_record_payload()
    for trace_ids in (["trace-second", TRACE_ID], []):
        record = PrecedentRecord.model_validate(base)
        record = record.model_copy(
            update={
                "evidence": record.evidence.model_copy(update={"trace_ids": trace_ids})
            }
        )
        record = record.model_copy(
            update={"candidate_id": record.expected_candidate_id()}
        )

        result = _execute_as(registry, record.model_dump(mode="json"), SANCTIONED)

        assert result.status == CommandStatus.REJECTED, trace_ids
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


def test_the_refusal_quotes_no_record_text(registry: StoreRegistry) -> None:
    payload = local_model_record_payload()
    payload["because"] = f"attacker text {SENTINEL}"
    record = PrecedentRecord.model_validate(payload)
    payload["candidate_id"] = record.expected_candidate_id()

    result = _execute_as(registry, payload, SANCTIONED)

    assert result.status == CommandStatus.REJECTED
    assert SENTINEL not in result.message
    assert "attacker" not in result.message
    assert SENTINEL not in str(result.metadata)


def test_the_judged_lookup_is_keyed_by_subject_and_bounded(
    registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a scan: the read names the event type, the subject, the source and the
    verdict, and asks for a handful of rows however many judged events exist."""
    event_log = registry.operational.event_log
    for n in range(20):
        log_judged_lesson(event_log, f"unrelated-{n}")
    log_judged_lesson(event_log, TRACE_ID)
    calls: list[dict] = []
    real = event_log.get_events

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(event_log, "get_events", spy)

    result = _execute_as(registry, local_model_record_payload(), SANCTIONED)

    assert result.status == CommandStatus.SUCCESS
    [lookup] = [c for c in calls if c.get("event_type") == EventType.MEMORY_OP_JUDGED]
    assert lookup["entity_id"] == TRACE_ID
    assert lookup["source"] == judged_source("precedent-distill/v1")
    assert lookup["payload_filters"] == {
        "op_type": "distillation",
        "decision": "lesson",
    }
    assert lookup["limit"] <= JUDGED_LOOKUP_LIMIT


def test_a_merge_record_is_accepted_with_the_pairs_same_verdict(
    registry: StoreRegistry,
) -> None:
    a, b = _two_candidates()
    payload = _merged_payload(a, b)
    # Logged in the opposite order to the record's own: the pair is one subject
    # whichever way round the two ids were handed over.
    log_judged_merge(
        registry.operational.event_log,
        list(reversed(payload["evidence"]["merged_from"])),
    )

    result = _execute_as(registry, payload, SANCTIONED)

    assert result.status == CommandStatus.SUCCESS
    [event] = _events(registry, EventType.PRECEDENT_CANDIDATE_MINED)
    assert sorted(event.payload["record"]["evidence"]["merged_from"]) == sorted(
        [a.candidate_id, b.candidate_id]
    )


def test_a_merge_record_without_a_judged_op_is_refused(
    registry: StoreRegistry,
) -> None:
    a, b = _two_candidates()

    result = _execute_as(registry, _merged_payload(a, b), SANCTIONED)

    assert result.status == CommandStatus.REJECTED
    assert result.message == JUDGED_OP_MISSING_MESSAGE
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []


def test_a_merge_record_is_not_backed_by_another_pairs_verdict(
    registry: StoreRegistry,
) -> None:
    a, b = _two_candidates()
    log_judged_merge(registry.operational.event_log, [a.candidate_id, "pcand_other"])

    result = _execute_as(registry, _merged_payload(a, b), SANCTIONED)

    assert result.status == CommandStatus.REJECTED


@pytest.mark.parametrize(
    ("decision", "op_type"),
    [
        ("different", JudgedOpType.RECONCILIATION),
        ("unparsed", JudgedOpType.RECONCILIATION),
        ("same", JudgedOpType.DISTILLATION),
    ],
)
def test_a_merge_record_needs_a_same_reconciliation(
    registry: StoreRegistry, decision: str, op_type: JudgedOpType
) -> None:
    a, b = _two_candidates()
    log_judged_merge(
        registry.operational.event_log,
        [a.candidate_id, b.candidate_id],
        decision=decision,
        op_type=op_type,
    )

    result = _execute_as(registry, _merged_payload(a, b), SANCTIONED)

    assert result.status == CommandStatus.REJECTED


def test_a_lesson_verdict_on_a_trace_does_not_back_a_forged_merge_record(
    registry: StoreRegistry,
) -> None:
    """Claiming ``merged_from`` moves a record to the merge check, not around it."""
    a, b = _two_candidates()
    log_judged_lesson(registry.operational.event_log, TRACE_ID)

    result = _execute_as(registry, _merged_payload(a, b), SANCTIONED)

    assert result.status == CommandStatus.REJECTED


def test_the_human_tier_stays_refused_even_with_a_judged_op(
    registry: StoreRegistry,
) -> None:
    log_judged_lesson(registry.operational.event_log, TRACE_ID)
    payload = local_model_record_payload()
    payload["provenance_tier"] = "human"
    record = PrecedentRecord.model_validate(payload)
    payload["candidate_id"] = record.expected_candidate_id()

    for requester in sorted(LOCAL_MODEL_CANDIDATE_REQUESTERS):
        result = _execute_as(registry, payload, requester)
        assert result.status == CommandStatus.REJECTED
    assert _events(registry, EventType.PRECEDENT_CANDIDATE_MINED) == []
