"""Ids that are not live documents (#1131): a label applies only to a document.

``seed_ids`` stores a live document and one id of each kind that is not one
(a graph node, a trace, a document that was deleted, an id never stored), and
the assertions check that a refused label verb left every one of them as it
was. Shared by the REST, MCP and CLI suites, which differ only in the door.
"""

from __future__ import annotations

from typing import Any

from tests.unit.retrieve.test_sensitivity import label
from trellis.schemas.classification import sensitivity_of
from trellis.schemas.trace import Trace, TraceContext, TraceSource
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

LIVE = "doc-live"
NODE = "node-1"
GONE = "doc-gone"

#: Every id that is not a live document, by what it is.
OFF_DOCUMENT_KINDS = ["graph_node", "trace", "deleted_document", "never_stored"]

#: The two label verbs, each with its arguments beyond ``doc_id``.
VERBS: dict[str, dict[str, Any]] = {
    "classification.set": {"sensitivity": "restricted"},
    "classification.tag_regulatory": {"tags": ["pii"]},
}


def seed_ids(registry: StoreRegistry) -> dict[str, str]:
    """Store a live document (``LIVE``) and one id of each non-document kind.

    Returns the id of each kind in :data:`OFF_DOCUMENT_KINDS`. The deleted
    document carried a ``restricted`` label while it was alive.
    """
    registry.knowledge.document_store.put(LIVE, "live body", {"t": "x"})
    registry.knowledge.graph_store.upsert_node(NODE, "entity", {"name": "a node"})
    trace = Trace(
        source=TraceSource.AGENT,
        intent="a task",
        steps=[],
        context=TraceContext(agent_id="a-1", domain="d"),
    )
    registry.operational.trace_store.append(trace)
    registry.knowledge.document_store.put(GONE, "to be deleted", label("restricted"))
    assert registry.knowledge.document_store.delete(GONE)
    return {
        "graph_node": NODE,
        "trace": trace.trace_id,
        "deleted_document": GONE,
        "never_stored": "no-such-id",
    }


def stored_label(registry: StoreRegistry, doc_id: str) -> str:
    row = registry.knowledge.document_store.get(doc_id)
    assert row is not None
    return sensitivity_of(row["metadata"])


def assert_nothing_written(registry: StoreRegistry, ids: dict[str, str]) -> None:
    """No document row appeared at a refused id, and the node is as seeded."""
    for doc_id in ids.values():
        assert registry.knowledge.document_store.get(doc_id) is None
    node = registry.knowledge.graph_store.get_node(NODE)
    assert node is not None
    assert node["properties"] == {"name": "a node"}


def rejections(registry: StoreRegistry) -> list[Any]:
    return registry.operational.event_log.get_events(
        event_type=EventType.MUTATION_REJECTED, limit=100
    )
