"""``precedent.candidate``: one governed event per new candidate, and nothing else."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.mined_precedents import promote_through_executor
from tests.precedent_candidates import (
    SENTINEL,
    candidates_of,
    record_candidate,
    record_candidates,
    recovering_trace,
)
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.policy_source import resolve_policy_path
from trellis.retrieve.precedents import list_precedents
from trellis.schemas.enums import PolicyType
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
