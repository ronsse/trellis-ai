"""A store/driver exception that escapes ``executor.execute`` unwrapped must
not reach the calling agent's response verbatim — the F2 shape from the
#748 gate (``gates/748.md`` follow-up 1). #748 fixed ``execute_mutation``'s
own ``executor.execute`` wrapper; this pins the same fix at the remaining
caller-facing sites in ``src/trellis/mcp/server.py`` where a store or driver
error can reach the text, and proves a caller-input site left alone still
returns its detail.

The forced exception below stands in for an unwrapped driver error (a
Postgres DETAIL line, a Neo4j constraint message): it is not a
``TrellisError`` and not one of ``executor._UNEXPECTED_HANDLER_FAILURE``'s
named types, so it propagates out of ``execute()`` exactly as a real one
would, carrying a synthetic secret in its message.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.shared.exceptions import McpError

import trellis.mcp.server as server_mod
from trellis.stores.registry import StoreRegistry

from .conftest import unwrap_tool

save_memory = unwrap_tool(server_mod.save_memory)
record_observation = unwrap_tool(server_mod.record_observation)

SECRET = "synthetic-g748-secret-qx91"  # noqa: S105 — synthetic, not a real credential
DRIVER_TEXT = f"DETAIL: Key (name)=({SECRET}) already exists."


class _FakeDriverError(Exception):
    """Stands in for an unwrapped driver exception: not a ``TrellisError``,
    not in ``executor._UNEXPECTED_HANDLER_FAILURE`` — escapes raw."""


class _ExplodingExecutor:
    def execute(self, command: Any) -> Any:
        raise _FakeDriverError(DRIVER_TEXT)


def _force_executor_explosion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server_mod,
        "build_curate_executor",
        lambda *args, **kwargs: _ExplodingExecutor(),
    )


class TestExecutorEscapeIsSanitized:
    """Re-measurement + regression pin for the two named F2 sites."""

    def test_save_memory_drops_the_secret_but_names_the_failure(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_executor_explosion(monkeypatch)
        with pytest.raises(McpError) as excinfo:
            save_memory(content="whatever content")
        message = excinfo.value.error.message
        data = json.dumps(excinfo.value.error.data)
        assert SECRET not in message
        assert SECRET not in data
        assert "governed memory write failed" in message

    def test_record_observation_drops_the_secret_but_names_the_failure(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _force_executor_explosion(monkeypatch)
        raw = record_observation(
            subject_entity_id="e1",
            subject_entity_type="service",
            observer_agent_id="agent-1",
            content="observed something",
            confidence=0.9,
        )
        assert SECRET not in raw
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "Execution failed" in payload["message"]


class TestCallerInputSiteKeepsItsDetail:
    """A control: a site left alone because the exception is the caller's
    own input must still return the detail they need to fix their call."""

    def test_record_observation_invalid_confidence_returns_the_constraint(
        self, temp_registry: StoreRegistry
    ) -> None:
        raw = record_observation(
            subject_entity_id="e1",
            subject_entity_type="service",
            observer_agent_id="agent-1",
            content="observed something",
            confidence=5.0,
        )
        payload = json.loads(raw)
        assert payload["status"] == "error"
        assert "confidence" in payload["message"]
        assert "Invalid observation" in payload["message"]
