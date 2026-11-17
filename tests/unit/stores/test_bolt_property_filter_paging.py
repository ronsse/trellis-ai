"""A Bolt property-filtered read pages until it has ``limit`` matches.

``properties.<key>`` filters run in Python because the Bolt stores keep
properties as one JSON string. The contract suite proves the answer on a
live engine; this proves, without one, that the read keeps paging past
pages holding no match, stops once ``limit`` rows match or a page comes
back short, and that an unfiltered read stays one statement.

The store's read helper is replaced by one that answers each statement's
``SKIP``/``LIMIT`` from a fixed newest-first list of rows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("neo4j")

from trellis.stores.base.graph_query import EdgeQuery, FilterClause, NodeQuery
from trellis.stores.bolt_opencypher import graph as bolt_graph
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore

PAGE = 3
_SKIP_LIMIT = re.compile(r"SKIP (\d+) LIMIT (\d+)")


def _node(i: int, tier: str) -> dict[str, Any]:
    return {
        "node_id": f"n{i}",
        "node_type": "service",
        "properties_json": json.dumps({"tier": tier}),
    }


def _edge(i: int, tier: str) -> dict[str, Any]:
    return {
        "edge_id": f"e{i}",
        "source_id": "a",
        "target_id": "b",
        "edge_type": f"kind_{i}",
        "properties_json": json.dumps({"tier": tier}),
    }


def _paged_store(
    monkeypatch: pytest.MonkeyPatch, records: list[dict[str, Any]]
) -> tuple[BoltOpenCypherGraphStore, list[str]]:
    monkeypatch.setattr(bolt_graph, "_PROPERTY_FILTER_PAGE_SIZE", PAGE)
    store = BoltOpenCypherGraphStore(
        driver=object(), database="neo4j", owns_driver=False, init_schema=False
    )
    sent: list[str] = []

    def answer(cypher: str, **_params: Any) -> list[Any]:
        sent.append(cypher)
        found = _SKIP_LIMIT.search(cypher)
        if found is None:
            return records
        skip, limit = int(found.group(1)), int(found.group(2))
        return records[skip : skip + limit]

    monkeypatch.setattr(store, "_run_read_list", answer)
    return store, sent


# Ten newer rows fail the filter; the three oldest pass it.
NODE_ROWS = [{"n": _node(i, "tin")} for i in range(10)] + [
    {"n": _node(10 + i, "gold")} for i in range(3)
]
EDGE_ROWS = [{"r": _edge(i, "tin")} for i in range(10)] + [
    {"r": _edge(10 + i, "gold")} for i in range(3)
]
GOLD = FilterClause("properties.tier", "eq", "gold")

READS: dict[str, tuple[list[dict[str, Any]], Callable[..., list[dict[str, Any]]]]] = {
    "query": (NODE_ROWS, lambda s, n: s.query(properties={"tier": "gold"}, limit=n)),
    "execute_node_query": (
        NODE_ROWS,
        lambda s, n: s.execute_node_query(NodeQuery(filters=(GOLD,), limit=n)),
    ),
    "execute_edge_query": (
        EDGE_ROWS,
        lambda s, n: s.execute_edge_query(EdgeQuery(filters=(GOLD,), limit=n)),
    ),
}


@pytest.mark.parametrize("read", sorted(READS))
def test_pages_past_unmatched_pages_and_stops_at_limit(
    read: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, run = READS[read]
    store, sent = _paged_store(monkeypatch, rows)

    results = run(store, 2)

    assert [r["properties"]["tier"] for r in results] == ["gold", "gold"]
    # Rows 10 and 11 sit on the fourth page (SKIP 9); nothing past it is read.
    assert [_SKIP_LIMIT.search(c).group(1) for c in sent] == ["0", "3", "6", "9"]


@pytest.mark.parametrize("read", sorted(READS))
def test_stops_at_a_short_page_when_too_few_match(
    read: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, run = READS[read]
    store, sent = _paged_store(monkeypatch, rows)

    results = run(store, 5)

    assert len(results) == 3
    # 13 rows: pages of 3, 3, 3, 3, then a short page of 1 ends the read.
    assert len(sent) == 5


@pytest.mark.parametrize(
    "run",
    [
        lambda s: s.query(limit=2),
        lambda s: s.execute_node_query(NodeQuery(limit=2)),
        lambda s: s.execute_edge_query(EdgeQuery(limit=2)),
    ],
    ids=["query", "execute_node_query", "execute_edge_query"],
)
def test_unfiltered_read_is_one_statement_without_skip(
    run: Callable[[BoltOpenCypherGraphStore], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sent = _paged_store(monkeypatch, [])

    run(store)

    assert len(sent) == 1
    assert "SKIP" not in sent[0]
