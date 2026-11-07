"""The graph writers that replace a node's whole bag write only over what they read.

``entity.create`` (over an existing id, and the ``if_absent`` placeholder),
``observation.record`` and ``measurement.record`` read the node, run their
checks on the read, then write. An unconditional write in the last step
replaced a version the checks had not seen: an ``entity.update`` that set a
first ``evidence_ref`` in between was lost, and an ``if_absent`` placeholder
overwrote a real create that committed in between.

Each test makes that window deterministic with
:func:`tests.unit.mutate._purge_race.rival_inside_first_read`, which executes a
rival command through the real executor between the handler's first read and
the read's return. The rival's write is committed, so the handler holds a stale
row and the store must refuse its write.

Defective subject: write with ``upsert_node`` again in the three handlers and
every test here that names a lost write fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.mutate._purge_race import rival_inside_first_read
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import (
    OUTCOME_LEFT_ALONE,
    OUTCOME_METADATA_KEY,
    Command,
    CommandStatus,
    Operation,
)
from trellis.mutate.handlers._common import _REWRITE_ATTEMPTS
from trellis.mutate.handlers.graph import ENTITY_LEFT_ALONE_MESSAGE
from trellis.schemas.measurement import Measurement
from trellis.schemas.observation import Observation
from trellis.schemas.well_known import MEASUREMENT, OBSERVATION
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _create(node_id: str, **properties: Any) -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_type": "concept",
            "entity_id": node_id,
            "name": "Original",
            "properties": properties,
        },
    )


def _set_evidence_ref(node_id: str, ref: str) -> Command:
    return Command(
        operation=Operation.ENTITY_UPDATE,
        args={"entity_id": node_id, "properties": {"evidence_ref": ref}},
    )


def _subject(registry: StoreRegistry) -> str:
    return registry.knowledge.graph_store.upsert_node(
        node_id=None, node_type="Dataset", properties={"name": "users"}
    )


def _observation(subject_id: str, **kwargs: Any) -> Observation:
    fields: dict[str, Any] = {"content": "first reading", **kwargs}
    return Observation(
        observation_id="obs-1",
        subject_entity_id=subject_id,
        subject_entity_type="Dataset",
        observer_agent_id="agent-1",
        **fields,
    )


def _measurement(subject_id: str, **kwargs: Any) -> Measurement:
    return Measurement(
        measurement_id="meas-1",
        subject_entity_id=subject_id,
        subject_entity_type="Dataset",
        metric_name="rows",
        metric_value=3.0,
        observer_agent_id="agent-1",
        **kwargs,
    )


def _props(registry: StoreRegistry, node_id: str) -> dict[str, Any]:
    node = registry.knowledge.graph_store.get_node(node_id)
    assert node is not None
    return node["properties"]


class TestEntityCreateOverAnExistingNode:
    def test_a_first_evidence_ref_set_mid_command_is_carried_forward(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put("doc-1", "body", {})
        executor = build_curate_executor(registry, clearance="restricted")
        assert executor.execute(_create("n1")).status == CommandStatus.SUCCESS

        with rival_inside_first_read(
            registry, executor, "n1", _set_evidence_ref("n1", "doc-1")
        ) as rival:
            loser = executor.execute(_create("n1", owner="team-b"))

        [committed] = rival
        assert committed.status == CommandStatus.SUCCESS
        assert loser.status == CommandStatus.SUCCESS
        assert _props(registry, "n1") == {
            "name": "Original",
            "owner": "team-b",
            "evidence_ref": "doc-1",
        }
        assert len(registry.knowledge.graph_store.get_node_history("n1")) == 3

    def test_a_conflicting_evidence_ref_is_refused_on_the_re_read(
        self, registry: StoreRegistry
    ) -> None:
        """The check ran on the stale read, which held no ``evidence_ref``."""
        registry.knowledge.document_store.put("doc-1", "body", {})
        registry.knowledge.document_store.put("doc-2", "body", {})
        executor = build_curate_executor(registry, clearance="restricted")
        executor.execute(_create("n1"))

        with rival_inside_first_read(
            registry, executor, "n1", _set_evidence_ref("n1", "doc-1")
        ):
            loser = executor.execute(_create("n1", evidence_ref="doc-2"))

        assert loser.status == CommandStatus.REJECTED
        assert loser.metadata["rejection_reason"] == "node_evidence_ref_changed"
        assert _props(registry, "n1")["evidence_ref"] == "doc-1"
        events = registry.operational.event_log
        assert [
            e.payload["command_id"]
            for e in events.get_events(event_type=EventType.MUTATION_REJECTED)
        ] == [loser.command_id]

    def test_a_node_that_never_settles_fails_after_the_attempt_cap(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor = build_curate_executor(registry, clearance="restricted")
        executor.execute(_create("n1"))
        graph = registry.knowledge.graph_store
        attempts: list[str] = []

        def always_stale(nid: str, *args: Any, **kwargs: Any) -> bool:
            attempts.append(nid)
            if len(attempts) > 100:
                msg = "retry is unbounded"
                raise RuntimeError(msg)
            return False

        monkeypatch.setattr(graph, "update_node_if_current", always_stale)
        result = executor.execute(_create("n1", owner="team-b"))

        assert result.status == CommandStatus.FAILED
        assert attempts == ["n1"] * _REWRITE_ATTEMPTS
        assert _props(registry, "n1") == {"name": "Original"}
        events = registry.operational.event_log
        assert len(events.get_events(event_type=EventType.ENTITY_CREATED)) == 1


class TestIfAbsentPlaceholder:
    @staticmethod
    def _placeholder(node_id: str) -> Command:
        return Command(
            operation=Operation.ENTITY_CREATE,
            args={
                "entity_type": "concept",
                "entity_id": node_id,
                "name": "Placeholder",
                "properties": {"stub": True},
                "if_absent": True,
            },
        )

    def test_it_does_not_replace_a_real_create_committed_mid_command(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_curate_executor(registry, clearance="restricted")

        with rival_inside_first_read(
            registry, executor, "n1", _create("n1", owner="team-a")
        ) as rival:
            placeholder = executor.execute(self._placeholder("n1"))

        [real] = rival
        assert real.status == CommandStatus.SUCCESS
        assert placeholder.status == CommandStatus.SUCCESS
        assert placeholder.created_id == "n1"
        assert placeholder.metadata[OUTCOME_METADATA_KEY] == OUTCOME_LEFT_ALONE
        # The human message is unchanged; the outcome is what a caller reads.
        assert placeholder.message.startswith(ENTITY_LEFT_ALONE_MESSAGE)
        graph = registry.knowledge.graph_store
        assert _props(registry, "n1") == {"name": "Original", "owner": "team-a"}
        assert len(graph.get_node_history("n1")) == 1
        events = registry.operational.event_log
        assert len(events.get_events(event_type=EventType.ENTITY_CREATED)) == 1

    def test_it_creates_when_nothing_raced(self, registry: StoreRegistry) -> None:
        executor = build_curate_executor(registry, clearance="restricted")

        result = executor.execute(self._placeholder("n1"))

        assert result.status == CommandStatus.SUCCESS
        assert _props(registry, "n1") == {"name": "Placeholder", "stub": True}
        assert OUTCOME_METADATA_KEY not in result.metadata
        assert result.message == "Entity created: Placeholder"

    def test_a_placeholder_over_an_existing_node_reports_it_was_left_alone(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_curate_executor(registry, clearance="restricted")
        executor.execute(_create("n1", owner="team-a"))

        result = executor.execute(self._placeholder("n1"))

        assert result.status == CommandStatus.SUCCESS
        assert result.metadata == {OUTCOME_METADATA_KEY: OUTCOME_LEFT_ALONE}
        assert result.message == f"{ENTITY_LEFT_ALONE_MESSAGE}: Placeholder"
        assert _props(registry, "n1") == {"name": "Original", "owner": "team-a"}

    def test_a_node_that_never_settles_fails_after_the_attempt_cap(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A create that keeps losing is a failure, not a silent skip."""
        executor = build_curate_executor(registry, clearance="restricted")
        graph = registry.knowledge.graph_store
        attempts: list[str] = []

        def never_created(nid: str, *args: Any, **kwargs: Any) -> bool:
            attempts.append(nid)
            return False

        monkeypatch.setattr(graph, "create_node_if_absent", never_created)
        result = executor.execute(self._placeholder("n1"))

        assert result.status == CommandStatus.FAILED
        assert attempts == ["n1"] * _REWRITE_ATTEMPTS
        assert graph.get_node("n1") is None


