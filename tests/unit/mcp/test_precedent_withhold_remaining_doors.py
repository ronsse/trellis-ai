"""The MCP doors #1211 left open judge a promoted precedent's source too (#1080).

``get_items``, ``get_graph``, ``get_file_context`` and ``search`` served a
precedent whose source was labelled above the caller (``search`` through the
pack, ``get_items`` / ``get_graph`` / ``get_file_context`` by id or by path).
They now ask the one predicate
(:class:`~trellis.retrieve.precedents.PrecedentClearance`). Each door has the
same three cases:

* a caller below the source's label: the precedent is withheld (never rewritten);
* a caller cleared for it: the precedent is served;
* a store with no label anywhere: the door answers exactly as it did before the
  gate, compared with the same call with the predicate off.

Each tool is called the way ``test_sensitivity_surfaces.py`` calls it, as an HTTP
caller whose scopes set the clearance.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
from mcp.shared.exceptions import MCPError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from tests.unit.mcp.test_sensitivity_surfaces import as_http, as_stdio
from tests.unit.retrieve.test_precedent_listing_clearance import (
    BODY,
    LEAKS,
    promote,
    relabel,
)
from trellis.retrieve.precedents import PrecedentClearance
from trellis.schemas.well_known import OBSERVATION

if TYPE_CHECKING:
    from collections.abc import Callable

    from trellis.stores.registry import StoreRegistry

get_items = unwrap_tool(server_mod.get_items)
get_graph = unwrap_tool(server_mod.get_graph)
get_file_context = unwrap_tool(server_mod.get_file_context)
search = unwrap_tool(server_mod.search)
query_observations = unwrap_tool(server_mod.query_observations)

SOURCE = "doc-source"
PATH = "svc/runbook.py"
#: Matches the source and the precedent, and is not itself one of ``LEAKS``: a
#: reply echoes the query it answers.
SEARCH_QUERY = "Heron"
#: The text that shows the precedent was served. A not-found reply echoes the id
#: asked for and the graph views abbreviate ids, so the title is what to look for.
SERVED = "Quartz Title Words"
ANCHOR = "doc-anchor"
#: A caller below the source's label, and one cleared for it.
BELOW = ["read"]
CLEARED = ["read", "confidential"]


@pytest.fixture
def open_precedent(temp_registry: StoreRegistry) -> str:
    """A precedent promoted from a source that carries no label at all.

    Beyond the promotion, the store holds what the path-keyed tool needs to meet
    the precedent: the precedent's own row names ``PATH``, and its node is linked
    to an open anchor document at ``PATH`` too.
    """
    docs = temp_registry.knowledge.document_store
    graph = temp_registry.knowledge.graph_store
    graph.upsert_node("entity://shared", "entity", {"name": "x"})
    docs.put(SOURCE, BODY, {"title": "Source"})
    docs.put(ANCHOR, "an anchor note", {"title": "Anchor", "source_path": "svc/a.py"})
    entity_id = promote(temp_registry, "cand:one", SOURCE)
    row = docs.get(entity_id)
    assert row is not None
    assert all(leak in row["content"] for leak in LEAKS), row["content"]
    docs.put(entity_id, row["content"], {**row["metadata"], "source_path": PATH})
    node = graph.get_node(entity_id)
    assert node is not None
    graph.upsert_node(
        entity_id,
        node["node_type"],
        node["properties"],
        document_ids=[ANCHOR],
    )
    return entity_id


@pytest.fixture
def precedent(temp_registry: StoreRegistry, open_precedent: str) -> str:
    """The same precedent once its source is labelled ``confidential``."""
    relabel(temp_registry, SOURCE, "confidential")
    return open_precedent


def _items(entity_id: str) -> str:
    return str(get_items([entity_id]))


def _graph_root(entity_id: str) -> str:
    return str(get_graph(entity_id))


def _graph_neighbour(entity_id: str) -> str:
    return str(get_graph("entity://shared"))


def _file_documents(entity_id: str) -> str:
    return str(get_file_context([PATH]))


def _file_entities(entity_id: str) -> str:
    return str(get_file_context(["svc/a.py"]))


def _search(entity_id: str) -> str:
    return str(search(SEARCH_QUERY))


def _unpack(text: str) -> str:
    """The reply without the per-call pack id and the like."""
    return re.sub(r"[0-9A-HJKMNP-TV-Z]{26}", "<id>", text)


#: door -> (call, the text that shows the precedent was served).
DOORS: dict[str, tuple[Callable[[str], str], str]] = {
    "get_items": (_items, SERVED),
    "get_graph_root": (_graph_root, SERVED),
    "get_graph_neighbour": (_graph_neighbour, SERVED),
    "get_file_context_document": (_file_documents, SERVED),
    "get_file_context_entity": (_file_entities, SERVED),
    "search": (_search, SERVED),
}


def _call(door: str, scopes: list[str], entity_id: str) -> str:
    """The door's reply, or the not-found a root read raises, for ``scopes``."""
    call, _ = DOORS[door]
    with as_http(scopes):
        try:
            return call(entity_id)
        except MCPError as exc:
            return f"MCP-ERROR {exc}"


