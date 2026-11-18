"""MCP ``get_graph`` above depth 1 drops what only a withheld node reaches (#1264).

A node labelled above the caller is left out of the neighbourhood with its
edges (#1029). A node whose every path from the root runs through it is now
left out too; one also reached by a path of readable nodes is still served. A
cleared caller, a stdio (unauthenticated) caller and a graph with no labelled
node read exactly what they read before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_graph = unwrap_tool(server_mod.get_graph)

ROOT = "entity://root"
GATE = "entity://gate"
FAR = "entity://far"
SIDE = "entity://side"
FAR_NAME = "Distant Pear Words"
GATE_NAME = "Gate Quince Words"
BELOW = ["read"]
CLEARED = ["read", "restricted"]


def _seed(registry: StoreRegistry, *, label: bool, diamond: bool = False) -> None:
    graph = registry.knowledge.graph_store
    gate: dict[str, Any] = {"name": GATE_NAME}
    if label:
        gate[DATA_CLASSIFICATION_KEY] = {"sensitivity": "restricted"}
    graph.upsert_node(ROOT, "entity", {"name": "Root Apple Words"})
    graph.upsert_node(GATE, "entity", gate)
    graph.upsert_node(FAR, "entity", {"name": FAR_NAME})
    graph.upsert_edge(ROOT, GATE, "feeds_into")
    graph.upsert_edge(GATE, FAR, "feeds_into")
    if diamond:
        graph.upsert_node(SIDE, "entity", {"name": "Side Plum Words"})
        graph.upsert_edge(ROOT, SIDE, "reads_from")
        graph.upsert_edge(SIDE, FAR, "reads_from")


def _graph(scopes: list[str] | None, depth: int = 2) -> str:
    if scopes is None:
        with as_stdio():
            return str(get_graph(ROOT, depth=depth))
    with as_http(scopes):
        return str(get_graph(ROOT, depth=depth))


@pytest.fixture
def chain(temp_registry: StoreRegistry) -> StoreRegistry:
    _seed(temp_registry, label=True)
    return temp_registry


def test_chain_far_node_is_not_served_below_the_label(chain) -> None:
    below = _graph(BELOW)
    cleared = _graph(CLEARED)

    assert FAR_NAME in cleared
    assert GATE_NAME in cleared
    assert FAR_NAME not in below
    assert FAR not in below
    assert GATE not in below


def test_diamond_far_node_is_served_through_the_readable_path(
    temp_registry: StoreRegistry,
) -> None:
    _seed(temp_registry, label=True, diamond=True)
    below = _graph(BELOW)

    assert FAR_NAME in below
    assert "Side Plum Words" in below
    assert GATE not in below
    assert GATE_NAME not in below
    assert "feeds_into" not in below, "every feeds_into edge touches the gate"
    assert "reads_from" in below


def test_unchanged_for_a_cleared_or_stdio_caller_and_an_unlabelled_graph(
    temp_registry: StoreRegistry,
) -> None:
    _seed(temp_registry, label=False, diamond=True)
    unlabelled_below = _graph(BELOW)
    assert FAR_NAME in unlabelled_below
    assert GATE_NAME in unlabelled_below, "an empty answer proves nothing"
    assert unlabelled_below == _graph(CLEARED) == _graph(None)

    # Label the gate: a cleared and a stdio caller still read the whole graph.
    temp_registry.knowledge.graph_store.upsert_node(
        GATE,
        "entity",
        {
            "name": GATE_NAME,
            DATA_CLASSIFICATION_KEY: {"sensitivity": "restricted"},
        },
    )
    cleared = _graph(CLEARED)
    assert GATE_NAME in cleared
    assert cleared == _graph(None)
