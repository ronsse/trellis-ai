"""A tool call with the wrong arguments is explained and, for a write, recorded.

FastMCP validates arguments before a tool body runs, so a misspelt or
mistyped argument never reaches the body's own handling. It came back as
pydantic's text (no accepted parameter named, the caller's value quoted) and
left no ``WRITE_REJECTED`` event, so the write-health report could not see
the rejection. ``_ExplainArgumentRejections`` rewrites that one failure from
the tool's own schema.

These drive the real dispatch through an in-memory ``fastmcp.Client``: the
failure exists only inside FastMCP's call path, so a direct call to the tool
function cannot reach it.

The call is rejected either way. What these pin is the message, the event,
and that nothing was loosened to produce them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client

import trellis.mcp.server as server_mod
from trellis.mcp.auth import set_auth_enforced
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

SECRET = "synthetic-argument-value-4f9q"  # noqa: S105 — synthetic, not a credential


@pytest.fixture(autouse=True)
def _allow_in_memory_client() -> None:
    """``Client(server_mod.mcp)`` is not an authenticated transport."""
    set_auth_enforced(enforced=False)


async def _call(name: str, args: dict[str, Any]) -> Any:
    async with Client(server_mod.mcp) as client:
        return await client.call_tool(name, args, raise_on_error=False)


def _text(result: Any) -> str:
    return str(result.content[0].text)


def _rejections(registry: StoreRegistry, tool: str) -> list[Any]:
    return [
        event
        for event in registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=50
        )
        if event.source == f"mcp:{tool}"
    ]


class TestMessage:
    async def test_an_unknown_argument_names_what_the_tool_accepts(self) -> None:
        result = await _call("record_feedback", {"pack_id": "p", "item_ids": [SECRET]})
        text = _text(result)
        assert result.is_error
        assert "unknown argument item_ids" in text
        accepted = (await server_mod.mcp.get_tool("record_feedback")).parameters[
            "properties"
        ]
        assert f"record_feedback accepts [{', '.join(accepted)}]" in text
        assert "did you mean helpful_item_ids, unhelpful_item_ids or " in text

    async def test_the_message_quotes_nothing_the_caller_sent(self) -> None:
        text = _text(
            await _call("record_feedback", {"pack_id": "p", "item_ids": [SECRET]})
        )
        assert SECRET not in text
        # pydantic's own rendering, which is what this replaces.
        assert "input_value" not in text
        assert "pydantic" not in text

    async def test_a_dict_passed_as_trace_json_is_told_to_pass_a_string(
        self,
    ) -> None:
        result = await _call(
            "save_experience", {"trace_json": {"source": "agent", "intent": SECRET}}
        )
        text = _text(result)
        assert result.is_error
        assert "trace_json must be a JSON string" in text
        assert SECRET not in text

    async def test_a_missing_required_argument_lists_what_is_required(self) -> None:
        text = _text(await _call("save_experience", {}))
        assert "missing required argument trace_json" in text
        assert "required: [trace_json]" in text

    async def test_a_read_tool_gets_the_message_too(self) -> None:
        text = _text(await _call("get_context", {"query": SECRET}))
        assert "unknown argument query" in text
        assert "get_context accepts [intent, " in text
        assert SECRET not in text


class TestStillRejected:
    """Guidance must not turn a wrong call into a working one."""

    async def test_a_misspelt_argument_is_not_accepted_as_an_alias(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = await _call(
            "record_feedback", {"pack_id": "p", "rating": 0.9, "item_ids": ["a"]}
        )
        assert result.is_error
        assert "unknown argument item_ids" in _text(result)

    async def test_an_object_is_not_coerced_into_a_trace(
        self, temp_registry: StoreRegistry
    ) -> None:
        valid = {"source": "agent", "intent": "x", "context": {"domain": "d"}}
        result = await _call("save_experience", {"trace_json": valid})
        assert result.is_error
        assert temp_registry.operational.trace_store.query(limit=5) == []

    async def test_a_correct_call_is_unaffected(
        self, temp_registry: StoreRegistry
    ) -> None:
        valid = {"source": "agent", "intent": "x", "context": {"domain": "d"}}
        result = await _call("save_experience", {"trace_json": json.dumps(valid)})
        assert not result.is_error
        assert _text(result).startswith("Trace saved:")
        assert _rejections(temp_registry, "save_experience") == []


class TestEvent:
    async def test_a_write_tool_emits_write_rejected(
        self, temp_registry: StoreRegistry
    ) -> None:
        await _call("record_feedback", {"pack_id": "p", "item_ids": [SECRET]})

        (event,) = _rejections(temp_registry, "record_feedback")
        rows = event.payload["rejections"]
        assert {"kind": "extra_forbidden", "loc": "item_ids"} in [
            {k: v for k, v in row.items() if k != "msg"} for row in rows
        ]
        assert any("did you mean helpful_item_ids" in h for h in event.payload["hints"])
        assert SECRET not in json.dumps(event.payload)

    async def test_a_dict_trace_json_is_recorded_as_a_type_rejection(
        self, temp_registry: StoreRegistry
    ) -> None:
        await _call("save_experience", {"trace_json": {"source": "agent"}})

        (event,) = _rejections(temp_registry, "save_experience")
        assert [
            {k: v for k, v in row.items() if k != "msg"}
            for row in event.payload["rejections"]
        ] == [{"kind": "type", "loc": "trace_json"}]

    async def test_a_read_tool_emits_no_write_event(
        self, temp_registry: StoreRegistry
    ) -> None:
        await _call("get_context", {"query": SECRET})

        events = temp_registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=50
        )
        assert list(events) == []

    async def test_a_failing_event_log_still_returns_the_guidance(
        self, temp_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(**_: Any) -> None:
            raise RuntimeError(SECRET)

        monkeypatch.setattr(server_mod, "_record_boundary_rejection", _boom)
        result = await _call("record_feedback", {"pack_id": "p", "item_ids": ["a"]})
        text = _text(result)
        assert result.is_error
        # The original failure is returned unchanged: guidance is advisory,
        # and a telemetry fault must not alter what the caller is told.
        assert SECRET not in text


class TestRecordedToolRoster:
    async def test_every_write_tool_is_recorded_or_named_as_left_out(self) -> None:
        """A write tool added later fails here until someone decides.

        Classed by each tool's declared scope, not from memory. The ones left
        out are named with the reason in ``_RECORDED_ARGUMENT_TOOLS``; the
        two API-key tools are admin-scoped and record no boundary rejection.
        """
        tools = await server_mod.mcp.list_tools()
        assert tools, "no tools registered"

        def _scope(tool: Any) -> str:
            checks = tool.auth if isinstance(tool.auth, list) else [tool.auth]
            names = [getattr(check, "__name__", "") for check in checks]
            (scoped,) = (n for n in names if n.startswith("require_scope_"))
            return scoped.removeprefix("require_scope_")

        writers = {tool.name for tool in tools if _scope(tool) != "read"}
        recorded = server_mod._RECORDED_ARGUMENT_TOOLS
        assert recorded <= writers
        assert writers - recorded == {
            "record_observation",
            "execute_mutation",
            "list_api_keys",
            "revoke_api_key",
        }

    async def test_a_write_tool_left_out_gets_the_message_and_no_event(
        self, temp_registry: StoreRegistry
    ) -> None:
        result = await _call("execute_mutation", {"commands": SECRET})
        assert result.is_error
        assert "unknown argument commands" in _text(result)
        assert _rejections(temp_registry, "execute_mutation") == []