@pytest.mark.parametrize("door", sorted(DOORS))
class TestEveryDoor:
    def test_a_caller_cleared_for_the_source_is_served(
        self, temp_registry, precedent, door
    ):
        reply = _call(door, CLEARED, precedent)

        assert DOORS[door][1] in reply, reply

    def test_a_caller_below_the_sources_label_is_not_served(
        self, temp_registry, precedent, door
    ):
        # The premise: the same precedent is served to a caller who may read it.
        assert DOORS[door][1] in _call(door, CLEARED, precedent)

        reply = _call(door, BELOW, precedent)

        assert DOORS[door][1] not in reply
        assert not any(leak in reply for leak in LEAKS), reply

    def test_an_unlabelled_store_answers_as_it_did_before_the_gate(
        self, temp_registry, open_precedent, door, monkeypatch
    ):
        with_gate = _call(door, BELOW, open_precedent)
        assert DOORS[door][1] in with_gate, "an empty answer proves nothing"

        monkeypatch.setattr(PrecedentClearance, "verdict_of", lambda *_a, **_k: None)
        without_gate = _call(door, BELOW, open_precedent)

        assert _unpack(with_gate) == _unpack(without_gate)

    def test_a_trusted_local_caller_is_not_judged(self, temp_registry, precedent, door):
        with as_stdio():
            reply = DOORS[door][0](precedent)

        assert DOORS[door][1] in reply


class TestGetItems:
    def test_a_withheld_precedent_is_reported_as_not_found(
        self, temp_registry, precedent
    ):
        with as_http(BELOW):
            withheld = get_items([precedent])
            missing = get_items(["precedent://learning/nothing-here"])

        assert withheld.replace(precedent, "X") == missing.replace(
            "precedent://learning/nothing-here", "X"
        )

    def test_a_precedent_with_no_document_row_is_judged_by_its_node(
        self, temp_registry
    ):
        """The graph branch: no row at the id, so the node names the source."""
        docs = temp_registry.knowledge.document_store
        docs.put(SOURCE, BODY, {"title": "Source"})
        relabel(temp_registry, SOURCE, "confidential")
        node_only = "precedent://learning/node-only"
        temp_registry.knowledge.graph_store.upsert_node(
            node_only,
            "precedent",
            {"name": "Node Only Words", "source_item_id": SOURCE},
        )

        with as_http(CLEARED):
            assert "Node Only Words" in get_items([node_only])
        with as_http(BELOW):
            assert "Node Only Words" not in get_items([node_only])

    def test_an_external_key_is_judged_by_the_source_too(
        self, temp_registry, precedent
    ):
        """A public-only caller reads documents labelled ``public`` and no node.

        Nothing labels a promoted precedent's row, so it reads ``internal``;
        labelled ``public`` by hand over a source that stays above ``public``,
        the row's own label passes and the source is what says no.
        """
        relabel(temp_registry, precedent, "public")
        external = ["read", "external"]

        with as_http(external):
            withheld = get_items([precedent])
        relabel(temp_registry, SOURCE, "public")
        with as_http(external):
            served = get_items([precedent])

        assert SERVED not in withheld
        assert "not found" in withheld
        assert SERVED in served

    def test_other_ids_in_the_same_call_are_still_served(
        self, temp_registry, precedent
    ):
        with as_http(BELOW):
            reply = get_items([precedent, ANCHOR])

        assert "an anchor note" in reply
        assert "Quartz Title Words" not in reply


class TestGetGraph:
    def test_a_withheld_root_is_the_not_found_a_missing_one_gets(
        self, temp_registry, precedent
    ):
        with as_http(BELOW), pytest.raises(MCPError, match="entity not found"):
            get_graph(precedent)

    def test_a_withheld_neighbour_is_left_out_with_its_edges(
        self, temp_registry, precedent
    ):
        with as_http(CLEARED):
            cleared = get_graph("entity://shared")
        with as_http(BELOW):
            below = get_graph("entity://shared")

        assert SERVED in cleared
        assert SERVED not in below
        assert "precedent_applies_to" not in below, "the edge goes with the node"
        assert "# x (entity)" in below, "the root itself is still served"


