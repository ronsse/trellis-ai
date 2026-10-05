"""A Bolt read with a ``LIMIT`` sorts and limits before it projects.

Whole-node reads return ``n {.*, embedding: null} AS n`` rather than ``n``
(``_NODE_WITHOUT_EMBEDDING``), so they do not fetch the embedding that a
Neo4j or ArcadeDB vector store keeps on each node row. An ``ORDER BY``
written after that ``AS n`` sorts the projected maps, so the engine builds a
map for every candidate row before the ``LIMIT`` keeps a few. ``query`` and
``execute_node_query`` therefore sort and limit ``n`` in a ``WITH`` and
project last. Both orders return the same rows, so only the statement shows
which one runs.

No database is needed: the store's read helper is replaced by one that
records the Cypher and returns no rows.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("neo4j")

from trellis.stores.base.graph_query import FilterClause, NodeQuery
from trellis.stores.bolt_opencypher.graph import (
    _NODE_WITHOUT_EMBEDDING,
    BoltOpenCypherGraphStore,
)

LIMITED_READS: dict[str, Callable[[BoltOpenCypherGraphStore], object]] = {
    "query": lambda store: store.query(limit=3),
    "query_node_type": lambda store: store.query(node_type="concept", limit=3),
    "query_properties": lambda store: store.query(properties={"name": "x"}, limit=3),
    "execute_node_query": lambda store: store.execute_node_query(
        NodeQuery(filters=(), limit=3)
    ),
    "execute_node_query_properties": lambda store: store.execute_node_query(
        NodeQuery(filters=(FilterClause("properties.name", "eq", "x"),), limit=3)
    ),
}


@pytest.mark.parametrize("read", sorted(LIMITED_READS))
def test_limited_read_projects_after_its_limit(
    read: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BoltOpenCypherGraphStore(
        driver=object(),
        database="neo4j",
        owns_driver=False,
        init_schema=False,
    )
    sent: list[str] = []

    def record(cypher: str, **_params: Any) -> list[Any]:
        sent.append(cypher)
        return []

    monkeypatch.setattr(store, "_run_read_list", record)
    LIMITED_READS[read](store)

    assert len(sent) == 1, sent
    cypher = sent[0]
    order_at = cypher.index(" WITH n ORDER BY n.created_at DESC ")
    limit_at = cypher.index(" LIMIT ")
    assert order_at < limit_at, cypher
    assert cypher.endswith(f" RETURN {_NODE_WITHOUT_EMBEDDING}"), cypher
