"""A stored ``source_strategy`` key cannot relabel the item it came with (#943).

The keyword, semantic and graph strategies build item metadata by splatting the
store's open metadata bag. ``source_strategy`` names the strategy that
*collected* the item, which is a fact about this search and not about the row:
it picks the class-prior axis that judges the item and becomes ``strategy_source``
in ``PACK_ASSEMBLED.injected_items[]``, so it feeds telemetry, RRF and feedback
attribution. It is therefore stamped after the splat, as ``recency_clock`` is.

No writer in the repo stores the key today, so each test plants it by hand. The
assertion is on the strategy's own output, not on a hand-built ``PackItem``,
because the stamp is decided where the metadata is built.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GraphSearch, KeywordSearch, SemanticSearch

_DIMS = 8

#: A name no built-in strategy answers to, so a leak cannot coincide with the
#: right answer.
_FORGED = "forged"


def _keyword_items(metadata: dict[str, Any]) -> list[Any]:
    store = MagicMock()
    store.search.return_value = [
        {
            "doc_id": "d1",
            "content": "a document with a stored source_strategy key",
            "metadata": metadata,
            "rank": -0.9,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ]
    return KeywordSearch(store).search("anything")


def _semantic_items(metadata: dict[str, Any]) -> list[Any]:
    store = MagicMock()
    store.query.return_value = [
        {
            "item_id": "v1",
            "score": 0.8,
            "metadata": {"content": "a vector row snapshot", **metadata},
        }
    ]
    return SemanticSearch(store, lambda _text: [0.0] * _DIMS).search("anything")


def _graph_items(properties: dict[str, Any]) -> list[Any]:
    store = MagicMock()
    del store.execute_node_query
    store.query.return_value = [
        {
            "node_id": "n1",
            "node_type": "concept",
            "properties": {"name": "a node with a substantive excerpt", **properties},
        }
    ]
    return GraphSearch(store).search("anything")


_CASES = [
    pytest.param(_keyword_items, "keyword", id="keyword"),
    pytest.param(_semantic_items, "semantic", id="semantic"),
    pytest.param(_graph_items, "graph", id="graph"),
]


@pytest.mark.parametrize(("collect", "expected"), _CASES)
def test_a_stored_source_strategy_cannot_relabel_the_item(
    collect: Any, expected: str
) -> None:
    items = collect({"source_strategy": _FORGED})
    assert len(items) == 1
    assert items[0].metadata["source_strategy"] == expected


@pytest.mark.parametrize(("collect", "expected"), _CASES)
def test_stored_metadata_without_the_key_is_served_unchanged(
    collect: Any, expected: str
) -> None:
    """The stamp moved, the default output did not: other keys still pass through."""
    items = collect({"title": "kept"})
    assert items[0].metadata["source_strategy"] == expected
    assert items[0].metadata["title"] == "kept"


def test_the_forged_label_does_not_reach_the_pack_attribution() -> None:
    """End to end for one axis: ``strategy_source`` is the collecting strategy."""
    store = MagicMock()
    store.search.return_value = [
        {
            "doc_id": "d1",
            "content": "a document with a stored source_strategy key",
            "metadata": {"source_strategy": _FORGED},
            "rank": -0.9,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ]
    pack = PackBuilder(strategies=[KeywordSearch(store)]).build(
        "anything", clearance="restricted"
    )
    assert [i.strategy_source for i in pack.items] == ["keyword"]
