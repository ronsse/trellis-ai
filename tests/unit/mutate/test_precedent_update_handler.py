"""Tests for PrecedentUpdateHandler — the precedent demote/rollback path.

``Operation.PRECEDENT_UPDATE`` / ``EventType.PRECEDENT_UPDATED`` shipped with
an enum verb and an arg schema (``{"precedent_id"}``) but no registered
handler — the same gap ``entity.update`` had before ``EntityUpdateHandler``.
Today's only caller is the auto-promote auto-demote path
(``trellis.learning.candidate_auto_promote``), which must demote a
policy-made promotion through this governed operation rather than writing
the EventLog directly (the bypass ``PrecedentMiner`` uses and which must not
be copied).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.errors import ValidationError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers import (
    PrecedentUpdateHandler,
    create_curate_handlers,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _create_precedent_node(registry: StoreRegistry, **kwargs: Any) -> str:
    return registry.knowledge.graph_store.upsert_node(
        node_id=kwargs.get("node_id"),
        node_type=kwargs.get("node_type", "precedent"),
        properties=kwargs.get("properties", {"name": "Some precedent"}),
        node_role=kwargs.get("node_role", "semantic"),
        generation_spec=kwargs.get("generation_spec"),
        document_ids=kwargs.get("document_ids"),
    )


class TestPrecedentUpdateHandler:
    def test_registered_in_curate_handlers(self, registry: StoreRegistry) -> None:
        handlers = create_curate_handlers(registry)
        assert Operation.PRECEDENT_UPDATE in handlers
        assert isinstance(handlers[Operation.PRECEDENT_UPDATE], PrecedentUpdateHandler)

    def test_demotes_node_and_emits_event(self, registry: StoreRegistry) -> None:
        node_id = _create_precedent_node(registry)
        handler = PrecedentUpdateHandler(registry)
        cmd = Command(
            operation=Operation.PRECEDENT_UPDATE,
            args={"precedent_id": node_id, "reason": "unhelpful_citation"},
            target_id=node_id,
        )
        returned_id, message = handler.handle(cmd)

        assert returned_id == node_id
        assert node_id in message

        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["demoted"] is True
        assert node["properties"]["demoted_reason"] == "unhelpful_citation"
        assert "demoted_at" in node["properties"]
        # Pre-existing properties survive the merge.
        assert node["properties"]["name"] == "Some precedent"

        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_UPDATED, entity_id=node_id
        )
        assert len(events) == 1
        assert events[0].payload["demoted"] is True
        assert events[0].payload["reason"] == "unhelpful_citation"

    def test_update_creates_new_scd2_version(self, registry: StoreRegistry) -> None:
        node_id = _create_precedent_node(registry)
        handler = PrecedentUpdateHandler(registry)
        handler.handle(
            Command(
                operation=Operation.PRECEDENT_UPDATE,
                args={"precedent_id": node_id},
            )
        )

        history = registry.knowledge.graph_store.get_node_history(node_id)
        assert len(history) == 2  # original version + demoted version
        current = [v for v in history if v["valid_to"] is None]
        closed = [v for v in history if v["valid_to"] is not None]
        assert len(current) == 1
        assert len(closed) == 1
        assert current[0]["properties"]["demoted"] is True
        assert "demoted" not in closed[0]["properties"]

    def test_default_reason_when_omitted(self, registry: StoreRegistry) -> None:
        node_id = _create_precedent_node(registry)
        handler = PrecedentUpdateHandler(registry)
        handler.handle(
            Command(
                operation=Operation.PRECEDENT_UPDATE,
                args={"precedent_id": node_id},
            )
        )
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["demoted_reason"] == "unspecified"

    def test_missing_precedent_is_refused(self, registry: StoreRegistry) -> None:
        handler = PrecedentUpdateHandler(registry)
        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.PRECEDENT_UPDATE,
                    args={"precedent_id": "nonexistent"},
                )
            )
        assert exc_info.value.code == "target_not_found"
        assert str(exc_info.value) == "Node not found: nonexistent"

    def test_missing_precedent_through_executor_is_rejected(
        self, registry: StoreRegistry
    ) -> None:
        """A miss is input to fix (REJECTED), not a store failure (FAILED)."""
        result = build_curate_executor(registry).execute(
            Command(
                operation=Operation.PRECEDENT_UPDATE,
                args={"precedent_id": "nonexistent"},
            )
        )
        assert result.status == CommandStatus.REJECTED

    def test_through_executor_emits_event(self, registry: StoreRegistry) -> None:
        node_id = _create_precedent_node(registry)
        result = build_curate_executor(registry).execute(
            Command(
                operation=Operation.PRECEDENT_UPDATE,
                args={"precedent_id": node_id, "reason": "unhelpful_citation"},
                target_id=node_id,
            )
        )
        assert result.status == CommandStatus.SUCCESS
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_UPDATED, entity_id=node_id
        )
        assert len(events) == 1
