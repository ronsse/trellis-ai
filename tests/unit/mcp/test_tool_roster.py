"""The MCP tool roster is the same list, in the same order, with the same schemas.

``server.py`` was split by tool family. FastMCP lists tools in registration
order and clients can see it, so a reordered import in ``tools/__init__.py``
would change the roster without failing any behavioural test. Each tool's
input schema is what a client validates its arguments against, so a signature
that drifted in the move would change it too.

``tool_roster_origin_main.json`` is the roster dumped from the pre-split module
(``tools/list`` through FastMCP's in-memory client). The test compares the
live roster to it: names in order, then each tool's ``inputSchema`` exactly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import trellis.mcp.server as server_mod
from trellis.mcp.auth import set_auth_enforced

_FIXTURE = Path(__file__).with_name("tool_roster_origin_main.json")

#: The roster, in the order FastMCP listed it before the split.
EXPECTED_TOOL_NAMES = [
    "get_context",
    "save_experience",
    "save_knowledge",
    "save_memory",
    "get_lessons",
    "get_graph",
    "get_items",
    "get_file_context",
    "record_feedback",
    "search",
    "get_objective_context",
    "get_task_context",
    "get_sectioned_context",
    "record_observation",
    "query_observations",
    "execute_mutation",
    "list_api_keys",
    "revoke_api_key",
]


@pytest.fixture(autouse=True)
def _allow_in_memory_client() -> None:
    """``Client(server_mod.mcp)`` is not an authenticated transport."""
    set_auth_enforced(enforced=False)


async def _live_roster() -> list[dict[str, Any]]:
    async with Client(server_mod.mcp) as client:
        tools = await client.list_tools()
    return [{"name": tool.name, "inputSchema": tool.inputSchema} for tool in tools]


def _recorded_roster() -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = json.loads(_FIXTURE.read_text())
    return roster


def test_the_recorded_roster_is_not_empty() -> None:
    # A comparison against an empty fixture would pass on an empty server.
    assert [tool["name"] for tool in _recorded_roster()] == EXPECTED_TOOL_NAMES


async def test_tool_names_and_order_match_origin_main() -> None:
    live = await _live_roster()
    assert [tool["name"] for tool in live] == EXPECTED_TOOL_NAMES


async def test_every_input_schema_matches_origin_main() -> None:
    live = {tool["name"]: tool["inputSchema"] for tool in await _live_roster()}
    recorded = {tool["name"]: tool["inputSchema"] for tool in _recorded_roster()}
    assert live.keys() == recorded.keys()
    drifted = sorted(name for name in recorded if live[name] != recorded[name])
    assert not drifted, f"input schema changed for: {drifted}"