class TestSearch:
    def test_the_withheld_line_counts_the_precedent(self, temp_registry, precedent):
        with as_http(BELOW):
            reply = search(SEARCH_QUERY)

        assert "**Withheld:**" in reply
        # The source is above this caller too; the precedent is the second row.
        assert "**Withheld:** 2 " in reply, reply


class TestGetFileContext:
    def test_an_open_document_at_the_same_path_is_still_served(
        self, temp_registry, precedent
    ):
        with as_http(BELOW):
            reply = get_file_context(["svc/a.py"])

        assert ANCHOR in reply

    def test_a_precedent_anchored_to_an_open_document_is_left_out(
        self, temp_registry, precedent
    ) -> None:
        with as_http(BELOW):
            below: Any = get_file_context(["svc/a.py"])
        with as_http(CLEARED):
            cleared: Any = get_file_context(["svc/a.py"])

        assert SERVED in cleared
        assert SERVED not in below


class TestAWithheldPrecedentIsNotNamedByAnotherNode:
    """``get_graph`` and a fetched entity name the precedent only to a reader (#1232).

    A pointer is an id with no text; the precedent's own row carries no label, so
    only its source's label, which the read doors judge, says a caller may not
    have it.
    """

    HOLDER = "node-holder"

    @pytest.fixture
    def holder(self, temp_registry: StoreRegistry, precedent: str) -> str:
        temp_registry.knowledge.graph_store.upsert_node(
            self.HOLDER,
            "entity",
            {"name": "holder note"},
            document_ids=[ANCHOR, precedent],
        )
        return self.HOLDER

    @pytest.mark.parametrize("tool", [get_graph, get_items], ids=["graph", "items"])
    def test_named_to_a_cleared_caller_and_not_to_one_below_it(
        self, temp_registry, precedent, holder, tool
    ):
        arg = holder if tool is get_graph else [holder]
        with as_http(CLEARED):
            cleared = str(tool(arg))
        with as_http(BELOW):
            below = str(tool(arg))

        assert precedent in cleared, "the premise: a reader sees the id"
        assert precedent not in below
        assert ANCHOR in below, "a readable pointer is kept"

    def test_a_precedent_nothing_withholds_is_still_named(
        self, temp_registry, open_precedent
    ):
        temp_registry.knowledge.graph_store.upsert_node(
            self.HOLDER,
            "entity",
            {"name": "holder note"},
            document_ids=[ANCHOR, open_precedent],
        )

        with as_http(BELOW):
            assert open_precedent in str(get_graph(self.HOLDER))


class TestAnObservationDoesNotNameAWithheldPrecedent:
    """``query_observations`` judges an observation's ``evidence_ref`` as the others do.

    The node carries no label, so it is served; its ``evidence_ref`` names a
    precedent whose source is labelled above a ``read`` key, which the plain
    document read would call readable (the precedent's own row has no label).
    """

    @pytest.fixture
    def observation(self, temp_registry: StoreRegistry, precedent: str) -> str:
        temp_registry.knowledge.graph_store.upsert_node(
            "obs-1",
            OBSERVATION,
            {
                "subject_entity_id": "subject-1",
                "observer_agent_id": "agent-1",
                "content": "p99 latency rose",
                "evidence_ref": precedent,
            },
        )
        return "obs-1"

    def test_named_to_a_cleared_caller_and_not_to_one_below_it(
        self, temp_registry, precedent, observation
    ):
        with as_http(CLEARED):
            cleared = query_observations(subject_entity_id="subject-1")
        with as_http(BELOW):
            below = query_observations(subject_entity_id="subject-1")

        assert precedent in cleared, "the premise: a reader sees the id"
        assert observation in below, "the observation is served; its pointer is not"
        assert precedent not in below

    def test_a_precedent_nothing_withholds_is_still_named(
        self, temp_registry, open_precedent
    ):
        temp_registry.knowledge.graph_store.upsert_node(
            "obs-1",
            OBSERVATION,
            {"subject_entity_id": "subject-1", "evidence_ref": open_precedent},
        )

        with as_http(BELOW):
            assert open_precedent in query_observations(subject_entity_id="subject-1")
