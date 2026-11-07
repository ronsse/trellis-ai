"""``entity.create`` and ``entity.update`` keep the precedent type honest (#1080).

Two ways to put a node in a state the read-side gate (``PrecedentClearance``)
does not expect, both closed here for every writer except the reviewed
promotion:

* **Retype to precedent.** ``source_excerpt_status`` is an ordinary property on
  a non-precedent node, so it can be stored there and the node then retyped to
  ``precedent`` through ``entity.update``, which carries the stored bag forward.
  Serving trusts the stamp on a precedent, so the update is refused when the
  node it would retype already holds the key.
* **Precedent namespace under another type.** The read gate keys on the
  ``precedent`` type, but ``precedent://learning/`` is judged by id: a node there
  stored as another type is left out of a ``/graph/search`` page without being
  subtracted from its ``total``. A write that would leave a node under the
  namespace with a type other than ``precedent`` is refused.

The promotion's command carries the reserved ``learning_promotion`` mark in
``Command.metadata`` (see ``test_entity_create_excerpt_status.py``); both checks
exempt it and nothing else.

Defective subject: drop the retype check in ``EntityUpdateHandler`` and the
retype tests write a stamped precedent; drop the namespace check in either
handler and its refusal tests store the node; make a check ignore the mark and
its promoter test is rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.core.precedent_document import (
    PRECEDENT_ENTITY_ID_PREFIX,
    PROMOTION_COMMAND_MARK,
    SOURCE_EXCERPT_STATUS_KEY,
)
from trellis.errors import ValidationError
from trellis.learning.scoring import submit_learning_promotion
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandResult, CommandStatus, Operation
from trellis.mutate.handlers import EntityCreateHandler, EntityUpdateHandler
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

CREATE_NAMESPACE = "precedent_namespace_type_via_entity_create"
UPDATE_NAMESPACE = "precedent_namespace_type_via_entity_update"
RETYPE = "precedent_retype_excerpt_status_via_entity_update"

IN_NAMESPACE = f"{PRECEDENT_ENTITY_ID_PREFIX}p1"
MARK = {PROMOTION_COMMAND_MARK: True}
STAMPED = {"name": "Pooling", SOURCE_EXCERPT_STATUS_KEY: "source_labelled"}


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _execute(registry: StoreRegistry, command: Command) -> CommandResult:
    return build_curate_executor(registry, clearance="restricted").execute(command)


def _create(
    node_id: str,
    node_type: str,
    *,
    properties: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_id": node_id,
            "entity_type": node_type,
            "name": "Pooling",
            "properties": dict(properties or {}),
        },
        metadata=metadata or {},
    )


def _update(
    node_id: str,
    *,
    node_type: str | None = None,
    properties: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Command:
    args: dict[str, Any] = {
        "entity_id": node_id,
        "properties": dict(properties or {"note": "edited"}),
    }
    if node_type is not None:
        args["entity_type"] = node_type
    return Command(
        operation=Operation.ENTITY_UPDATE, args=args, metadata=metadata or {}
    )


def _seed(
    registry: StoreRegistry,
    node_id: str,
    node_type: str,
    properties: dict[str, Any] | None = None,
) -> None:
    """Store a node directly, as a write the handlers did not judge would."""
    registry.knowledge.graph_store.upsert_node(
        node_id=node_id,
        node_type=node_type,
        properties=dict(properties or {"name": "Pooling"}),
    )


def _node(registry: StoreRegistry, node_id: str) -> dict[str, Any]:
    node = registry.knowledge.graph_store.get_node(node_id)
    assert node is not None
    return node


def _rejection_reasons(registry: StoreRegistry) -> list[str]:
    return [
        e.payload["reason"]
        for e in registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
    ]


class TestCreateRefusesTheNamespaceUnderAnotherType:
    def test_a_new_node_is_refused_and_nothing_is_stored(
        self, registry: StoreRegistry
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(_create(IN_NAMESPACE, "concept"))

        assert exc_info.value.code == CREATE_NAMESPACE
        assert PRECEDENT_ENTITY_ID_PREFIX in str(exc_info.value)
        assert registry.knowledge.graph_store.get_node(IN_NAMESPACE) is None

    def test_an_existing_misfiled_node_is_not_upserted(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, IN_NAMESPACE, "concept", {"name": "Old"})

        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(_create(IN_NAMESPACE, "document"))

        assert exc_info.value.code == CREATE_NAMESPACE
        node = _node(registry, IN_NAMESPACE)
        assert node["node_type"] == "concept"
        assert node["properties"] == {"name": "Old"}

    def test_the_governed_path_rejects_it_and_audits_the_reason(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(registry, _create(IN_NAMESPACE, "concept"))

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CREATE_NAMESPACE
        assert _rejection_reasons(registry) == [CREATE_NAMESPACE]
        assert registry.knowledge.graph_store.get_node(IN_NAMESPACE) is None

    def test_a_mark_set_to_anything_but_true_does_not_exempt(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(
            registry,
            _create(IN_NAMESPACE, "concept", metadata={PROMOTION_COMMAND_MARK: "true"}),
        )

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CREATE_NAMESPACE

    def test_the_promotions_marked_command_is_exempt(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(registry, _create(IN_NAMESPACE, "concept", metadata=MARK))

        assert result.status == CommandStatus.SUCCESS
        assert _node(registry, IN_NAMESPACE)["node_type"] == "concept"

    def test_a_precedent_in_the_namespace_is_accepted(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(registry, _create(IN_NAMESPACE, "precedent"))

        assert result.status == CommandStatus.SUCCESS
        assert _node(registry, IN_NAMESPACE)["node_type"] == "precedent"

    def test_the_real_promotion_still_writes_its_precedent(
        self, registry: StoreRegistry
    ) -> None:
        outcome = submit_learning_promotion(
            build_curate_executor(registry, clearance="restricted"),
            entity_payload={
                "entity_type": "precedent",
                "entity_id": IN_NAMESPACE,
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
        assert _node(registry, IN_NAMESPACE)["node_type"] == "precedent"

    @pytest.mark.parametrize(
        "node_id",
        [
            "n1",
            "precedent://other/p1",
            "Precedent://learning/p1",
            "x/precedent://learning/p1",
        ],
    )
    def test_any_other_id_keeps_any_type(
        self, registry: StoreRegistry, node_id: str
    ) -> None:
        result = _execute(registry, _create(node_id, "concept"))

        assert result.status == CommandStatus.SUCCESS
        assert _node(registry, node_id)["node_type"] == "concept"


class TestUpdateRefusesTheNamespaceUnderAnotherType:
    def test_a_retype_of_a_misfiled_node_to_another_type_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, IN_NAMESPACE, "concept")

        result = _execute(registry, _update(IN_NAMESPACE, node_type="document"))

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == UPDATE_NAMESPACE
        assert _rejection_reasons(registry) == [UPDATE_NAMESPACE]
        node = _node(registry, IN_NAMESPACE)
        assert node["node_type"] == "concept"
        assert "note" not in node["properties"]
        assert len(registry.knowledge.graph_store.get_node_history(IN_NAMESPACE)) == 1

    def test_an_edit_that_leaves_a_misfiled_node_misfiled_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, IN_NAMESPACE, "concept")

        with pytest.raises(ValidationError) as exc_info:
            EntityUpdateHandler(registry).handle(_update(IN_NAMESPACE))

        assert exc_info.value.code == UPDATE_NAMESPACE
        assert "note" not in _node(registry, IN_NAMESPACE)["properties"]

    def test_retyping_a_misfiled_node_to_precedent_is_the_way_out(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, IN_NAMESPACE, "concept")

        result = _execute(registry, _update(IN_NAMESPACE, node_type="precedent"))

        assert result.status == CommandStatus.SUCCESS
        assert _node(registry, IN_NAMESPACE)["node_type"] == "precedent"

    def test_the_promotions_marked_command_is_exempt(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, IN_NAMESPACE, "concept")

        result = _execute(
            registry, _update(IN_NAMESPACE, node_type="document", metadata=MARK)
        )

        assert result.status == CommandStatus.SUCCESS
        assert _node(registry, IN_NAMESPACE)["node_type"] == "document"

    def test_a_precedent_in_the_namespace_is_edited_as_before(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, IN_NAMESPACE, "precedent")

        for node_type in (None, "precedent"):
            result = _execute(registry, _update(IN_NAMESPACE, node_type=node_type))

            assert result.status == CommandStatus.SUCCESS, node_type
        node = _node(registry, IN_NAMESPACE)
        assert node["node_type"] == "precedent"
        assert node["properties"]["note"] == "edited"

    def test_a_precedent_cannot_be_retyped_away_at_all(
        self, registry: StoreRegistry
    ) -> None:
        """The older guard still answers first, with its own code."""
        _seed(registry, IN_NAMESPACE, "precedent")

        result = _execute(registry, _update(IN_NAMESPACE, node_type="concept"))

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "precedent_entity_type_change_refused"
        )

    def test_any_other_id_keeps_any_type(self, registry: StoreRegistry) -> None:
        _seed(registry, "n1", "concept")

        for node_type in (None, "document", "concept"):
            result = _execute(registry, _update("n1", node_type=node_type))

            assert result.status == CommandStatus.SUCCESS, node_type


class TestUpdateRefusesARetypeToPrecedentThatCarriesTheStamp:
    def test_a_node_created_with_the_stamp_cannot_be_retyped(
        self, registry: StoreRegistry
    ) -> None:
        created = _execute(registry, _create("n1", "concept", properties=STAMPED))
        assert created.status == CommandStatus.SUCCESS

        result = _execute(registry, _update("n1", node_type="precedent"))

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == RETYPE
        assert _rejection_reasons(registry) == [RETYPE]
        node = _node(registry, "n1")
        assert node["node_type"] == "concept"
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_labelled"
        assert len(registry.knowledge.graph_store.get_node_history("n1")) == 1

    def test_the_message_names_the_rule(self, registry: StoreRegistry) -> None:
        _seed(registry, "n1", "concept", STAMPED)

        with pytest.raises(ValidationError) as exc_info:
            EntityUpdateHandler(registry).handle(_update("n1", node_type="precedent"))

        assert SOURCE_EXCERPT_STATUS_KEY in str(exc_info.value)
        assert "precedent" in str(exc_info.value)

    def test_a_stamp_with_no_value_still_counts(self, registry: StoreRegistry) -> None:
        _seed(registry, "n1", "concept", {"name": "x", SOURCE_EXCERPT_STATUS_KEY: None})

        result = _execute(registry, _update("n1", node_type="precedent"))

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == RETYPE

    def test_the_promotions_marked_command_is_exempt(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, "n1", "concept", STAMPED)

        result = _execute(registry, _update("n1", node_type="precedent", metadata=MARK))

        assert result.status == CommandStatus.SUCCESS
        node = _node(registry, "n1")
        assert node["node_type"] == "precedent"
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_labelled"

    def test_a_mark_set_to_anything_but_true_does_not_exempt(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, "n1", "concept", STAMPED)

        result = _execute(
            registry,
            _update("n1", node_type="precedent", metadata={PROMOTION_COMMAND_MARK: 1}),
        )

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == RETYPE

    def test_a_retype_of_a_node_without_the_stamp_is_unaffected(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, "n1", "concept")

        result = _execute(registry, _update("n1", node_type="precedent"))

        assert result.status == CommandStatus.SUCCESS
        node = _node(registry, "n1")
        assert node["node_type"] == "precedent"
        assert SOURCE_EXCERPT_STATUS_KEY not in node["properties"]

    def test_a_stamped_non_precedent_keeps_its_stamp_through_other_edits(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, "n1", "concept", STAMPED)

        for node_type in (None, "document"):
            result = _execute(registry, _update("n1", node_type=node_type))

            assert result.status == CommandStatus.SUCCESS, node_type
        node = _node(registry, "n1")
        assert node["node_type"] == "document"
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_labelled"

    def test_a_promoted_precedent_kept_as_precedent_is_unchanged(
        self, registry: StoreRegistry
    ) -> None:
        outcome = submit_learning_promotion(
            build_curate_executor(registry, clearance="restricted"),
            entity_payload={
                "entity_type": "precedent",
                "entity_id": IN_NAMESPACE,
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

        result = _execute(registry, _update(IN_NAMESPACE, node_type="precedent"))

        assert result.status == CommandStatus.SUCCESS
        node = _node(registry, IN_NAMESPACE)
        assert node["node_type"] == "precedent"
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_included"
        assert node["properties"]["note"] == "edited"

    def test_the_stamp_named_in_the_same_call_keeps_its_own_code(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, "n1", "concept", STAMPED)

        result = _execute(
            registry,
            _update(
                "n1",
                node_type="precedent",
                properties={SOURCE_EXCERPT_STATUS_KEY: "source_labelled"},
            ),
        )

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "precedent_excerpt_status_via_entity_update"
        )

    def test_a_stamp_stored_between_the_read_and_the_write_is_still_refused(
        self, registry: StoreRegistry
    ) -> None:
        """The check runs on the version the write replaces, not on an earlier read."""
        _seed(registry, "n1", "concept")
        graph = registry.knowledge.graph_store
        real = graph.update_node_if_current
        raced = {"done": False}

        def stamp_first(node_id: str, valid_from: Any, **fields: Any) -> bool:
            if not raced["done"]:
                raced["done"] = True
                current = graph.get_node(node_id)
                assert current is not None
                assert graph.update_node_if_current(
                    node_id,
                    current["valid_from"],
                    node_type=current["node_type"],
                    properties={**current["properties"], **STAMPED},
                    node_role=current.get("node_role", "semantic"),
                    generation_spec=current.get("generation_spec"),
                    document_ids=current.get("document_ids") or None,
                )
            return real(node_id, valid_from, **fields)

        graph.update_node_if_current = stamp_first  # type: ignore[method-assign]

        result = _execute(registry, _update("n1", node_type="precedent"))

        assert raced["done"]
        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == RETYPE
        node = _node(registry, "n1")
        assert node["node_type"] == "concept"
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_labelled"
