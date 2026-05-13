"""Tests for ObservationRecordHandler + MeasurementRecordHandler.

Item 1 Phase 1 — verifies the governed-pipeline contract for the new
empirical-observation entity types: deep schema validation raises on
missing required fields, idempotent upserts collapse on the same id,
and the audit event fires on success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.errors import ValidationError
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers import (
    MeasurementRecordHandler,
    ObservationRecordHandler,
    create_curate_handlers,
)
from trellis.schemas.measurement import Measurement
from trellis.schemas.observation import Observation
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _seed_subject(registry: StoreRegistry, name: str = "users") -> str:
    """Insert a subject node so the hasObservation edge attaches cleanly."""
    return registry.knowledge.graph_store.upsert_node(
        node_id=None, node_type="Dataset", properties={"name": name}
    )


class TestObservationRecordHandler:
    def test_persists_observation_and_emits_event(
        self, registry: StoreRegistry
    ) -> None:
        subject_id = _seed_subject(registry)
        handler = ObservationRecordHandler(registry)
        obs = Observation(
            subject_entity_id=subject_id,
            subject_entity_type="Dataset",
            observer_agent_id="agent-1",
            content="filter-projection asymmetry on users.email",
            confidence=0.85,
        )
        cmd = Command(operation=Operation.OBSERVATION_RECORD, args={"observation": obs})

        created_id, message = handler.handle(cmd)

        assert created_id == obs.observation_id
        assert obs.observation_id in message
        node = registry.knowledge.graph_store.get_node(obs.observation_id)
        assert node is not None
        assert node["node_type"] == "Observation"
        assert node["properties"]["content"].startswith("filter-projection")

        events = registry.operational.event_log.get_events(
            event_type=EventType.OBSERVATION_RECORDED
        )
        assert any(ev.entity_id == obs.observation_id for ev in events)

    def test_accepts_dict_payload(self, registry: StoreRegistry) -> None:
        subject_id = _seed_subject(registry)
        handler = ObservationRecordHandler(registry)
        body = {
            "subject_entity_id": subject_id,
            "subject_entity_type": "Dataset",
            "observer_agent_id": "agent-1",
            "content": "hello",
            "confidence": 0.5,
        }
        cmd = Command(
            operation=Operation.OBSERVATION_RECORD, args={"observation": body}
        )
        created_id, _ = handler.handle(cmd)
        assert created_id is not None
        assert registry.knowledge.graph_store.get_node(created_id) is not None

    def test_missing_required_field_raises_validation_error(
        self, registry: StoreRegistry
    ) -> None:
        """No silent defaults: missing required fields raise loudly."""
        handler = ObservationRecordHandler(registry)
        # Missing `content` and `confidence` — both required.
        body = {
            "subject_entity_id": "ds-1",
            "subject_entity_type": "Dataset",
            "observer_agent_id": "agent-1",
        }
        cmd = Command(
            operation=Operation.OBSERVATION_RECORD, args={"observation": body}
        )
        with pytest.raises(ValidationError):
            handler.handle(cmd)

    def test_idempotent_on_repeat_id(self, registry: StoreRegistry) -> None:
        """Repeating the same observation_id is a no-op upsert."""
        subject_id = _seed_subject(registry)
        handler = ObservationRecordHandler(registry)
        obs = Observation(
            observation_id="obs-fixed-1",
            subject_entity_id=subject_id,
            subject_entity_type="Dataset",
            observer_agent_id="agent-1",
            content="x",
            confidence=0.5,
        )
        cmd = Command(operation=Operation.OBSERVATION_RECORD, args={"observation": obs})

        first_id, _ = handler.handle(cmd)
        second_id, _ = handler.handle(cmd)

        assert first_id == second_id == "obs-fixed-1"
        # Same logical id, regardless of version churn.
        node = registry.knowledge.graph_store.get_node("obs-fixed-1")
        assert node is not None

    def test_registered_in_curate_handler_factory(
        self, registry: StoreRegistry
    ) -> None:
        handlers = create_curate_handlers(registry)
        assert Operation.OBSERVATION_RECORD in handlers
        assert Operation.MEASUREMENT_RECORD in handlers


class TestMeasurementRecordHandler:
    def test_persists_measurement_and_emits_event(
        self, registry: StoreRegistry
    ) -> None:
        subject_id = _seed_subject(registry)
        handler = MeasurementRecordHandler(registry)
        meas = Measurement(
            subject_entity_id=subject_id,
            subject_entity_type="Dataset",
            metric_name="null_rate",
            metric_value=0.03,
            unit="percent",
            observer_agent_id="agent-2",
        )
        cmd = Command(
            operation=Operation.MEASUREMENT_RECORD, args={"measurement": meas}
        )

        created_id, _ = handler.handle(cmd)
        assert created_id == meas.measurement_id

        node = registry.knowledge.graph_store.get_node(meas.measurement_id)
        assert node is not None
        assert node["node_type"] == "Measurement"
        assert node["properties"]["metric_name"] == "null_rate"
        assert node["properties"]["metric_value"] == pytest.approx(0.03)

        events = registry.operational.event_log.get_events(
            event_type=EventType.MEASUREMENT_RECORDED
        )
        assert any(ev.entity_id == meas.measurement_id for ev in events)

    def test_missing_required_field_raises(self, registry: StoreRegistry) -> None:
        handler = MeasurementRecordHandler(registry)
        body = {
            "subject_entity_id": "ds-1",
            # subject_entity_type, metric_name, metric_value, observer_agent_id missing
        }
        cmd = Command(
            operation=Operation.MEASUREMENT_RECORD, args={"measurement": body}
        )
        with pytest.raises(ValidationError):
            handler.handle(cmd)
