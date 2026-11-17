"""A Bolt property-filtered read widens its window until ``limit`` rows match.

``properties.<key>`` filters run in Python because the Bolt stores keep
properties as one JSON string. The contract suite proves the answer on a
live engine; this proves, without one, that the read re-runs one ordered
statement with a ``LIMIT`` window that grows by
``_PROPERTY_FILTER_WINDOW_GROWTH`` and never uses ``SKIP``, stops once
``limit`` rows match or a statement returns fewer rows than its window,
sends nothing for a ``limit`` of zero or less, and that an unfiltered read
stays one statement.

The store's read helper is replaced by one that answers each statement's
``LIMIT`` from a fixed newest-first list of rows.
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

MIN_WINDOW = 3
GROWTH = bolt_graph._PROPERTY_FILTER_WINDOW_GROWTH
_LIMIT = re.compile(r"LIMIT (\d+|\$limit)")
#: A defective subject that never widens would loop forever; fail instead.
_RUNAWAY = 20


def _node(i: int, tier: str) -> dict[str, Any]:
    return {
        "node_id": f"n{i:03d}",
        "node_type": "service",
        "properties_json": json.dumps({"tier": tier}),
    }


def _edge(i: int, tier: str) -> dict[str, Any]:
    return {
        "edge_id": f"e{i:03d}",
        "source_id": "a",
        "target_id": "b",
        "edge_type": f"kind_{i}",
        "properties_json": json.dumps({"tier": tier}),
    }


def _windowed_store(
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, Any]],
    min_window: int = MIN_WINDOW,
) -> tuple[BoltOpenCypherGraphStore, list[str]]:
    monkeypatch.setattr(bolt_graph, "_PROPERTY_FILTER_MIN_WINDOW", min_window)
    store = BoltOpenCypherGraphStore(
        driver=object(), database="neo4j", owns_driver=False, init_schema=False
    )
    sent: list[str] = []

    def answer(cypher: str, **params: Any) -> list[Any]:
        sent.append(cypher)
        if len(sent) > _RUNAWAY:
            msg = f"more than {_RUNAWAY} statements: the window is not growing"
            raise AssertionError(msg)
        found = _LIMIT.search(cypher)
        assert found is not None, cypher
        bound = found.group(1)
        window = int(params["limit"]) if bound == "$limit" else int(bound)
        return records[:window]

    monkeypatch.setattr(store, "_run_read_list", answer)
    return store, sent


def _windows(sent: list[str]) -> list[int]:
    return [int(_LIMIT.search(c).group(1)) for c in sent]  # type: ignore[union-attr]


def _rows(make: Callable[[int, str], dict[str, Any]]) -> list[dict[str, Any]]:
    # Newest first: 100 rows fail the filter, three pass, then 200 older
    # rows fail it, so a window reaching the matches need not reach the end.
    tiers = ["tin"] * 100 + ["gold"] * 3 + ["tin"] * 200
    return [make(i, tier) for i, tier in enumerate(tiers)]


NODE_ROWS = [{"n": n} for n in _rows(_node)]
EDGE_ROWS = [{"r": e} for e in _rows(_edge)]
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
def test_window_grows_until_limit_rows_match(
    read: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, run = READS[read]
    store, sent = _windowed_store(monkeypatch, rows)

    results = run(store, 1)

    assert [r["properties"]["tier"] for r in results] == ["gold"]
    # First window max(1 * 10, 3) = 10, then 40, then 160. The 160-row
    # window is full but holds the first match (row 100), so the read stops
    # there rather than widening to 640.
    assert _windows(sent) == [10, 10 * GROWTH, 10 * GROWTH * GROWTH]
    assert _windows(sent) == [10, 40, 160]
    assert not any("SKIP" in c for c in sent)


@pytest.mark.parametrize("read", sorted(READS))
def test_stops_when_a_statement_returns_fewer_rows_than_its_window(
    read: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, run = READS[read]
    store, sent = _windowed_store(monkeypatch, rows)

    results = run(store, 5)

    assert [r["properties"]["tier"] for r in results] == ["gold"] * 3
    # Windows 50 and 200 come back full; 800 returns all 303 rows, which
    # is everything, so the read ends with three of the five asked for.
    assert _windows(sent) == [50, 50 * GROWTH, 50 * GROWTH * GROWTH]
    assert not any("SKIP" in c for c in sent)


@pytest.mark.parametrize("read", sorted(READS))
def test_first_window_is_at_least_the_minimum(
    read: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, run = READS[read]
    store, sent = _windowed_store(monkeypatch, rows, min_window=120)

    results = run(store, 2)

    assert len(results) == 2
    # max(2 * 10, 120) = 120 reaches rows 100 and 101: one statement.
    assert _windows(sent) == [120]


@pytest.mark.parametrize("read", sorted(READS))
@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_returns_nothing_and_sends_nothing(
    read: str, limit: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, run = READS[read]
    store, sent = _windowed_store(monkeypatch, rows)

    assert run(store, limit) == []
    assert sent == []


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
    store, sent = _windowed_store(monkeypatch, [])

    run(store)

    assert len(sent) == 1
    assert "SKIP" not in sent[0]
