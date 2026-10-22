"""``get_task_context(entity_ids=...)`` anchors the graph axis on those entities.

A caller that names the entity it is about to touch has told the pack
exactly where to look. Until now the ids reached the section router
(``TierMapper``: "an item whose id is listed matches this section") and
nothing else, so the graph axis still served its newest-nodes window and a
precedent attached to the named entity was absent whenever enough newer
nodes existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import get_task_context as _get_task_context

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_task_context = unwrap_tool(_get_task_context)

#: Names nothing the seed extractor can resolve: no word of it is a node
#: name, alias or ``domain:``/``tool:``/... id.
INTENT = "do the next step"

ENTITY = "tbl:ledger"
PRECEDENT = "precedent:ledger-rule"
PRECEDENT_BODY = "Always reconcile the ledger before closing the period"

#: More than ``limit`` (20) newer nodes, so the recency window cannot
#: reach anything written before them.
_NEWER = 30


def _seed(registry: StoreRegistry) -> None:
    """An old precedent on an old entity, then a pile of newer nodes."""
    graph = registry.knowledge.graph_store
    graph.upsert_node(ENTITY, "table", {"name": "ledger", "description": "x" * 60})
    graph.upsert_node(
        PRECEDENT, "precedent", {"name": "ledger rule", "description": PRECEDENT_BODY}
    )
    graph.upsert_edge(PRECEDENT, ENTITY, "precedent_applies_to")
    for i in range(_NEWER):
        graph.upsert_node(
            f"concept:filler-{i}",
            "concept",
            {"name": f"filler {i}", "description": f"unrelated filler number {i} " * 4},
        )


def test_the_precedent_is_absent_without_entity_ids(
    temp_registry: StoreRegistry,
) -> None:
    """The control: the scenario really does bury the precedent."""
    _seed(temp_registry)

    assert PRECEDENT not in get_task_context(INTENT)


def test_entity_ids_serve_the_precedent_attached_to_them(
    temp_registry: StoreRegistry,
) -> None:
    _seed(temp_registry)

    result = get_task_context(INTENT, entity_ids=[ENTITY])

    assert PRECEDENT in result
