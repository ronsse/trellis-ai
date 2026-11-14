"""A graph node's own label already withholds it from a pack (#1029).

Pin, not a change: :class:`GraphSearch` spreads a node's properties into the
item's metadata, and the collect seam judges every item by
``sensitivity_of(item.metadata)`` (:func:`partition_by_clearance`). So a node
whose properties carry ``data_classification`` above the caller's clearance is
withheld from that caller's pack and recorded in ``rejected_items``. The
direct graph doors were taught the same rule afterwards; this file keeps the
pack half of that agreement from drifting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import GraphSearch
from trellis.schemas import well_known as wk
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.schemas.pack import PackBudget
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from pathlib import Path

_LABELLED = "node-labelled"
_PLAIN = "node-plain"


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    graph = reg.knowledge.graph_store
    graph.upsert_node(
        node_id=_LABELLED,
        node_type=wk.OBSERVATION,
        properties={
            "name": "ledger table",
            "description": "caching notes for the ledger",
            DATA_CLASSIFICATION_KEY: {"sensitivity": "restricted"},
        },
    )
    graph.upsert_node(
        node_id=_PLAIN,
        node_type=wk.OBSERVATION,
        properties={"name": "plain table", "description": "caching notes, plain"},
    )
    return reg


def _build(registry: StoreRegistry, clearance: str):  # type: ignore[no-untyped-def]
    builder = PackBuilder(strategies=[GraphSearch(registry.knowledge.graph_store)])
    return builder.build(
        "caching",
        budget=PackBudget(max_items=50, max_tokens=100_000),
        clearance=clearance,  # type: ignore[arg-type]
    )


def test_the_fixture_node_really_carries_the_label(registry: StoreRegistry) -> None:
    node = registry.knowledge.graph_store.get_node(_LABELLED)
    assert node is not None
    assert node["properties"][DATA_CLASSIFICATION_KEY] == {"sensitivity": "restricted"}


def test_a_labelled_node_is_withheld_from_an_internal_pack(
    registry: StoreRegistry,
) -> None:
    pack = _build(registry, "internal")
    served = {i.item_id for i in pack.items}
    assert _PLAIN in served
    assert _LABELLED not in served
    assert _LABELLED in {r.item_id for r in pack.retrieval_report.rejected_items}


def test_a_labelled_node_is_served_to_a_restricted_pack(
    registry: StoreRegistry,
) -> None:
    pack = _build(registry, "restricted")
    assert {i.item_id for i in pack.items} >= {_PLAIN, _LABELLED}
    assert _LABELLED not in {r.item_id for r in pack.retrieval_report.rejected_items}
