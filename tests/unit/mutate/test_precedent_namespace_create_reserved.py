"""Only the promotion creates under ``precedent://learning/`` (#1080).

``entity.create`` once refused the namespace only for a type other than
``precedent`` and for a caller-set ``source_excerpt_status``. A caller that left
the stamp out could still create a ``precedent`` there, which the read gate
then judges as a promoted precedent. Every create at an id in the namespace now
needs the promotion's reserved mark, whatever the type and whether the id is
absent or already holds a node.

The code names the rule the command broke, most specific first (see
``EntityCreateHandler``): the existing precedent's own rules, the excerpt
status, the type, and last ``precedent_namespace_via_entity_create``.

Defective subject: drop the new refusal in ``EntityCreateHandler`` and the
plain-create tests store the node; make it ignore the mark and the promoter
tests are rejected; run it before the narrower checks and the ordering tests
answer with the wrong code.
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
from trellis.mutate.commands import (
    OUTCOME_LEFT_ALONE,
    OUTCOME_METADATA_KEY,
    Command,
    CommandResult,
    CommandStatus,
    Operation,
)
from trellis.mutate.handlers import EntityCreateHandler, EntityUpdateHandler
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

CODE = "precedent_namespace_via_entity_create"
TYPE_CODE = "precedent_namespace_type_via_entity_create"
STATUS_CODE = "precedent_excerpt_status_via_entity_create"
RETYPE_CODE = "entity_create_retypes_existing_precedent"
OVER_CODE = "entity_create_over_existing_precedent"

NODE_ID = f"{PRECEDENT_ENTITY_ID_PREFIX}p1"
MARK = {PROMOTION_COMMAND_MARK: True}


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _execute(registry: StoreRegistry, command: Command) -> CommandResult:
    return build_curate_executor(registry, clearance="restricted").execute(command)


def _create(
    node_id: str = NODE_ID,
    node_type: str = "precedent",
    *,
    properties: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    args: dict[str, Any] | None = None,
    **extra_args: Any,
) -> Command:
    """``metadata`` is the command's; ``args`` and keywords are its arguments."""
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={
            "entity_id": node_id,
            "entity_type": node_type,
            "name": "Pooling",
            "properties": dict(properties or {}),
            **(args or {}),
            **extra_args,
        },
        metadata=metadata or {},
    )


def _update(
    node_id: str = NODE_ID, *, properties: dict[str, Any], **extra_args: Any
) -> Command:
    return Command(
        operation=Operation.ENTITY_UPDATE,
        args={"entity_id": node_id, "properties": properties, **extra_args},
    )


def _seed(
    registry: StoreRegistry,
    node_type: str = "precedent",
    properties: dict[str, Any] | None = None,
) -> None:
    registry.knowledge.graph_store.upsert_node(
        node_id=NODE_ID,
        node_type=node_type,
        properties=dict(properties or {"name": "Old"}),
    )


def _node(registry: StoreRegistry) -> dict[str, Any]:
    node = registry.knowledge.graph_store.get_node(NODE_ID)
    assert node is not None
    return node


def _rejection_reasons(registry: StoreRegistry) -> list[str]:
    return [
        e.payload["reason"]
        for e in registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_REJECTED
        )
    ]


