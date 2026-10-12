"""Tests for EntityUpdateHandler + document_ids threading through create.

Covers issue #260: the ``entity.update`` verb shipped in the Operation enum
with no registered handler, and ``EntityCreateHandler`` did not thread the
``document_ids`` graph↔document link. These tests pin the SCD-2 update
semantics, the carry-forward / replace rules for ``document_ids``, event
emission, and the refusal of an ``entity_id`` that names no node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.mutate._purge_race import redaction, rival_inside_first_read
from trellis.errors import ValidationError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers import (
    EntityCreateHandler,
    EntityUpdateHandler,
    create_curate_handlers,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _create_node(registry: StoreRegistry, **kwargs: Any) -> str:
    return registry.knowledge.graph_store.upsert_node(
        node_id=kwargs.get("node_id"),
        node_type=kwargs.get("node_type", "concept"),
        properties=kwargs.get("properties", {"name": "Original"}),
        node_role=kwargs.get("node_role", "semantic"),
        generation_spec=kwargs.get("generation_spec"),
        document_ids=kwargs.get("document_ids"),
    )


class TestEntityUpdateHandler:
    def test_registered_in_curate_handlers(self, registry: StoreRegistry) -> None:
        handlers = create_curate_handlers(registry)
        assert Operation.ENTITY_UPDATE in handlers
        assert isinstance(handlers[Operation.ENTITY_UPDATE], EntityUpdateHandler)

    def test_merges_properties_and_emits_event(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        handler = EntityUpdateHandler(registry)
        cmd = Command(
            operation=Operation.ENTITY_UPDATE,
            args={"entity_id": node_id, "properties": {"status": "active"}},
        )
        returned_id, message = handler.handle(cmd)

        assert returned_id == node_id
        assert node_id in message
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        # Merge, not replace: the pre-existing ``name`` survives.
        assert node["properties"]["status"] == "active"
        assert node["properties"]["name"] == "Original"

        events = registry.operational.event_log.get_events(
            event_type=EventType.ENTITY_UPDATED
        )
        assert len(events) == 1
        assert events[0].entity_id == node_id

    def test_update_creates_new_scd2_version(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        handler = EntityUpdateHandler(registry)
        handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "name": "Renamed"},
            )
        )

        history = registry.knowledge.graph_store.get_node_history(node_id)
        assert len(history) == 2  # original version + updated version

        current = [v for v in history if v["valid_to"] is None]
        closed = [v for v in history if v["valid_to"] is not None]
        assert len(current) == 1
        assert len(closed) == 1
        assert current[0]["properties"]["name"] == "Renamed"
        assert closed[0]["properties"]["name"] == "Original"

    def test_name_arg_updates_name(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        handler = EntityUpdateHandler(registry)
        handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "name": "New Name"},
            )
        )
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["name"] == "New Name"

    def test_document_ids_carried_forward_when_omitted(
        self, registry: StoreRegistry
    ) -> None:
        node_id = _create_node(registry, document_ids=["doc-1"])
        handler = EntityUpdateHandler(registry)
        handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "properties": {"k": "v"}},
            )
        )
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        # A version bump must not silently drop the pointer-not-prose link.
        assert node["document_ids"] == ["doc-1"]

    def test_document_ids_replaced_when_provided(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry, document_ids=["doc-1"])
        handler = EntityUpdateHandler(registry)
        handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "document_ids": ["doc-2"]},
            )
        )
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["document_ids"] == ["doc-2"]

    def test_preserves_node_role_across_update(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry, node_role="structural")
        handler = EntityUpdateHandler(registry)
        handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "properties": {"k": "v"}},
            )
        )
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        # node_role is immutable across versions; carried forward, not reset.
        assert node["node_role"] == "structural"

    def test_preserves_generation_spec_across_update(
        self, registry: StoreRegistry
    ) -> None:
        spec = {"generator": "test-gen", "version": "1", "inputs": ["a"]}
        node_id = _create_node(registry, node_role="curated", generation_spec=spec)
        handler = EntityUpdateHandler(registry)
        handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "properties": {"k": "v"}},
            )
        )
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        # generation_spec is immutable across versions; a version bump must
        # carry it forward — a curated node without its spec would fail the
        # graph store's role validation and lose its regeneration audit trail.
        assert node["generation_spec"] == spec
        assert node["node_role"] == "curated"

    def test_missing_entity_is_refused(self, registry: StoreRegistry) -> None:
        handler = EntityUpdateHandler(registry)
        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.ENTITY_UPDATE,
                    args={"entity_id": "nonexistent"},
                )
            )
        assert exc_info.value.code == "target_not_found"
        assert str(exc_info.value) == "Node not found: nonexistent"

    def test_missing_entity_through_executor_is_rejected(
        self, registry: StoreRegistry
    ) -> None:
        """A miss is input to fix (REJECTED), not a store failure (FAILED)."""
        node_id = _create_node(registry)
        executor = build_curate_executor(registry)
        applied = executor.execute(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "properties": {"phase": "2"}},
            )
        )
        missing = executor.execute(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": "nope", "properties": {"phase": "2"}},
            )
        )

        assert applied.status == CommandStatus.SUCCESS
        assert missing.status == CommandStatus.REJECTED
        assert missing.message == "Node not found: nope"
        assert missing.metadata["rejection_reason"] == "target_not_found"
        rejected = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
        assert [
            (e.payload["command_id"], e.payload["status"], e.payload["reason"])
            for e in rejected
        ] == [(missing.command_id, "rejected", "target_not_found")]

    def test_happy_path_through_executor(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        executor = build_curate_executor(registry)
        result = executor.execute(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "properties": {"phase": "2"}},
            )
        )
        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == node_id
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["phase"] == "2"


class TestEntityUpdateRacingAnotherWrite:
    """A write that commits between the handler's read and its write wins."""

    @pytest.mark.parametrize(
        "args",
        [{"properties": {"phase": "2"}}, {"name": "Renamed"}],
        ids=["properties", "name"],
    )
    def test_purged_mid_command_is_refused_and_stays_purged(
        self, registry: StoreRegistry, args: dict[str, Any]
    ) -> None:
        executor = build_curate_executor(registry)
        graph = registry.knowledge.graph_store
        created = executor.execute(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={"entity_type": "person", "name": "Original"},
            )
        )
        node_id = created.created_id
        assert node_id is not None
        assert len(graph.get_aliases(node_id)) == 1
        update = Command(
            operation=Operation.ENTITY_UPDATE, args={"entity_id": node_id, **args}
        )

        with rival_inside_first_read(
            registry, executor, node_id, redaction(node_id)
        ) as rival:
            loser = executor.execute(update)

        [purge] = rival
        assert purge.status == CommandStatus.SUCCESS
        assert loser.status == CommandStatus.REJECTED
        assert loser.message == f"Node not found: {node_id}"
        assert loser.metadata["rejection_reason"] == "target_not_found"
        assert graph.get_node(node_id) is None
        assert graph.get_node_history(node_id) == []
        assert graph.get_aliases(node_id) == []
        events = registry.operational.event_log
        assert events.get_events(event_type=EventType.ENTITY_UPDATED) == []
        rejected = events.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [(e.payload["command_id"], e.payload["reason"]) for e in rejected] == [
            (loser.command_id, "target_not_found")
        ]

    def test_an_update_committed_mid_command_is_kept(
        self, registry: StoreRegistry
    ) -> None:
        node_id = _create_node(registry)
        executor = build_curate_executor(registry)
        graph = registry.knowledge.graph_store
        first = Command(
            operation=Operation.ENTITY_UPDATE,
            args={"entity_id": node_id, "properties": {"owner": "rival"}},
        )
        second = Command(
            operation=Operation.ENTITY_UPDATE,
            args={"entity_id": node_id, "properties": {"phase": "2"}},
        )

        with rival_inside_first_read(registry, executor, node_id, first) as rival:
            result = executor.execute(second)

        [committed] = rival
        assert committed.status == CommandStatus.SUCCESS
        assert result.status == CommandStatus.SUCCESS
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"] == {
            "name": "Original",
            "owner": "rival",
            "phase": "2",
        }
        assert len(graph.get_node_history(node_id)) == 3

    def test_a_node_that_never_settles_fails_after_five_attempts(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node_id = _create_node(registry)
        executor = build_curate_executor(registry)
        graph = registry.knowledge.graph_store
        attempts: list[str] = []

        def always_stale(nid: str, *args: Any, **kwargs: Any) -> bool:
            attempts.append(nid)
            if len(attempts) > 100:
                msg = "retry is unbounded"
                raise RuntimeError(msg)
            return False

        monkeypatch.setattr(graph, "update_node_if_current", always_stale)
        result = executor.execute(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "properties": {"phase": "2"}},
            )
        )

        assert result.status == CommandStatus.FAILED
        assert attempts == [node_id] * 5
        node = graph.get_node(node_id)
        assert node is not None
        assert node["properties"] == {"name": "Original"}
        assert len(graph.get_node_history(node_id)) == 1
        events = registry.operational.event_log
        assert events.get_events(event_type=EventType.ENTITY_UPDATED) == []
        rejected = events.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [(e.payload["command_id"], e.payload["status"]) for e in rejected] == [
            (result.command_id, "failed")
        ]


class TestEntityCreateDocumentIds:
    def test_threads_document_ids(self, registry: StoreRegistry) -> None:
        handler = EntityCreateHandler(registry)
        node_id, _ = handler.handle(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_type": "concept",
                    "name": "E",
                    "document_ids": ["doc-42"],
                },
            )
        )
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["document_ids"] == ["doc-42"]

        # The link rides the ENTITY_CREATED audit payload too.
        events = registry.operational.event_log.get_events(
            event_type=EventType.ENTITY_CREATED
        )
        assert len(events) == 1
        assert events[0].payload["document_ids"] == ["doc-42"]

    def test_create_without_document_ids_has_empty_link(
        self, registry: StoreRegistry
    ) -> None:
        handler = EntityCreateHandler(registry)
        node_id, _ = handler.handle(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={"entity_type": "concept", "name": "E"},
            )
        )
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["document_ids"] == []
