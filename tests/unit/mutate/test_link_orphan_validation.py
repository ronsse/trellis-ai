"""Pre-flight FK validation for the LINK_CREATE handler.

Covers the contract added in unit B1: the handler must reject edges
whose endpoints don't reference an existing entity *before* any side
effect (no ``upsert_edge`` call, no ``LINK_CREATED`` event). Tested
in isolation against a ``MagicMock(spec=GraphStore)`` so the suite
stays fast and backend-agnostic.

The legacy CLI ``graph-health`` command still detects orphan *nodes*
post-hoc; this handler closes the door on orphan *edges* at ingest.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.errors import ValidationError
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers import LinkCreateHandler
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.graph import GraphStore


def _node(node_id: str) -> dict[str, Any]:
    """Minimal current-version node row, enough to satisfy ``_resolve_node``."""
    return {
        "node_id": node_id,
        "node_type": "concept",
        "node_role": "semantic",
        "generation_spec": None,
        "document_ids": [],
        "properties": {"name": node_id},
        "created_at": None,
        "updated_at": None,
        "valid_from": None,
        "valid_to": None,
    }


def _registry_with_known_nodes(*known: str) -> tuple[Any, MagicMock, MagicMock]:
    """Build a fake registry whose graph store recognises ``known`` IDs.

    Returns ``(registry, graph_store, event_log)`` so tests can assert
    on call patterns. ``graph_store.get_node`` returns a node row when
    the ID is known and ``None`` otherwise; ``store.query`` returns an
    empty list (no entity_id property fallback hits in these tests).
    """
    graph_store = MagicMock(spec=GraphStore)
    known_set = set(known)
    graph_store.get_node.side_effect = lambda nid, *_a, **_kw: (
        _node(nid) if nid in known_set else None
    )
    graph_store.query.return_value = []
    graph_store.upsert_edge.return_value = "edge-1"

    event_log = MagicMock(spec=EventLog)
    event_log.emit.return_value = MagicMock(event_id="evt-1")

    knowledge = MagicMock()
    knowledge.graph_store = graph_store
    operational = MagicMock()
    operational.event_log = event_log

    registry = MagicMock()
    registry.knowledge = knowledge
    registry.operational = operational
    return registry, graph_store, event_log


def _emitted_event_types(event_log: MagicMock) -> list[EventType]:
    """All :class:`EventType` values passed to ``event_log.emit``."""
    return [call.args[0] for call in event_log.emit.call_args_list]


def _link_cmd(
    source_id: str,
    target_id: str,
    *,
    allow_dangling: bool = False,
) -> Command:
    args: dict[str, Any] = {
        "source_id": source_id,
        "target_id": target_id,
        "edge_kind": "related_to",
    }
    if allow_dangling:
        args["allow_dangling"] = True
    return Command(operation=Operation.LINK_CREATE, args=args)


class TestLinkCreateFKValidation:
    """The handler rejects dangling endpoints before any side effect."""

    def test_both_endpoints_present_succeeds(self) -> None:
        registry, graph_store, event_log = _registry_with_known_nodes("a", "b")
        handler = LinkCreateHandler(registry)

        edge_id, message = handler.handle(_link_cmd("a", "b"))

        assert edge_id == "edge-1"
        assert "related_to" in message
        graph_store.upsert_edge.assert_called_once()
        assert EventType.LINK_CREATED in _emitted_event_types(event_log)

    def test_missing_source_raises_and_emits_no_event(self) -> None:
        registry, graph_store, event_log = _registry_with_known_nodes("b")
        handler = LinkCreateHandler(registry)

        with pytest.raises(ValidationError) as excinfo:
            handler.handle(_link_cmd("ghost", "b"))

        # Message names which side failed and which ID was attempted.
        assert "source_id='ghost'" in str(excinfo.value)
        # No write, no LINK_CREATED event.
        graph_store.upsert_edge.assert_not_called()
        assert EventType.LINK_CREATED not in _emitted_event_types(event_log)

    def test_missing_target_raises_and_emits_no_event(self) -> None:
        registry, graph_store, event_log = _registry_with_known_nodes("a")
        handler = LinkCreateHandler(registry)

        with pytest.raises(ValidationError) as excinfo:
            handler.handle(_link_cmd("a", "ghost"))

        assert "target_id='ghost'" in str(excinfo.value)
        graph_store.upsert_edge.assert_not_called()
        assert EventType.LINK_CREATED not in _emitted_event_types(event_log)

    def test_both_missing_raises_mentioning_both(self) -> None:
        registry, graph_store, event_log = _registry_with_known_nodes()
        handler = LinkCreateHandler(registry)

        with pytest.raises(ValidationError) as excinfo:
            handler.handle(_link_cmd("ghost-s", "ghost-t"))

        msg = str(excinfo.value)
        # Both endpoints surfaced in the same error so callers can
        # report all root causes in a single round trip.
        assert "source_id='ghost-s'" in msg
        assert "target_id='ghost-t'" in msg
        # ``ValidationError.errors`` carries the per-side reasons.
        assert len(excinfo.value.errors) == 2
        graph_store.upsert_edge.assert_not_called()
        assert EventType.LINK_CREATED not in _emitted_event_types(event_log)

    def test_allow_dangling_skips_fk_check(self) -> None:
        # Neither endpoint exists in the graph store. Without the
        # escape hatch this would raise; with it, the edge writes through.
        registry, graph_store, event_log = _registry_with_known_nodes()
        handler = LinkCreateHandler(registry)

        edge_id, message = handler.handle(
            _link_cmd("ghost-s", "ghost-t", allow_dangling=True)
        )

        assert edge_id == "edge-1"
        assert "related_to" in message
        # No FK lookups happened — the fast path skips ``get_node`` entirely.
        graph_store.get_node.assert_not_called()
        graph_store.query.assert_not_called()
        graph_store.upsert_edge.assert_called_once()
        # Success path still emits the canonical LINK_CREATED event.
        assert EventType.LINK_CREATED in _emitted_event_types(event_log)
