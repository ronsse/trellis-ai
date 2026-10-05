"""Whole-node Bolt graph reads leave the shape-#2 ``embedding`` behind.

Shared by ``test_neo4j_vector.py`` and ``test_arcadedb_vector.py``, whose
``stores`` fixtures pair a Bolt graph store with the vector store that
writes ``embedding`` onto the same ``(:Node)`` rows.

No graph-store return value carries the embedding, with or without the
projection: ``_node_props_to_dict`` builds its dict from named keys. What
the projection changes is what crosses the wire, so these checks spy on
the raw row each read hands to that conversion. Both engines answer
``RETURN n {.*, embedding: null} AS n`` with the key present and ``None``,
which is why a raw row passes when ``embedding`` is absent or ``None``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import trellis.stores.bolt_opencypher.graph as bolt_graph
from trellis.schemas.well_known import normalize_entity_name
from trellis.stores.base.graph import AliasBindStatus
from trellis.stores.base.graph_query import FilterClause, NodeQuery

if TYPE_CHECKING:
    import pytest

#: Two versions, each with an embedding; the current one is the nearest
#: neighbour of :data:`EMBEDDED_VECTOR`.
EMBEDDED = "emb-a"
EMBEDDED_NAME = "Embedded A two"
EMBEDDED_VECTOR = [0.9, 0.1, 0.0]
#: A curated node with an embedding.
CURATED = "emb-b"
CURATED_SPEC: dict[str, Any] = {
    "node_id": CURATED,
    "node_type": "concept",
    "properties": {"name": "Curated B"},
    "node_role": "curated",
    "generation_spec": {"generator_name": "embed-gen"},
}
#: A node with no embedding at all.
PLAIN = "plain-c"


def seed_embedded_nodes(graph: Any, vector: Any) -> None:
    """Store the three nodes and their embeddings through the two stores."""
    graph.upsert_node(EMBEDDED, "doc", {"name": "Embedded A"}, document_ids=["d-1"])
    vector.upsert(EMBEDDED, [1.0, 0.0, 0.0], metadata={"kind": "seed"})
    graph.upsert_node(
        EMBEDDED, "doc", {"name": EMBEDDED_NAME}, document_ids=["d-1", "d-2"]
    )
    vector.upsert(EMBEDDED, EMBEDDED_VECTOR, metadata={"kind": "seed"})
    graph.upsert_node(
        CURATED,
        CURATED_SPEC["node_type"],
        CURATED_SPEC["properties"],
        node_role=CURATED_SPEC["node_role"],
        generation_spec=CURATED_SPEC["generation_spec"],
    )
    vector.upsert(CURATED, [0.0, 1.0, 0.0])
    graph.upsert_node(PLAIN, "doc", {"name": "Plain C"})
    graph.upsert_edge(EMBEDDED, CURATED, "relates_to")


def stored_rows(graph: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Every ``(:Node)`` row as stored, embedding included, keyed by version."""
    with graph._driver.session(database=graph._database) as session:
        rows = [dict(record["n"]) for record in session.run("MATCH (n:Node) RETURN n")]
    return {(row["node_id"], row["valid_from"]): row for row in rows}


def _owner_read(graph: Any) -> list[dict[str, Any]]:
    # A claim held by EMBEDDED, contested by CURATED with a stale-owner key
    # equal to EMBEDDED's name: the store reads the owner's current row to
    # compare names, finds them equal, and answers CONFLICT without writing.
    # The fixtures wipe Node and Alias rows but not AliasClaim, so each call
    # claims a fresh raw id rather than meet a previous run's claim.
    raw_id = f"raw-{uuid4().hex}"
    graph.bind_alias_if_absent(EMBEDDED, "embed-src", raw_id)
    result = graph.bind_alias_if_absent(
        CURATED,
        "embed-src",
        raw_id,
        stale_owner_name_key=normalize_entity_name(EMBEDDED_NAME),
    )
    assert result.status is AliasBindStatus.CONFLICT
    assert result.entity_id == EMBEDDED
    return []


def _unchanged_bulk_upsert(graph: Any) -> list[dict[str, Any]]:
    # The pre-read decides this re-upsert is a no-op, so no row changes.
    assert graph.upsert_nodes_bulk([dict(CURATED_SPEC)]) == [CURATED]
    return []


#: Each whole-node read, mapped to a call that returns the node dicts it
#: gave its caller (empty where the read's result is not node dicts).
WHOLE_NODE_READS: dict[str, Callable[[Any], list[dict[str, Any]]]] = {
    "get_node": lambda graph: [graph.get_node(EMBEDDED)],
    "get_nodes_bulk": lambda graph: graph.get_nodes_bulk([EMBEDDED, CURATED, PLAIN]),
    "get_node_history": lambda graph: graph.get_node_history(EMBEDDED),
    "get_subgraph": lambda graph: graph.get_subgraph([EMBEDDED])["nodes"],
    "query": lambda graph: graph.query(node_type="doc"),
    "search_nodes": lambda graph: graph.search_nodes()[0],
    "execute_node_query": lambda graph: graph.execute_node_query(
        NodeQuery(filters=(FilterClause("node_type", "eq", "concept"),), limit=10)
    ),
    "upsert_nodes_bulk": _unchanged_bulk_upsert,
    "bind_alias_if_absent": _owner_read,
}


def _without_embedding(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "embedding"}


def assert_read_leaves_embedding_out(
    graph: Any, vector: Any, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    """Run one whole-node read and check what it fetched and returned."""
    seed_embedded_nodes(graph, vector)
    before = stored_rows(graph)
    convert = bolt_graph._node_props_to_dict
    fetched: list[dict[str, Any]] = []

    def spy(props: dict[str, Any]) -> dict[str, Any]:
        fetched.append(dict(props))
        return convert(props)

    with monkeypatch.context() as patch:
        patch.setattr(bolt_graph, "_node_props_to_dict", spy)
        returned = WHOLE_NODE_READS[read](graph)

    keys = [(row["node_id"], row["valid_from"]) for row in fetched]
    assert any(isinstance(before[key].get("embedding"), list) for key in keys), (
        f"{read} fetched no row that stores an embedding, so it proves nothing"
    )
    for key, row in zip(keys, fetched, strict=True):
        assert row.get("embedding") is None, f"{read} fetched the embedding of {key}"
        assert _without_embedding(row) == _without_embedding(before[key]), (
            f"{read} fetched {key} with other properties changed"
        )
    for node in returned:
        assert "embedding" not in node, f"{read} returned an embedding key"
        assert node == convert(before[(node["node_id"], node["valid_from"])])
    assert stored_rows(graph) == before, f"{read} changed a stored row"


def run_every_whole_node_read(graph: Any) -> None:
    for read in WHOLE_NODE_READS.values():
        read(graph)
