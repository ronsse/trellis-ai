"""``save_experience`` refuses a trace nested past the bound, and says so (#1186).

Two paths reach the refusal. A trace the JSON parser can still read passes
validation and is refused by the store on the way in; one nested past the
parser's own limit is refused by validation, and its hint must not blame the
JSON syntax.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.stores.base.trace import MAX_TRACE_PAYLOAD_DEPTH
from trellis.stores.registry import StoreRegistry

save_experience = unwrap_tool(server_mod.save_experience)


def _trace(levels: int) -> str:
    result: dict[str, Any] = {"leaf": 1}
    for _ in range(levels - 1):
        result = {"k": result}
    return json.dumps(
        {
            "source": "agent",
            "intent": "probe",
            "context": {"agent_id": "a"},
            "steps": [{"step_type": "tool_call", "name": "n", "result": result}],
        }
    )


class TestSaveExperience:
    def test_a_trace_at_the_bound_is_stored_and_listed(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_experience(trace_json=_trace(MAX_TRACE_PAYLOAD_DEPTH))

        assert reply.startswith("Trace saved: ")
        assert len(temp_registry.operational.trace_store.query()) == 1

    def test_a_trace_the_parser_can_read_is_refused_by_the_store_write(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(McpError) as caught:
            save_experience(trace_json=_trace(MAX_TRACE_PAYLOAD_DEPTH + 1))

        assert "failed to store trace" in str(caught.value)
        assert f"more than {MAX_TRACE_PAYLOAD_DEPTH} levels" in str(caught.value)
        assert temp_registry.operational.trace_store.count() == 0

    def test_a_trace_past_the_parser_limit_gets_a_hint_that_names_the_depth(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(McpError) as caught:
            save_experience(trace_json=_trace(250))

        text = str(caught.value)
        assert "invalid trace JSON" in text
        assert f"under {MAX_TRACE_PAYLOAD_DEPTH} levels" in text
        assert "strict JSON" not in text
        assert temp_registry.operational.trace_store.count() == 0