class TestObservationRecord:
    def test_a_first_evidence_ref_set_mid_command_is_carried_forward(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put("doc-1", "body", {})
        executor = build_curate_executor(registry, clearance="restricted")
        subject = _subject(registry)
        record = Command(
            operation=Operation.OBSERVATION_RECORD,
            args={"observation": _observation(subject)},
        )
        assert executor.execute(record).status == CommandStatus.SUCCESS
        revised = Command(
            operation=Operation.OBSERVATION_RECORD,
            args={"observation": _observation(subject, content="revised reading")},
        )

        with rival_inside_first_read(
            registry, executor, "obs-1", _set_evidence_ref("obs-1", "doc-1")
        ) as rival:
            loser = executor.execute(revised)

        [committed] = rival
        assert committed.status == CommandStatus.SUCCESS
        assert loser.status == CommandStatus.SUCCESS
        props = _props(registry, "obs-1")
        assert props["content"] == "revised reading"
        assert props["evidence_ref"] == "doc-1"
        node = registry.knowledge.graph_store.get_node("obs-1")
        assert node is not None
        assert node["node_type"] == OBSERVATION

    def test_a_conflicting_evidence_ref_is_refused_on_the_re_read(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put("doc-1", "body", {})
        registry.knowledge.document_store.put("doc-2", "body", {})
        executor = build_curate_executor(registry, clearance="restricted")
        subject = _subject(registry)
        executor.execute(
            Command(
                operation=Operation.OBSERVATION_RECORD,
                args={"observation": _observation(subject)},
            )
        )
        conflicting = Command(
            operation=Operation.OBSERVATION_RECORD,
            args={"observation": _observation(subject, evidence_ref="doc-2")},
        )

        with rival_inside_first_read(
            registry, executor, "obs-1", _set_evidence_ref("obs-1", "doc-1")
        ):
            loser = executor.execute(conflicting)

        assert loser.status == CommandStatus.REJECTED
        assert loser.metadata["rejection_reason"] == "node_evidence_ref_changed"
        assert _props(registry, "obs-1")["evidence_ref"] == "doc-1"


class TestMeasurementRecord:
    def test_a_first_evidence_ref_set_mid_command_is_carried_forward(
        self, registry: StoreRegistry
    ) -> None:
        registry.knowledge.document_store.put("doc-1", "body", {})
        executor = build_curate_executor(registry, clearance="restricted")
        subject = _subject(registry)
        executor.execute(
            Command(
                operation=Operation.MEASUREMENT_RECORD,
                args={"measurement": _measurement(subject)},
            )
        )
        remeasured = Command(
            operation=Operation.MEASUREMENT_RECORD,
            args={"measurement": _measurement(subject, unit="rows")},
        )

        with rival_inside_first_read(
            registry, executor, "meas-1", _set_evidence_ref("meas-1", "doc-1")
        ) as rival:
            loser = executor.execute(remeasured)

        [committed] = rival
        assert committed.status == CommandStatus.SUCCESS
        assert loser.status == CommandStatus.SUCCESS
        props = _props(registry, "meas-1")
        assert props["unit"] == "rows"
        assert props["evidence_ref"] == "doc-1"
        node = registry.knowledge.graph_store.get_node("meas-1")
        assert node is not None
        assert node["node_type"] == MEASUREMENT
