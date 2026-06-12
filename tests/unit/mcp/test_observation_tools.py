"""Tests for the ``record_observation`` + ``query_observations`` MCP tools.

Item 1 Phase 1. Uses the conftest fixtures (``temp_registry``,
``_suppress_structlog``) shared with the rest of the MCP suite.
"""

from __future__ import annotations

import json

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import (
    query_observations as _query_observations,
)
from trellis.mcp.server import (
    record_observation as _record_observation,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

record_observation = unwrap_tool(_record_observation)
query_observations = unwrap_tool(_query_observations)


class TestRecordObservationTool:
    def test_happy_path_returns_observation_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        subject_id = temp_registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="Dataset", properties={"name": "users"}
        )
        raw = record_observation(
            subject_entity_id=subject_id,
            subject_entity_type="Dataset",
            observer_agent_id="agent-1",
            content="filter-projection asymmetry",
            confidence=0.85,
        )
        payload = json.loads(raw)
        assert payload["status"] == "ok"
        assert payload["observation_id"]

        # Audit event emitted.
        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.OBSERVATION_RECORDED
        )
        assert any(ev.entity_id == payload["observation_id"] for ev in events)

    def test_invalid_confidence_returns_error_envelope(
        self, temp_registry: StoreRegistry
    ) -> None:
        """confidence > 1.0 violates the Pydantic constraint — no silent
        clamp; the tool returns a structured error envelope."""
        raw = record_observation(
            subject_entity_id="ds-1",
            subject_entity_type="Dataset",
            observer_agent_id="agent-1",
            content="x",
            confidence=2.0,  # out of [0, 1]
        )
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "Invalid observation" in payload["message"]


class TestQueryObservationsTool:
    def test_returns_recorded_observations(self, temp_registry: StoreRegistry) -> None:
        """SDK→MCP query consistency: the tool sees what the handler wrote."""
        subject_id = temp_registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="Dataset", properties={"name": "users"}
        )
        raw = record_observation(
            subject_entity_id=subject_id,
            subject_entity_type="Dataset",
            observer_agent_id="agent-1",
            content="a",
            confidence=0.5,
        )
        observation_id = json.loads(raw)["observation_id"]

        result = json.loads(query_observations(subject_entity_id=subject_id))
        assert result["status"] == "ok"
        assert len(result["observations"]) == 1
        assert result["observations"][0]["observation_id"] == observation_id

    def test_filter_by_observer(self, temp_registry: StoreRegistry) -> None:
        subject_id = temp_registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="Dataset", properties={"name": "users"}
        )
        for observer in ("agent-a", "agent-b"):
            record_observation(
                subject_entity_id=subject_id,
                subject_entity_type="Dataset",
                observer_agent_id=observer,
                content="x",
                confidence=0.5,
            )

        result = json.loads(query_observations(observer_agent_id="agent-a"))
        assert result["status"] == "ok"
        assert len(result["observations"]) == 1
        assert result["observations"][0]["observer_agent_id"] == "agent-a"
