"""Boundary-rejection telemetry wiring on the MCP write tools.

Every rejection an agent can hit at a write tool's front door must leave
a ``WRITE_REJECTED`` event behind — the recall-gap study found 13 such
rejections in production transcripts with zero backend visibility.
"""

from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

from .conftest import unwrap_tool

save_experience = unwrap_tool(server_mod.save_experience)
save_memory = unwrap_tool(server_mod.save_memory)
save_knowledge = unwrap_tool(server_mod.save_knowledge)
record_feedback = unwrap_tool(server_mod.record_feedback)


def _rejection_events(registry: StoreRegistry) -> list:
    return registry.operational.event_log.get_events(
        event_type=EventType.WRITE_REJECTED
    )


class TestSaveExperienceTelemetry:
    def test_schema_rejection_emits_event_with_hints(
        self, temp_registry: StoreRegistry
    ) -> None:
        bad = (
            '{"source": "agent", "intent": "x",'
            ' "outcome": {"status": "success", "artifacts": ["a"]},'
            ' "context": {"domain": "d"}}'
        )
        with pytest.raises(McpError) as excinfo:
            save_experience(trace_json=bad)

        (event,) = _rejection_events(temp_registry)
        assert event.payload["tool"] == "save_experience"
        rejections = event.payload["rejections"]
        assert any(row["loc"] == "outcome.artifacts" for row in rejections)
        assert any("artifacts_produced" in hint for hint in event.payload["hints"])
        # The raised error teaches the same fix it recorded.
        assert "artifacts_produced" in excinfo.value.error.message

    def test_source_enum_rejection_lists_allowed_values(
        self, temp_registry: StoreRegistry
    ) -> None:
        bad = '{"source": "claude-code", "intent": "x", "context": {"domain": "d"}}'
        with pytest.raises(McpError) as excinfo:
            save_experience(trace_json=bad)
        assert "workflow" in excinfo.value.error.message
        (event,) = _rejection_events(temp_registry)
        assert event.payload["rejections"][0]["kind"] == "enum"

    def test_empty_trace_json_is_recorded(self, temp_registry: StoreRegistry) -> None:
        with pytest.raises(McpError):
            save_experience(trace_json="  ")
        (event,) = _rejection_events(temp_registry)
        assert event.payload["rejections"][0]["kind"] == "empty_required"

    def test_valid_trace_emits_no_rejection(self, temp_registry: StoreRegistry) -> None:
        good = (
            '{"source": "agent", "intent": "works",'
            ' "outcome": {"status": "success", "summary": "ok"},'
            ' "context": {"domain": "d"}}'
        )
        result = save_experience(trace_json=good)
        assert "Trace saved" in result
        assert _rejection_events(temp_registry) == []


class TestOtherWriteTools:
    def test_save_memory_empty_content(self, temp_registry: StoreRegistry) -> None:
        with pytest.raises(McpError):
            save_memory(content="   ")
        (event,) = _rejection_events(temp_registry)
        assert event.payload["tool"] == "save_memory"
        assert event.payload["rejections"][0]["loc"] == "content"

    def test_save_knowledge_dangling_evidence_ref(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(McpError):
            save_knowledge(name="thing", evidence_ref="doc-does-not-exist")
        (event,) = _rejection_events(temp_registry)
        assert event.payload["tool"] == "save_knowledge"
        assert event.payload["rejections"][0]["kind"] == "dangling_reference"

    def test_record_feedback_no_target(self, temp_registry: StoreRegistry) -> None:
        with pytest.raises(McpError):
            record_feedback()
        (event,) = _rejection_events(temp_registry)
        assert event.payload["tool"] == "record_feedback"
        assert event.payload["rejections"][0]["kind"] == "missing"

    def test_record_feedback_bad_rating(self, temp_registry: StoreRegistry) -> None:
        with pytest.raises(McpError):
            record_feedback(trace_id="t1", rating=1.5)
        (event,) = _rejection_events(temp_registry)
        assert event.payload["rejections"][0]["loc"] == "rating"
