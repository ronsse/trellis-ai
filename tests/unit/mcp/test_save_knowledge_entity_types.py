"""``save_knowledge`` shows the well-known entity types where a type is picked.

The ``entity_type`` parameter description is rendered from
``ENTITY_TYPE_DESCRIPTIONS``, so these tests read it the way a client does
(``tools/list`` through FastMCP's in-memory client), not from the source. An
unknown type is still stored exactly as given; the reply only gains a note.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.auth import set_auth_enforced
from trellis.schemas import well_known as wk
from trellis.stores.registry import StoreRegistry

save_knowledge = unwrap_tool(server_mod.save_knowledge)


async def _listed_save_knowledge() -> Any:
    set_auth_enforced(enforced=False)
    try:
        async with Client(server_mod.mcp) as client:
            tools = await client.list_tools()
    finally:
        set_auth_enforced(enforced=True)
    return next(tool for tool in tools if tool.name == "save_knowledge")


async def test_entity_type_description_lists_every_canonical_type() -> None:
    tool = await _listed_save_knowledge()
    text = tool.inputSchema["properties"]["entity_type"]["description"]
    for name, sentence in wk.ENTITY_TYPE_DESCRIPTIONS.items():
        assert f"- {name}: {sentence}" in text, name
    assert sorted(wk.ENTITY_TYPE_DESCRIPTIONS) == sorted(wk.CANONICAL_ENTITY_TYPES)
    # The slot is filled, and the non-canonical "system" example is gone.
    assert "{well_known_entity_types}" not in text
    assert '"system"' not in text
    assert "{well_known_entity_types}" not in (tool.description or "")


def _node_type(registry: StoreRegistry, reply: str) -> str:
    node_id = reply.split("Entity created: ", 1)[1].split(" ", 1)[0]
    node = registry.knowledge.graph_store.get_node(node_id)
    assert node is not None
    return str(node["node_type"])


@pytest.mark.parametrize("entity_type", ["concept", "Person", "service"])
def test_known_type_reply_has_no_note(
    temp_registry: StoreRegistry, entity_type: str
) -> None:
    reply = save_knowledge(name=f"thing-{entity_type}", entity_type=entity_type)
    assert len(reply.splitlines()) == 1
    assert reply.startswith("Entity created: ")
    assert reply.endswith(f"({entity_type}: thing-{entity_type})")
    assert "Note: entity_type" not in reply


def test_unknown_type_is_kept_and_lists_the_well_known_types(
    temp_registry: StoreRegistry,
) -> None:
    reply = save_knowledge(name="restart the queue", entity_type="runbook")
    lines = reply.splitlines()
    assert lines[0].endswith("(runbook: restart the queue)")
    assert lines[1] == (
        "Note: entity_type 'runbook' is not a well-known type; kept as given. "
        f"Well-known types: {', '.join(wk.ENTITY_TYPE_DESCRIPTIONS)}."
    )
    assert _node_type(temp_registry, reply) == "runbook"


@pytest.mark.parametrize(
    ("given", "nearest"),
    [
        ("organization", "Organization"),
        ("Organisation", "Organization"),
        ("software_application", "SoftwareApplication"),
        ("persons", "Person"),
    ],
)
def test_unknown_type_names_the_nearest_obvious_match(
    temp_registry: StoreRegistry, given: str, nearest: str
) -> None:
    reply = save_knowledge(name=f"x-{given}", entity_type=given)
    assert reply.splitlines()[1] == (
        f"Note: entity_type '{given}' is not a well-known type; kept as given. "
        f"Nearest well-known type: {nearest}."
    )
    # Never rewritten: the stored type is the caller's string.
    assert _node_type(temp_registry, reply) == given
