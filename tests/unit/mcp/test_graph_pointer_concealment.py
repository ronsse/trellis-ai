"""MCP graph reads name no document the caller cannot read.

``get_graph`` (the root and every neighbour), ``get_items`` on an entity id and
``get_file_context`` serve a node whole, since a node carries no label (#1036).
What they drop is a pointer, in ``document_ids`` or the ``evidence_ref``
property, naming a document above the caller's clearance.

Ambient state is installed as ``test_sensitivity_surfaces.py`` does it; the
tools are called unwrapped, so only the clearance decides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import (
    ADMIN,
    READ,
    RESTRICTED,
    as_http,
    as_stdio,
)
from tests.unit.retrieve.test_sensitivity import label
from trellis.ingest_corpus.models import chunk_doc_id

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_graph = unwrap_tool(server_mod.get_graph)
get_items = unwrap_tool(server_mod.get_items)
get_file_context = unwrap_tool(server_mod.get_file_context)

HIDDEN = "doc-hidden"
OPEN = "doc-open"
OTHER_OPEN = "doc-other-open"
ROOT = "node-root"
NEIGHBOUR = "node-neighbour"


@pytest.fixture
def seeded(temp_registry: StoreRegistry) -> StoreRegistry:
    docs = temp_registry.knowledge.document_store
    docs.put(OPEN, "open body", {"source_path": "svc/open.py"})
    docs.put(OTHER_OPEN, "other open body", {})
    docs.put(
        HIDDEN, "hidden body", {"source_path": "svc/hidden.py", **label("restricted")}
    )
    graph = temp_registry.knowledge.graph_store
    # ``save_knowledge`` writes the same id to both carriers.
    graph.upsert_node(
        ROOT,
        "entity",
        {"name": "root claim", "evidence_ref": HIDDEN, "note": "kept"},
        document_ids=[OPEN, HIDDEN],
    )
    graph.upsert_node(
        NEIGHBOUR,
        "entity",
        {"name": "neighbour claim"},
        document_ids=[HIDDEN, OTHER_OPEN],
    )
    graph.upsert_edge(ROOT, NEIGHBOUR, "related_to")
    return temp_registry


class TestGetGraph:
    def test_a_read_token_sees_the_hidden_id_on_neither_root_nor_neighbour(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(READ):
            result = get_graph(ROOT)
        assert HIDDEN not in result
        assert "root claim" in result
        assert "neighbour claim" in result
        assert "note" in result, "an unrelated property is untouched"

    def test_the_readable_pointers_are_still_named(self, seeded: StoreRegistry) -> None:
        with as_http(READ):
            result = get_graph(ROOT)
        assert f"`{OPEN}`" in result
        assert f"`{OTHER_OPEN}`" in result

    def test_a_neighbour_viewed_as_the_root_is_concealed_too(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(READ):
            result = get_graph(NEIGHBOUR)
        assert HIDDEN not in result
        assert f"`{OTHER_OPEN}`" in result

    @pytest.mark.parametrize(
        "caller", [as_http(ADMIN), as_http(RESTRICTED), as_stdio()]
    )
    def test_a_restricted_caller_is_served_unchanged(
        self, seeded: StoreRegistry, caller: Any
    ) -> None:
        with caller:
            result = get_graph(ROOT)
        assert f"`{HIDDEN}`" in result
        assert f"- **evidence_ref**: {HIDDEN}" in result
        assert f"`{OPEN}`" in result

    def test_the_hidden_id_is_judged_once_for_the_whole_response(
        self, seeded: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = seeded.knowledge.document_store
        real_get = store.get
        reads: list[str] = []

        def counting(doc_id: str) -> Any:
            reads.append(doc_id)
            return real_get(doc_id)

        monkeypatch.setattr(store, "get", counting)
        with as_http(READ):
            get_graph(ROOT)
        assert reads.count(HIDDEN) == 1
        assert sorted(set(reads)) == sorted({HIDDEN, OPEN, OTHER_OPEN})

    def test_the_stored_node_is_not_mutated(self, seeded: StoreRegistry) -> None:
        with as_http(READ):
            get_graph(ROOT)
        stored = seeded.knowledge.graph_store.get_node(ROOT)
        assert stored["document_ids"] == [OPEN, HIDDEN]
        assert stored["properties"]["evidence_ref"] == HIDDEN

    def test_a_chunk_pointer_whose_parent_is_hidden_is_hidden(
        self, seeded: StoreRegistry
    ) -> None:
        chunk = chunk_doc_id(HIDDEN, 0)
        seeded.knowledge.document_store.put(chunk, "chunk body", {})
        seeded.knowledge.graph_store.upsert_node(
            "node-chunky",
            "entity",
            {"name": "chunky claim", "evidence_ref": chunk},
            document_ids=[chunk, OPEN],
        )
        with as_http(READ):
            result = get_graph("node-chunky")
        assert chunk not in result
        assert f"`{OPEN}`" in result


class TestGetItems:
    def test_a_fetched_entity_names_no_hidden_id(self, seeded: StoreRegistry) -> None:
        with as_http(READ):
            result = get_items([ROOT])
        assert HIDDEN not in result
        assert "root claim" in result
        assert f"`{OPEN}`" in result
        assert "note" in result

    @pytest.mark.parametrize(
        "caller", [as_http(ADMIN), as_http(RESTRICTED), as_stdio()]
    )
    def test_a_restricted_caller_is_served_unchanged(
        self, seeded: StoreRegistry, caller: Any
    ) -> None:
        with caller:
            result = get_items([ROOT])
        assert f"`{HIDDEN}`" in result
        assert f"- **evidence_ref**: {HIDDEN}" in result

    def test_the_document_branch_is_unaffected(self, seeded: StoreRegistry) -> None:
        with as_http(READ):
            readable = get_items([OPEN])
            withheld = get_items([HIDDEN])
        assert "open body" in readable
        assert "hidden body" not in withheld


class TestGetFileContext:
    """The markdown never printed an entity's ``document_ids``; the builder's
    result dict did (``tests/unit/retrieve/test_graph_pointer_concealment.py``
    reads that directly). This pins the tool's text."""

    def test_a_read_token_sees_the_entity_and_no_hidden_id(
        self, seeded: StoreRegistry
    ) -> None:
        with as_http(READ):
            result = get_file_context(["svc/open.py"])
        assert "root claim" in result
        assert HIDDEN not in result
