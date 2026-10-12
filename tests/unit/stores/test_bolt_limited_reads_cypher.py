"""A Bolt read with a ``LIMIT`` sorts and limits before it projects.

``query`` and ``execute_node_query`` sort and limit ``n`` in a ``WITH``
before the ``_NODE_WITHOUT_EMBEDDING`` projection, so the engine builds a
map only for the rows it returns. Projecting first returns the same rows,
so only the statement shows which order runs.

No database is needed: the store's read helper is replaced by one that
records the Cypher and returns no rows.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("neo4j")

from trellis.stores.base.graph_query import NodeQuery
from trellis.stores.bolt_opencypher.graph import (
    _NODE_WITHOUT_EMBEDDING,
    BoltOpenCypherGraphStore,
)

LIMITED_READS: dict[str, Callable[[BoltOpenCypherGraphStore], object]] = {
    "query": lambda store: store.query(limit=3),
    "execute_node_query": lambda store: store.execute_node_query(NodeQuery(limit=3)),
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
    projected_at = cypher.index(_NODE_WITHOUT_EMBEDDING)
    assert cypher.index("ORDER BY") < projected_at, cypher
    assert cypher.index("LIMIT") < projected_at, cypher
