"""``precedent.candidate``: one governed event per new candidate, and nothing else."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.mined_precedents import emit_historical_miner_row, promote_through_executor
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
from trellis.retrieve.precedents import is_miner_candidate_event, list_precedents
from trellis.schemas.enums import PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope
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


def test_readers_treat_an_executor_stamped_candidate_as_not_promoted(
    registry: StoreRegistry,
) -> None:
    record_candidates(registry, recovering_trace())
    promote_through_executor(registry, trace_id="trace-1", title="Approved lesson")
    event_log = registry.operational.event_log

    [candidate, *_] = _events(registry, EventType.PRECEDENT_CANDIDATE_MINED)
    [promoted] = _events(registry, EventType.PRECEDENT_PROMOTED)

    assert is_miner_candidate_event(candidate)
    assert not is_miner_candidate_event(promoted)
    served = list_precedents(event_log)
    assert [p["title"] for p in served] == ["Approved lesson"]
    assert SENTINEL not in str(served)


def test_the_predicate_answer_for_existing_rows_is_unchanged(
    registry: StoreRegistry,
) -> None:
    event_log = registry.operational.event_log
    emit_historical_miner_row(event_log, entity_id="m1", shape="generated")
    emit_historical_miner_row(event_log, entity_id="m2", shape="extracted")
    event_log.emit(
        EventType.PRECEDENT_PROMOTED,
        source="test",
        entity_id="p1",
        payload={"title": "t", "description": "d"},
    )

    rows = {
        e.entity_id: is_miner_candidate_event(e)
        for e in _events(registry, EventType.PRECEDENT_PROMOTED)
    }

    assert rows == {"m1": True, "m2": True, "p1": False}
