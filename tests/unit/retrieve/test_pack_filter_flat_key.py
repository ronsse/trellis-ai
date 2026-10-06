"""A pack filter key reaches the SQLite store axes as one flat key, as written.

``PackBuilder.build(filters=...)`` forwards a key it does not own to the graph
store's ``query(properties=...)`` and the vector store's ``query(filters=...)``.
#729 refused a key outside ``[A-Za-z0-9_-]+`` there, so the pack recorded both
axes as strategy failures. Both stores now bind the key, so the key that would
have injected a tautology selects exactly the rows that carry it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GraphSearch, KeywordSearch, SemanticSearch
from trellis.schemas.pack import Pack
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path

#: Spliced raw, this key would match every node and vector both axes can see.
TAUTOLOGY_KEY = "team') OR 1=1 OR ('"
TEXT = "how to fix the cache layer"


def _embed(text: str) -> list[float]:
    """Deterministic 3-d toy embedding — enough for ordering, not meaning."""
    return [1.0, (sum(ord(c) for c in text) % 97) / 97.0, 0.5]


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    graph = reg.knowledge.graph_store
    graph.upsert_node(
        node_id="node-1",
        node_type="service",
        properties={"name": "cache service", TAUTOLOGY_KEY: "data"},
    )
    graph.upsert_node(
        node_id="node-2",
        node_type="service",
        properties={"name": "cache layer", TAUTOLOGY_KEY: "platform"},
    )
    reg.knowledge.document_store.put("doc-1", TEXT, {"title": "cache doc"})
    vectors = reg.knowledge.vector_store
    vectors.upsert(
        "vec-1", _embed(TEXT), metadata={"excerpt": TEXT, TAUTOLOGY_KEY: "data"}
    )
    vectors.upsert(
        "vec-2",
        _embed("cache layer fix"),
        metadata={"excerpt": "cache layer fix", TAUTOLOGY_KEY: "platform"},
    )
    yield reg
    reg.close()


def _build(
    registry: StoreRegistry, filters: dict[str, Any]
) -> tuple[Pack, dict[str, Any]]:
    """Build a three-axis pack; return it with its ``PACK_ASSEMBLED`` payload."""
    event_log = registry.operational.event_log
    builder = PackBuilder(
        strategies=[
            KeywordSearch(registry.knowledge.document_store),
            SemanticSearch(registry.knowledge.vector_store, _embed),
            GraphSearch(registry.knowledge.graph_store),
        ],
        event_log=event_log,
    )
    pack = builder.build("fix the cache layer", filters=filters)
    (event,) = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, entity_id=pack.pack_id
    )
    return pack, event.payload or {}


def test_the_injection_key_selects_the_rows_that_carry_it(
    registry: StoreRegistry,
) -> None:
    pack, payload = _build(registry, {TAUTOLOGY_KEY: "platform"})

    assert payload["strategy_failures"] == []
    assert pack.retrieval_report.strategies_used == ["keyword", "semantic", "graph"]
    assert sorted(item.item_id for item in pack.items) == ["node-2", "vec-2"]
