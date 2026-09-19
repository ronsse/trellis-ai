"""Failure paths of the links ``save_knowledge`` derives for a new note.

The note already exists when linking runs, so every failure here must come
back as a response line and never as a raise. The happy paths are driven
end to end through the MCP tool in ``test_server.py::TestSaveKnowledge``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from trellis.mcp.knowledge_links import domain_values, link_knowledge_node
from trellis.mutate import build_curate_executor
from trellis.schemas.well_known import APPLIES_TO, CONCEPT, SOFTWARE_APPLICATION

if TYPE_CHECKING:
    from trellis.stores.base.graph import GraphStore
    from trellis.stores.registry import StoreRegistry


class _FailingReads:
    """Graph store proxy whose named read methods raise."""

    def __init__(self, inner: GraphStore, *failing: str) -> None:
        self._inner = inner
        self._failing = set(failing)

    def __getattr__(self, name: str) -> Any:
        if name in self._failing:

            def _raise(*args: Any, **kwargs: Any) -> Any:
                msg = f"{name} unavailable"
                raise RuntimeError(msg)

            return _raise
        return getattr(self._inner, name)


@pytest.fixture
def graph(temp_registry: StoreRegistry) -> GraphStore:
    store = temp_registry.knowledge.graph_store
    store.upsert_node("01NOTE", CONCEPT, {"name": "note"})
    store.upsert_node("domain:trellis", CONCEPT, {"name": "trellis"})
    store.upsert_node("tool:bash", SOFTWARE_APPLICATION, {"name": "Bash"})
    return store


def _link(
    registry: StoreRegistry,
    graph_store: Any,
    *,
    relates_to: str | None = "Bash",
    domain: Any = "trellis",
) -> list[str]:
    return link_knowledge_node(
        build_curate_executor(registry),
        graph_store,
        node_id="01NOTE",
        properties={"domain": domain},
        relates_to=relates_to,
        edge_kind="entity_related_to",
    )


class TestDomainValues:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("trellis", ["trellis"]),
            ("  trellis  ", ["trellis"]),
            (["trellis", "infra"], ["trellis", "infra"]),
            (("trellis", "infra"), ["trellis", "infra"]),
            (
                ["trellis", " trellis ", "", "  ", 3, None, "infra"],
                ["trellis", "infra"],
            ),
            ("", []),
            (None, []),
            (3, []),
            ({"name": "trellis"}, []),
        ],
    )
    def test_shapes(self, raw: Any, expected: list[str]) -> None:
        assert domain_values({"domain": raw}) == expected

    def test_absent(self) -> None:
        assert domain_values({}) == []


class TestFailuresBecomeLines:
    def test_relates_to_lookup_failure_still_links_the_domain(
        self, temp_registry: StoreRegistry, graph: GraphStore
    ) -> None:
        # Only the relates_to lookup reads the alias index.
        lines = _link(temp_registry, _FailingReads(graph, "resolve_alias"))

        assert lines[0] == (
            "Warning: could not resolve relates_to 'Bash' (store error)"
            " — edge not created"
        )
        assert lines[1].startswith("Domain link: ")
        edges = graph.get_edges("01NOTE", direction="outgoing")
        assert [(e["target_id"], e["edge_type"]) for e in edges] == [
            ("domain:trellis", APPLIES_TO)
        ]

    def test_a_node_read_failure_links_nothing(
        self, temp_registry: StoreRegistry, graph: GraphStore
    ) -> None:
        lines = _link(temp_registry, _FailingReads(graph, "get_nodes_bulk"))

        assert lines == [
            (
                "Warning: could not resolve relates_to 'Bash' (store error)"
                " — edge not created"
            ),
            (
                "Warning: could not look up domain nodes (store error)"
                " — domain not linked"
            ),
        ]
        assert graph.get_edges("01NOTE", direction="outgoing") == []

    def test_a_domain_value_never_matches_a_non_domain_node(
        self, temp_registry: StoreRegistry, graph: GraphStore
    ) -> None:
        # Neither an exact id nor a name alias may answer for a domain: only
        # a ``domain:`` node can be what a note's domain property names.
        graph.upsert_node("01OTHER", CONCEPT, {"name": "infra notes"})
        graph.upsert_alias("01OTHER", "name", "infra notes", raw_name="infra notes")

        lines = _link(
            temp_registry, graph, relates_to=None, domain=["tool:bash", "infra notes"]
        )

        assert lines == [
            "Note: domain 'tool:bash' has no domain node — not linked",
            "Note: domain 'infra notes' has no domain node — not linked",
        ]
        assert graph.get_edges("01NOTE", direction="outgoing") == []

    def test_a_domain_node_never_links_to_itself(
        self, temp_registry: StoreRegistry, graph: GraphStore
    ) -> None:
        lines = link_knowledge_node(
            build_curate_executor(temp_registry),
            graph,
            node_id="domain:trellis",
            properties={"domain": "trellis"},
            relates_to=None,
            edge_kind="entity_related_to",
        )

        assert lines == []
        assert graph.get_edges("domain:trellis", direction="outgoing") == []

    def test_no_relates_to_and_no_domain_is_silent(
        self, temp_registry: StoreRegistry, graph: GraphStore
    ) -> None:
        # A store that raises on any read proves nothing was read.
        failing = _FailingReads(graph, "resolve_alias", "get_nodes_bulk")
        assert _link(temp_registry, failing, relates_to=None, domain=None) == []
