"""A pack filter key that is not a plain JSON object key fails the SQLite store axes.

``PackBuilder.build(filters=...)`` forwards a key it does not own to the graph
store's ``query(properties=...)`` and the vector store's ``query(filters=...)``.
Both raise ``ValueError`` for a key outside ``[A-Za-z0-9_-]+``, and the pack
records that as each axis's strategy failure while the keyword axis still runs.
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
    reg.knowledge.graph_store.upsert_node(
        node_id="node-1",
        node_type="service",
        properties={"name": "cache service", "owner_team": "data"},
    )
    reg.knowledge.document_store.put("doc-1", TEXT, {"title": "cache doc"})
    reg.knowledge.vector_store.upsert(
        "vec-1", _embed(TEXT), metadata={"excerpt": TEXT, "owner_team": "data"}
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


def test_a_bad_key_fails_both_store_axes_with_a_value_error(
    registry: StoreRegistry,
) -> None:
    pack, payload = _build(registry, {TAUTOLOGY_KEY: "platform"})

    assert [item.item_id for item in pack.items] == []
    assert pack.retrieval_report.strategies_used == ["keyword"]
    failures = {f["strategy"]: f for f in payload["strategy_failures"]}
    assert set(failures) == {"semantic", "graph"}
    for failure in failures.values():
        assert failure["error_class"] == "ValueError"
        assert "not a plain JSON object key" in failure["message"]
