"""The MCP graph doors withhold a node labelled above the caller (#1029).

A node's own ``properties["data_classification"]`` already withholds it from a
pack (``tests/unit/retrieve/test_node_label_pack.py``). The direct doors that
ask :meth:`~trellis.retrieve.precedents.PrecedentClearance.verdict_of` about a
node (``get_graph``, ``get_items``, ``get_file_context``) now read the same
label: a node above the caller's clearance answers as not found, and as a
neighbour it is left out with every edge that touches it. A node without the
key is never judged there, so a store with no labelled node answers exactly as
it did before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from mcp.shared.exceptions import MCPError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http
from trellis.retrieve.precedents import (
    PRECEDENT_WITHHELD_LABELLED,
    PRECEDENT_WITHHELD_UNREADABLE,
    PrecedentClearance,
)
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_graph = unwrap_tool(server_mod.get_graph)
get_items = unwrap_tool(server_mod.get_items)
get_file_context = unwrap_tool(server_mod.get_file_context)

LABELLED = "entity://ledger"
PLAIN = "entity://plain"
ANCHOR = "doc-anchor"
PATH = "svc/a.py"
#: The text that shows the labelled node was served.
LEDGER = "Ledger Secret Words"
PLAIN_NAME = "Plain Table Words"
EDGE = "feeds_into"
#: A caller below the label, and one cleared for it.
BELOW = ["read"]
CLEARED = ["read", "restricted"]


def _seed(registry: StoreRegistry, *, label: bool) -> None:
    """Two nodes joined by an edge, both anchored to an open document at PATH."""
    docs = registry.knowledge.document_store
    graph = registry.knowledge.graph_store
    docs.put(ANCHOR, "an anchor note", {"title": "Anchor", "source_path": PATH})
    ledger_props: dict[str, Any] = {"name": LEDGER, "description": "ledger rows"}
    if label:
        ledger_props[DATA_CLASSIFICATION_KEY] = {"sensitivity": "restricted"}
    graph.upsert_node(LABELLED, "entity", ledger_props, document_ids=[ANCHOR])
    graph.upsert_node(
        PLAIN,
        "entity",
        {"name": PLAIN_NAME, "description": "plain rows"},
        document_ids=[ANCHOR],
    )
    graph.upsert_edge(PLAIN, LABELLED, EDGE)


@pytest.fixture
def labelled(temp_registry: StoreRegistry) -> StoreRegistry:
    _seed(temp_registry, label=True)
    node = temp_registry.knowledge.graph_store.get_node(LABELLED)
    assert node is not None
    assert DATA_CLASSIFICATION_KEY in node["properties"], "the premise"
    return temp_registry


@pytest.fixture
def unlabelled(temp_registry: StoreRegistry) -> StoreRegistry:
    _seed(temp_registry, label=False)
    return temp_registry


def _graph(entity_id: str, scopes: list[str]) -> str:
    with as_http(scopes):
        try:
            return str(get_graph(entity_id))
        except MCPError as exc:
            return f"MCP-ERROR {exc}"


def test_get_graph_withholds_a_node_labelled_above_the_caller(
    labelled: StoreRegistry,
) -> None:
    root_below = _graph(LABELLED, BELOW)
    neighbour_below = _graph(PLAIN, BELOW)
    root_cleared = _graph(LABELLED, CLEARED)
    neighbour_cleared = _graph(PLAIN, CLEARED)

    # Below the label: the labelled root is the not-found a missing id gets ...
    assert root_below.startswith("MCP-ERROR"), root_below
    assert "entity not found" in root_below
    assert LEDGER not in root_below
    # ... and as a neighbour it is left out with its edge; the root still serves.
    assert PLAIN_NAME in neighbour_below
    assert LEDGER not in neighbour_below
    assert EDGE not in neighbour_below, "the edge goes with the node"
    # Cleared for it: both are served whole.
    assert LEDGER in root_cleared
    assert LEDGER in neighbour_cleared
    assert EDGE in neighbour_cleared


def test_a_withheld_root_answers_as_a_missing_one(labelled: StoreRegistry) -> None:
    missing = _graph("entity://nothing-here", BELOW)
    assert _graph(LABELLED, BELOW).replace(LABELLED, "X") == missing.replace(
        "entity://nothing-here", "X"
    )


def test_get_items_withholds_a_labelled_node(labelled: StoreRegistry) -> None:
    with as_http(BELOW):
        withheld = str(get_items([LABELLED, PLAIN]))
        missing = str(get_items(["entity://nothing-here"]))
    with as_http(CLEARED):
        cleared = str(get_items([LABELLED]))

    assert LEDGER not in withheld
    assert PLAIN_NAME in withheld, "other ids in the same call are still served"
    assert "not found" in missing
    assert LABELLED in withheld
    assert "not found" in withheld
    assert LEDGER in cleared


def test_get_file_context_leaves_out_a_labelled_node(labelled: StoreRegistry) -> None:
    with as_http(BELOW):
        below = str(get_file_context([PATH]))
    with as_http(CLEARED):
        cleared = str(get_file_context([PATH]))

    assert LEDGER in cleared
    assert PLAIN_NAME in cleared
    assert LEDGER not in below
    assert PLAIN_NAME in below
    assert ANCHOR in below


def test_an_unlabelled_store_answers_the_same_at_every_clearance(
    unlabelled: StoreRegistry,
) -> None:
    for entity_id in (LABELLED, PLAIN):
        below = _graph(entity_id, BELOW)
        assert LEDGER in below, "an empty answer proves nothing"
        assert below == _graph(entity_id, CLEARED)


class TestVerdictOf:
    """The predicate itself, past the doors."""

    @staticmethod
    def _node(props: Any) -> dict[str, Any]:
        return {"node_id": LABELLED, "node_type": "entity", "properties": props}

    @staticmethod
    def _gate(registry: StoreRegistry, clearance: str) -> PrecedentClearance:
        return PrecedentClearance(
            registry.knowledge.document_store,
            clearance,  # type: ignore[arg-type]
            registry.knowledge.graph_store,
        )

    def test_an_id_only_verdict_is_not_memoised_over_a_labelled_node(
        self, temp_registry: StoreRegistry
    ) -> None:
        gate = self._gate(temp_registry, "internal")
        labelled = self._node({DATA_CLASSIFICATION_KEY: {"sensitivity": "restricted"}})

        assert gate.verdict_of(LABELLED) is None
        assert gate.verdict_of(LABELLED, labelled) == PRECEDENT_WITHHELD_LABELLED

    @pytest.mark.parametrize(
        ("clearance", "sensitivity", "expected"),
        [
            ("internal", "restricted", PRECEDENT_WITHHELD_LABELLED),
            ("internal", "confidential", PRECEDENT_WITHHELD_LABELLED),
            ("internal", "internal", None),
            ("internal", "public", None),
            ("confidential", "confidential", None),
            ("confidential", "restricted", PRECEDENT_WITHHELD_LABELLED),
            ("restricted", "restricted", None),
        ],
    )
    def test_a_label_is_compared_with_the_clearance(
        self,
        temp_registry: StoreRegistry,
        clearance: str,
        sensitivity: str,
        expected: str | None,
    ) -> None:
        node = self._node({DATA_CLASSIFICATION_KEY: {"sensitivity": sensitivity}})
        assert self._gate(temp_registry, clearance).verdict_of(LABELLED, node) == (
            expected
        )

    def test_a_malformed_label_reads_as_restricted(
        self, temp_registry: StoreRegistry
    ) -> None:
        node = self._node({DATA_CLASSIFICATION_KEY: "secret-ish"})
        gate = self._gate(temp_registry, "confidential")
        assert gate.verdict_of(LABELLED, node) == PRECEDENT_WITHHELD_LABELLED

    def test_a_node_without_the_key_is_not_judged(
        self, temp_registry: StoreRegistry
    ) -> None:
        node = self._node({"name": "x"})
        assert self._gate(temp_registry, "internal").verdict_of(LABELLED, node) is None

    def test_properties_that_are_not_a_mapping_are_unreadable(
        self, temp_registry: StoreRegistry
    ) -> None:
        gate = self._gate(temp_registry, "internal")
        assert gate.verdict_of(LABELLED, self._node("not-a-mapping")) == (
            PRECEDENT_WITHHELD_UNREADABLE
        )
        assert (
            self._gate(temp_registry, "restricted").verdict_of(
                LABELLED, self._node("not-a-mapping")
            )
            is None
        )
