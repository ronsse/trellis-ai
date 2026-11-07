"""``entity.create`` cannot stamp a precedent's ``source_excerpt_status``.

The reviewed promotion stamps ``source_excerpt_status`` on the precedent it
writes, and serving trusts the stamp to exempt a precedent from its source's
label (#1080). ``entity.update`` already refuses a caller-set value; this file
covers ``entity.create``, which replaces a node's whole bag and so could set it
on an existing precedent, on a new precedent-typed node, and at an id in the
precedent namespace.

The promotion itself writes through ``entity.create``, so the refusal cannot
key on the property alone and must not trust a field a caller can set. The
promotion's command carries the reserved ``learning_promotion`` mark in
``Command.metadata``: REST ``/commands/batch`` refuses a caller-planted reserved
key and MCP ``execute_mutation`` has no caller metadata, so no agent-facing
door can produce it.

Defective subject: drop the ``precedent_excerpt_status_via_entity_create``
guard in ``EntityCreateHandler`` and every refusal test here writes the stamp;
make the guard ignore the mark and the promoter test is rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.core.precedent_document import (
    PROMOTION_COMMAND_MARK,
    SOURCE_EXCERPT_STATUS_KEY,
)
from trellis.errors import ValidationError
from trellis.learning.scoring import submit_learning_promotion
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers import EntityCreateHandler
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

CODE = "precedent_excerpt_status_via_entity_create"
STAMP = {SOURCE_EXCERPT_STATUS_KEY: "source_labelled"}


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _create(
    *,
    entity_id: str | None = "n1",
    entity_type: str = "precedent",
    properties: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Command:
    args: dict[str, Any] = {
        "entity_type": entity_type,
        "name": "Pooling",
        "properties": dict(properties or {}),
    }
    if entity_id is not None:
        args["entity_id"] = entity_id
    return Command(
        operation=Operation.ENTITY_CREATE, args=args, metadata=metadata or {}
    )


def _seed(registry: StoreRegistry, node_id: str, node_type: str) -> None:
    registry.knowledge.graph_store.upsert_node(
        node_id=node_id,
        node_type=node_type,
        properties={"name": "Pooling", SOURCE_EXCERPT_STATUS_KEY: "source_included"},
    )


class TestACallerCannotSetTheStatusOnAPrecedent:
    def test_an_existing_precedent_refuses_it(self, registry: StoreRegistry) -> None:
        _seed(registry, "n1", "precedent")

        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(_create(properties=STAMP))

        assert exc_info.value.code == CODE
        node = registry.knowledge.graph_store.get_node("n1")
        assert node is not None
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_included"
        assert len(registry.knowledge.graph_store.get_node_history("n1")) == 1

    def test_a_new_precedent_refuses_it(self, registry: StoreRegistry) -> None:
        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(_create(properties=STAMP))

        assert exc_info.value.code == CODE
        assert registry.knowledge.graph_store.get_node("n1") is None

    def test_a_new_precedent_with_a_generated_id_refuses_it(
        self, registry: StoreRegistry
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(
                _create(entity_id=None, properties=STAMP)
            )

        assert exc_info.value.code == CODE
        assert registry.knowledge.graph_store.count_nodes() == 0

    def test_a_retype_to_precedent_in_the_same_call_refuses_it(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, "n1", "concept")

        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(_create(properties=STAMP))

        assert exc_info.value.code == CODE
        node = registry.knowledge.graph_store.get_node("n1")
        assert node is not None
        assert node["node_type"] == "concept"

    def test_an_id_in_the_precedent_namespace_refuses_it(
        self, registry: StoreRegistry
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(
                _create(
                    entity_id="precedent://learning/forged",
                    entity_type="concept",
                    properties=STAMP,
                )
            )

        assert exc_info.value.code == CODE
        assert registry.knowledge.graph_store.count_nodes() == 0

    def test_the_governed_path_rejects_it_and_audits_the_reason(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, "n1", "precedent")

        result = build_curate_executor(registry, clearance="restricted").execute(
            _create(properties=STAMP)
        )

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CODE
        rejected = registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
        assert [(e.payload["command_id"], e.payload["reason"]) for e in rejected] == [
            (result.command_id, CODE)
        ]

    def test_a_mark_set_to_anything_but_true_does_not_exempt(
        self, registry: StoreRegistry
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(
                _create(properties=STAMP, metadata={PROMOTION_COMMAND_MARK: "true"})
            )

        assert exc_info.value.code == CODE


class TestWhatStaysAccepted:
    def test_the_promotions_marked_command_stamps_a_precedent(
        self, registry: StoreRegistry
    ) -> None:
        result = build_curate_executor(registry, clearance="restricted").execute(
            _create(properties=STAMP, metadata={PROMOTION_COMMAND_MARK: True})
        )

        assert result.status == CommandStatus.SUCCESS
        node = registry.knowledge.graph_store.get_node("n1")
        assert node is not None
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_labelled"

    def test_a_precedent_create_that_does_not_name_the_status_is_unaffected(
        self, registry: StoreRegistry
    ) -> None:
        result = build_curate_executor(registry, clearance="restricted").execute(
            _create(properties={"summary": "pool connections"})
        )

        assert result.status == CommandStatus.SUCCESS

    def test_an_ordinary_node_keeps_the_property_as_any_other(
        self, registry: StoreRegistry
    ) -> None:
        result = build_curate_executor(registry, clearance="restricted").execute(
            _create(entity_type="concept", properties={SOURCE_EXCERPT_STATUS_KEY: "x"})
        )

        assert result.status == CommandStatus.SUCCESS
        node = registry.knowledge.graph_store.get_node("n1")
        assert node is not None
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "x"

    def test_the_real_promotion_still_writes_its_stamp(
        self, registry: StoreRegistry
    ) -> None:
        """``submit_learning_promotion`` carries the mark, so its stamp lands."""
        executor = build_curate_executor(registry, clearance="restricted")
        outcome = submit_learning_promotion(
            executor,
            entity_payload={
                "entity_type": "precedent",
                "entity_id": "precedent://learning/p1",
                "name": "Pooling",
                "properties": {
                    "summary": "pool connections",
                    SOURCE_EXCERPT_STATUS_KEY: "source_included",
                },
            },
            edge_payloads=[],
            graph_store=registry.knowledge.graph_store,
            event_log=registry.operational.event_log,
            document_store=registry.knowledge.document_store,
            requested_by="test",
        )

        assert outcome["status"] != "entity_failed", outcome
        node = registry.knowledge.graph_store.get_node("precedent://learning/p1")
        assert node is not None
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_included"