def _promote(registry: StoreRegistry) -> dict[str, Any]:
    return submit_learning_promotion(
        build_curate_executor(registry, clearance="restricted"),
        entity_payload={
            "entity_type": "precedent",
            "entity_id": NODE_ID,
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


class TestAPlainCreateIsRefused:
    def test_a_new_precedent_is_refused_and_nothing_is_stored(
        self, registry: StoreRegistry
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(_create())

        assert exc_info.value.code == CODE
        assert PRECEDENT_ENTITY_ID_PREFIX in str(exc_info.value)
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_the_governed_path_rejects_it_and_audits_the_code(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(registry, _create())

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CODE
        assert _rejection_reasons(registry) == [CODE]
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_an_if_absent_create_of_an_absent_id_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(registry, _create(if_absent=True))

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CODE
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    def test_an_upsert_over_a_current_precedent_is_refused_and_leaves_it(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry, "precedent", {"name": "Old", "keep": "me"})

        result = _execute(registry, _create(properties={"keep": "gone"}))

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CODE
        node = _node(registry)
        assert node["properties"] == {"name": "Old", "keep": "me"}
        assert len(registry.knowledge.graph_store.get_node_history(NODE_ID)) == 1

    @pytest.mark.parametrize(
        "metadata",
        [
            {PROMOTION_COMMAND_MARK: "true"},
            {PROMOTION_COMMAND_MARK: 1},
            {PROMOTION_COMMAND_MARK: False},
            {"metadata": MARK},
        ],
        ids=["string", "one", "false", "nested"],
    )
    def test_a_mark_that_is_not_exactly_true_in_the_metadata_lifts_nothing(
        self, registry: StoreRegistry, metadata: dict[str, Any]
    ) -> None:
        result = _execute(registry, _create(metadata=metadata))

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CODE
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None

    @pytest.mark.parametrize(
        "args",
        [
            {PROMOTION_COMMAND_MARK: True},
            {"metadata": MARK},
            {"properties": {PROMOTION_COMMAND_MARK: True}},
        ],
        ids=["mark-in-args", "metadata-in-args", "mark-in-properties"],
    )
    def test_a_mark_planted_in_the_args_or_properties_lifts_nothing(
        self, registry: StoreRegistry, args: dict[str, Any]
    ) -> None:
        result = _execute(registry, _create(args=args))

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == CODE
        assert registry.knowledge.graph_store.get_node(NODE_ID) is None


class TestTheNarrowerCodeWins:
    def test_a_stamp_on_a_precedent_keeps_the_excerpt_status_code(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(
            registry,
            _create(properties={SOURCE_EXCERPT_STATUS_KEY: "source_labelled"}),
        )

        assert result.metadata["rejection_reason"] == STATUS_CODE

    def test_another_type_keeps_the_type_code(self, registry: StoreRegistry) -> None:
        result = _execute(registry, _create(node_type="concept"))

        assert result.metadata["rejection_reason"] == TYPE_CODE

    def test_another_type_with_a_stamp_keeps_the_excerpt_status_code(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(
            registry,
            _create(
                node_type="concept",
                properties={SOURCE_EXCERPT_STATUS_KEY: "source_labelled"},
            ),
        )

        assert result.metadata["rejection_reason"] == STATUS_CODE

    def test_a_non_current_precedent_keeps_its_lifecycle_code(
        self, registry: StoreRegistry
    ) -> None:
        _seed(
            registry,
            "precedent",
            {"name": "Old", "lifecycle": {"state": "deprecated", "reason": "x"}},
        )

        result = _execute(registry, _create())

        assert result.metadata["rejection_reason"] == OVER_CODE
        assert _node(registry)["properties"]["lifecycle"]["state"] == "deprecated"

    def test_a_retype_of_an_existing_precedent_keeps_its_retype_code(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry)

        result = _execute(registry, _create(node_type="concept"))

        assert result.metadata["rejection_reason"] == RETYPE_CODE
        assert _node(registry)["node_type"] == "precedent"


class TestAnIfAbsentPlaceholderOverANodeWritesNothing:
    @pytest.mark.parametrize("node_type", ["precedent", "concept"])
    def test_it_is_left_alone_not_refused(
        self, registry: StoreRegistry, node_type: str
    ) -> None:
        _seed(registry, node_type, {"name": "Old", "keep": "me"})

        result = _execute(registry, _create(if_absent=True))

        assert result.status == CommandStatus.SUCCESS
        # The closed outcome field is the contract; the message is for people.
        assert result.metadata[OUTCOME_METADATA_KEY] == OUTCOME_LEFT_ALONE
        assert _rejection_reasons(registry) == []
        node = _node(registry)
        assert node["properties"] == {"name": "Old", "keep": "me"}
        assert len(registry.knowledge.graph_store.get_node_history(NODE_ID)) == 1


class TestThePromotionStillWrites:
    def test_the_marked_command_creates_its_precedent(
        self, registry: StoreRegistry
    ) -> None:
        result = _execute(registry, _create(metadata=MARK))

        assert result.status == CommandStatus.SUCCESS
        assert _node(registry)["node_type"] == "precedent"

    def test_the_real_promotion_writes_and_rewrites_its_precedent(
        self, registry: StoreRegistry
    ) -> None:
        first = _promote(registry)
        assert first["status"] != "entity_failed", first
        assert _node(registry)["node_type"] == "precedent"

        second = _promote(registry)

        assert second["status"] != "entity_failed", second
        assert _rejection_reasons(registry) == []
        node = _node(registry)
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_included"

    def test_a_refused_plain_create_does_not_block_the_promotion_after_it(
        self, registry: StoreRegistry
    ) -> None:
        assert _execute(registry, _create()).status == CommandStatus.REJECTED

        outcome = _promote(registry)

        assert outcome["status"] != "entity_failed", outcome
        assert _node(registry)["node_type"] == "precedent"


class TestEntityUpdateOfAPromotedPrecedentIsNotWidened:
    """The reservation is on create. A precedent is still edited without the mark."""

    def test_a_property_edit_needs_no_mark(self, registry: StoreRegistry) -> None:
        _promote(registry)

        result = _execute(registry, _update(properties={"note": "edited"}))

        assert result.status == CommandStatus.SUCCESS
        node = _node(registry)
        assert node["properties"]["note"] == "edited"
        assert node["properties"][SOURCE_EXCERPT_STATUS_KEY] == "source_included"

    def test_a_document_link_attach_needs_no_mark(
        self, registry: StoreRegistry
    ) -> None:
        _promote(registry)
        registry.knowledge.document_store.put("doc-1", "body", {"title": "d"})

        result = _execute(
            registry,
            _update(properties={"note": "linked"}, document_ids=["doc-1"]),
        )

        assert result.status == CommandStatus.SUCCESS
        assert _node(registry)["document_ids"] == ["doc-1"]

    def test_the_handler_called_directly_edits_it_too(
        self, registry: StoreRegistry
    ) -> None:
        _seed(registry)

        EntityUpdateHandler(registry).handle(_update(properties={"note": "edited"}))

        assert _node(registry)["properties"]["note"] == "edited"


class TestOrdinaryCreatesAreUnaffected:
    @pytest.mark.parametrize(
        "node_id",
        [
            "n1",
            "precedent://other/p1",
            "Precedent://learning/p1",
            "x/precedent://learning/p1",
            "precedent:/learning/p1",
        ],
    )
    @pytest.mark.parametrize("node_type", ["precedent", "concept"])
    def test_any_other_id_keeps_any_type(
        self, registry: StoreRegistry, node_id: str, node_type: str
    ) -> None:
        result = _execute(registry, _create(node_id, node_type))

        assert result.status == CommandStatus.SUCCESS
        stored = registry.knowledge.graph_store.get_node(node_id)
        assert stored is not None
        assert stored["node_type"] == node_type

    def test_a_create_without_an_entity_id_is_unaffected(
        self, registry: StoreRegistry
    ) -> None:
        command = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "precedent", "name": "Pooling"},
        )

        result = _execute(registry, command)

        assert result.status == CommandStatus.SUCCESS
        assert result.created_id is not None
        assert not result.created_id.startswith(PRECEDENT_ENTITY_ID_PREFIX)
