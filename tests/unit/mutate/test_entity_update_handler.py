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
from trellis.schemas.classification import LIFECYCLE_KEY
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


class TestEntityUpdatePrecedentLifecycleRefusal:
    """``entity.update`` refuses a write to ``properties.lifecycle`` on a
    precedent node (item 4 of the demotion-holes follow-up): this used to
    be the only way to undo ``precedent.demote``, silently and with no
    reason recorded. Every other node's ``lifecycle`` key is an ordinary
    property this handler still merges like any other -- the save-surfaces'
    supersession stamp (``mcp.supersession``) depends on that staying true.
    """

    def test_precedent_lifecycle_write_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        """Defective subject: a handler that drops the ``node_type ==
        "precedent"`` guard (or the ``LIFECYCLE_KEY in supplied_properties``
        check) lets this write through -- delete either condition in
        ``EntityUpdateHandler.handle`` and this ``pytest.raises`` finds
        nothing raised, with the node below silently reading back
        ``current`` instead of ``demoted``.
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityUpdateHandler(registry)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.ENTITY_UPDATE,
                    args={
                        "entity_id": node_id,
                        "properties": {LIFECYCLE_KEY: {"state": "current"}},
                    },
                )
            )

        assert exc_info.value.code == "precedent_lifecycle_via_entity_update"
        message = str(exc_info.value)
        assert "precedent.demote" in message
        assert "precedent.reinstate" in message
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    def test_precedent_update_without_lifecycle_key_still_merges(
        self, registry: StoreRegistry
    ) -> None:
        """The refusal is scoped to the ``lifecycle`` key specifically, not
        to every write touching a precedent node."""
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityUpdateHandler(registry)

        handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "properties": {"note": "unrelated"}},
            )
        )

        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["note"] == "unrelated"
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    def test_non_precedent_lifecycle_write_still_merges(
        self, registry: StoreRegistry
    ) -> None:
        """Control case for the mutant proof above: a non-precedent node's
        ``lifecycle`` key is an ordinary property this handler still
        merges -- the supersession stamp (``mcp.supersession``) writes
        ``lifecycle`` through this exact path for non-precedent nodes and
        must keep working.

        Defective subject: a handler that refuses *any* ``lifecycle``
        write regardless of ``node_type`` (over-broad fix) would raise
        here too -- widen the ``node_type == "precedent"`` check to
        unconditional and this ``handle`` call raises instead of merging.
        """
        node_id = _create_node(
            registry,
            node_type="document",
            properties={"name": "Some note"},
        )
        handler = EntityUpdateHandler(registry)

        returned_id, _ = handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={
                    "entity_id": node_id,
                    "properties": {LIFECYCLE_KEY: {"state": "superseded"}},
                },
            )
        )

        assert returned_id == node_id
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "superseded"

    def test_precedent_lifecycle_write_refused_through_executor(
        self, registry: StoreRegistry
    ) -> None:
        """The CLI/REST/MCP surfaces must see a REJECTED command, not a
        store failure, and the refusal must name both reversal verbs."""
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        executor = build_curate_executor(registry)

        result = executor.execute(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={
                    "entity_id": node_id,
                    "properties": {LIFECYCLE_KEY: {"state": "current"}},
                },
            )
        )

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "precedent_lifecycle_via_entity_update"
        )
        assert "precedent.reinstate" in result.message


class TestEntityUpdatePropertiesPairsFormNormalised:
    """``properties`` may arrive as a dict OR as an iterable of ``(key,
    value)`` pairs -- the merge below, ``props.update(supplied_properties)``,
    accepts both, and MCP ``execute_mutation`` / REST ``/commands/batch``
    both carry ``args`` as loosely-typed JSON, so a caller can and does
    send the pairs form (gate-876b should-fix). The precedent lifecycle
    guard used to test ``isinstance(supplied_properties, dict)`` before
    checking for ``LIFECYCLE_KEY``, so a pairs-form payload --
    ``properties=[["lifecycle", {...}]]`` -- skipped the check entirely
    and reinstated a demoted precedent with no ``PRECEDENT_REINSTATED``
    event and no reason recorded. ``EntityUpdateHandler.handle`` now
    normalises with ``dict(raw_properties)`` before either guard reads it,
    so both shapes see the same key.
    """

    def test_pairs_form_lifecycle_write_on_precedent_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        """Defective subject: reverting the normalisation to
        ``isinstance(supplied_properties, dict) and LIFECYCLE_KEY in
        supplied_properties`` (reading the un-normalised value) makes
        this ``pytest.raises`` find nothing raised, with the node below
        silently reading back ``current`` instead of ``demoted``.
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityUpdateHandler(registry)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.ENTITY_UPDATE,
                    args={
                        "entity_id": node_id,
                        "properties": [[LIFECYCLE_KEY, {"state": "current"}]],
                    },
                )
            )

        assert exc_info.value.code == "precedent_lifecycle_via_entity_update"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_REINSTATED
        )
        assert len(events) == 0

    def test_dict_form_lifecycle_write_on_precedent_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        """Control case: the dict form was never the hole -- pinned here
        too so a fix that normalises only the pairs form (and breaks the
        dict form in the process) is caught the same way."""
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityUpdateHandler(registry)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.ENTITY_UPDATE,
                    args={
                        "entity_id": node_id,
                        "properties": {LIFECYCLE_KEY: {"state": "current"}},
                    },
                )
            )

        assert exc_info.value.code == "precedent_lifecycle_via_entity_update"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    def test_pairs_form_non_lifecycle_property_still_merges(
        self, registry: StoreRegistry
    ) -> None:
        """The normalisation must not break legitimate pairs-form usage
        for a key other than lifecycle -- a caller sending
        ``properties=[["description", "..."]]`` through MCP
        ``execute_mutation`` must keep working exactly as the dict form
        does."""
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityUpdateHandler(registry)

        handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={
                    "entity_id": node_id,
                    "properties": [["description", "via pairs"]],
                },
            )
        )

        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["description"] == "via pairs"
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    def test_pairs_form_lifecycle_write_refused_through_executor(
        self, registry: StoreRegistry
    ) -> None:
        """The CLI/REST/MCP surfaces must see a REJECTED command for the
        pairs form too, not a silent reinstate -- this is the exact shape
        MCP ``execute_mutation`` and REST ``/commands/batch`` carry
        ``args`` in."""
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        executor = build_curate_executor(registry)

        result = executor.execute(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={
                    "entity_id": node_id,
                    "properties": [[LIFECYCLE_KEY, {"state": "current"}]],
                },
            )
        )

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "precedent_lifecycle_via_entity_update"
        )


class TestEntityUpdatePrecedentTypeChangeRefusal:
    """Closes the two-step bypass (item F1, demotion-holes follow-up):
    update 1 moves a precedent's ``entity_type`` away from ``"precedent"``
    with no ``lifecycle`` payload (the lifecycle guard above never fires,
    since nothing names ``lifecycle``); update 2 moves it back to
    ``"precedent"`` while writing ``properties.lifecycle.state="current"``
    in the same call -- read against the *now non-precedent* stored node,
    the old guard's ``existing_node["node_type"] == "precedent"`` check
    was false, so the write went through with no ``PRECEDENT_REINSTATED``
    event and no reason recorded. Two independent guards close it: a
    precedent's ``node_type`` may not be changed away at all, and the
    lifecycle guard is also keyed on a *supplied* ``entity_type ==
    "precedent"``, not only the currently-stored one.
    """

    def test_node_type_change_away_from_precedent_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        """Defective subject: delete the node_type-away guard and this
        ``pytest.raises`` finds nothing raised, with the node below
        silently reading back ``node_type == "concept"``.
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityUpdateHandler(registry)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.ENTITY_UPDATE,
                    args={"entity_id": node_id, "entity_type": "concept"},
                )
            )

        assert exc_info.value.code == "precedent_entity_type_change_refused"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["node_type"] == "precedent"
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    def test_two_step_bypass_is_closed_at_step_one(
        self, registry: StoreRegistry
    ) -> None:
        """The exact bypass sequence from the review (gate-876): step 1
        types the precedent away with no lifecycle write -- this alone
        would have succeeded before this fix, since the lifecycle guard
        only fires on a lifecycle write. Step 2 (type it back while
        writing ``lifecycle.state="current"`` in the same call) never
        becomes reachable because step 1 is refused outright, so this
        test exercises exactly the first half of the sequence and
        confirms the node is untouched -- still ``precedent``, still
        ``demoted`` -- after the refusal.
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityUpdateHandler(registry)

        with pytest.raises(ValidationError):
            handler.handle(
                Command(
                    operation=Operation.ENTITY_UPDATE,
                    args={"entity_id": node_id, "entity_type": "concept"},
                )
            )

        # Step 2 cannot be submitted against a node that never actually
        # changed type -- confirm the state step 1 would have needed to
        # reach is simply absent.
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["node_type"] == "precedent"
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_REINSTATED
        )
        assert len(events) == 0

    def test_supplied_entity_type_precedent_also_guards_the_lifecycle_write(
        self, registry: StoreRegistry
    ) -> None:
        """Pins mutant M3 (gate-876): a single ``entity.update`` call that
        supplies BOTH ``entity_type: "precedent"`` and a ``lifecycle``
        property is refused via the ``supplied_entity_type ==
        "precedent"`` half of the guard's ``or`` -- not only via the
        stored-type half exercised by
        ``TestEntityUpdatePrecedentLifecycleRefusal``.

        Defective subject: narrow the guard's condition from ``or`` to
        ``and`` (``is_existing_precedent and supplied_entity_type ==
        "precedent"``) and this still passes, since the stored type here
        already is "precedent" -- that mutant is caught instead by
        ``test_node_type_change_away_from_precedent_is_refused`` reaching
        a node whose stored type is NOT precedent while ``entity_type``
        is omitted, where ``and`` would require a redundant
        ``entity_type="precedent"`` that test never supplies, making the
        ``and``-guard raise nothing. Together the two tests pin both
        halves of the ``or`` independently.
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityUpdateHandler(registry)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.ENTITY_UPDATE,
                    args={
                        "entity_id": node_id,
                        "entity_type": "precedent",
                        "properties": {LIFECYCLE_KEY: {"state": "current"}},
                    },
                )
            )

        assert exc_info.value.code == "precedent_lifecycle_via_entity_update"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"

    def test_entity_type_change_on_a_non_precedent_is_unaffected(
        self, registry: StoreRegistry
    ) -> None:
        """Control case: the node_type-away guard is scoped to precedents
        only -- changing a concept's entity_type must still work."""
        node_id = _create_node(
            registry, node_type="concept", properties={"name": "Widget"}
        )
        handler = EntityUpdateHandler(registry)

        returned_id, _ = handler.handle(
            Command(
                operation=Operation.ENTITY_UPDATE,
                args={"entity_id": node_id, "entity_type": "document"},
            )
        )

        assert returned_id == node_id
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["node_type"] == "document"


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


class TestEntityCreateOverExistingPrecedentRefusal:
    """``entity.create`` refuses an ``entity_id`` that already names a
    precedent (item F1, demotion-holes follow-up): the handler's property
    replace is unconditional, so re-creating over an existing precedent
    id dropped its ``lifecycle`` -- a demoted precedent came back
    "current" with no reason recorded and no ``PRECEDENT_REINSTATED``
    event, the same silent-reinstate shape the ``entity.update`` guards
    close on that verb.
    """

    def test_create_over_existing_precedent_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        """Defective subject: delete the precedent guard in
        ``EntityCreateHandler.handle`` and this ``pytest.raises`` finds
        nothing raised, with the node below silently reading back
        ``lifecycle`` absent (equivalent to "current").
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        handler = EntityCreateHandler(registry)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.ENTITY_CREATE,
                    args={
                        "entity_id": node_id,
                        "entity_type": "precedent",
                        "name": "Pooling pattern",
                    },
                )
            )

        assert exc_info.value.code == "entity_create_over_existing_precedent"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == "demoted"
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_REINSTATED
        )
        assert len(events) == 0

    def test_create_over_existing_precedent_is_refused_through_executor(
        self, registry: StoreRegistry
    ) -> None:
        """The CLI/REST/MCP surfaces must see a REJECTED command, not a
        store failure that would otherwise read as a crash."""
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={
                "name": "Pooling pattern",
                LIFECYCLE_KEY: {"state": "demoted"},
            },
        )
        executor = build_curate_executor(registry)

        result = executor.execute(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": node_id,
                    "entity_type": "precedent",
                    "name": "Pooling pattern",
                },
            )
        )

        assert result.status == CommandStatus.REJECTED
        assert (
            result.metadata["rejection_reason"]
            == "entity_create_over_existing_precedent"
        )

    @pytest.mark.parametrize(
        "record",
        [
            {"state": ""},
            {"state": None},
            {"state": 5},
            {},
            "demoted",
            ["demoted"],
        ],
        ids=[
            "empty_state",
            "null_state",
            "non_str_state",
            "no_state_key",
            "non_dict_record",
            "list_record",
        ],
    )
    def test_create_over_malformed_lifecycle_record_reads_as_current(
        self, registry: StoreRegistry, record: Any
    ) -> None:
        """A malformed record is no more informative than an absent one, so
        re-creating over it must not be refused. ``""`` is the case the
        guard once read as a real, non-current state.

        Defective subject: return ``state`` unconditionally from
        ``_current_lifecycle_state`` and the ``""`` row raises
        ``entity_create_over_existing_precedent``.
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={"name": "Pooling pattern", LIFECYCLE_KEY: record},
        )

        EntityCreateHandler(registry).handle(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": node_id,
                    "entity_type": "precedent",
                    "name": "Pooling pattern",
                },
            )
        )

        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert LIFECYCLE_KEY not in node["properties"]

    @pytest.mark.parametrize("state", ["demoted", "archived", "superseded"])
    def test_create_over_a_real_non_current_state_is_still_refused(
        self, registry: StoreRegistry, state: str
    ) -> None:
        """The empty-state fix must not fail open on a state that means
        something: every real non-current state keeps its refusal."""
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={"name": "Pooling pattern", LIFECYCLE_KEY: {"state": state}},
        )

        with pytest.raises(ValidationError) as exc_info:
            EntityCreateHandler(registry).handle(
                Command(
                    operation=Operation.ENTITY_CREATE,
                    args={
                        "entity_id": node_id,
                        "entity_type": "precedent",
                        "name": "Pooling pattern",
                    },
                )
            )

        assert exc_info.value.code == "entity_create_over_existing_precedent"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"][LIFECYCLE_KEY]["state"] == state

    def test_create_over_existing_non_precedent_is_still_an_upsert(
        self, registry: StoreRegistry
    ) -> None:
        """Control case: the refusal is scoped to precedents only --
        re-creating over an existing concept id must still work as the
        ordinary upsert ``EntityCreateHandler``'s docstring describes
        (e.g. re-extracting an entity)."""
        node_id = _create_node(
            registry, node_type="concept", properties={"name": "Original"}
        )
        handler = EntityCreateHandler(registry)

        returned_id, _ = handler.handle(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": node_id,
                    "entity_type": "concept",
                    "name": "Replaced",
                },
            )
        )

        assert returned_id == node_id
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["name"] == "Replaced"

    def test_create_over_existing_precedent_preserves_document_ids_resolution(
        self, registry: StoreRegistry
    ) -> None:
        """Control case for the refactor that threads ``existing_node``
        into ``_resolve_document_ids`` instead of re-reading: a
        non-precedent node's stored ``document_ids`` must still carry
        forward on an omitted field."""
        node_id = _create_node(
            registry,
            node_type="concept",
            properties={"name": "Original"},
            document_ids=["doc-7"],
        )
        handler = EntityCreateHandler(registry)

        returned_id, _ = handler.handle(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": node_id,
                    "entity_type": "concept",
                    "name": "Replaced",
                },
            )
        )

        assert returned_id == node_id
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["document_ids"] == ["doc-7"]

    def test_create_over_current_precedent_with_matching_type_succeeds(
        self, registry: StoreRegistry
    ) -> None:
        """A *current* precedent's id is not refused outright (gate-876b
        R1): re-approving a learning candidate whose target entity is
        already current is a legitimate ``entity.create`` --
        ``submit_learning_promotion`` writes it this way, and
        ``_precedent_reapproval_refusal`` already lets a current
        precedent's re-approval through before this handler ever runs.
        The refusal is scoped to a *non-current* lifecycle state, not to
        "the id already names a precedent".

        Defective subject: a guard that refuses ``entity.create`` over
        *any* existing precedent regardless of lifecycle state (the
        pre-fix shape that broke every re-approval main allows) raises
        here, where this call must succeed and apply the change instead.
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={"name": "Pooling pattern", "description": "old"},
        )
        handler = EntityCreateHandler(registry)

        returned_id, _ = handler.handle(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": node_id,
                    "entity_type": "precedent",
                    "name": "Pooling pattern",
                    "properties": {"description": "re-scored"},
                },
            )
        )

        assert returned_id == node_id
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["description"] == "re-scored"

    def test_create_over_current_precedent_with_different_entity_type_is_refused(
        self, registry: StoreRegistry
    ) -> None:
        """A current precedent's lifecycle state alone does not clear
        ``entity.create`` to proceed: retyping it away in the same call
        (``entity_type="concept"``) must still be refused, mirroring
        ``EntityUpdateHandler``'s own retype guard -- a later
        ``entity.update`` could then write ``properties.lifecycle`` under
        the now-non-precedent ``node_type`` and slip past the guard that
        refuses it on a precedent, the same end state the two-step
        bypass closes on the update path.

        Defective subject: a guard narrowed to check only lifecycle state
        (an incomplete R1 fix that drops the ``entity_type`` check
        entirely) finds nothing raised here, with the node below
        silently retyped to ``concept``.
        """
        node_id = _create_node(
            registry,
            node_type="precedent",
            properties={"name": "Pooling pattern"},
        )
        handler = EntityCreateHandler(registry)

        with pytest.raises(ValidationError) as exc_info:
            handler.handle(
                Command(
                    operation=Operation.ENTITY_CREATE,
                    args={
                        "entity_id": node_id,
                        "entity_type": "concept",
                        "name": "Pooling pattern",
                    },
                )
            )

        assert exc_info.value.code == "entity_create_retypes_existing_precedent"
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["node_type"] == "precedent"
        # The refusal is raised before any store write, so no new SCD-2
        # version was opened -- still the single version ``_create_node``
        # wrote.
        assert len(registry.knowledge.graph_store.get_node_history(node_id)) == 1


class TestLifecycleStateReadersAgree:
    """The handler's reader and the scorer's reader answer the same question
    (is this precedent current?) for the promote preview and the real write,
    so they must give one answer for every shape of record, malformed or not.
    """

    @pytest.mark.parametrize(
        "properties",
        [
            {},
            {LIFECYCLE_KEY: None},
            {LIFECYCLE_KEY: "demoted"},
            {LIFECYCLE_KEY: {}},
            {LIFECYCLE_KEY: {"state": ""}},
            {LIFECYCLE_KEY: {"state": None}},
            {LIFECYCLE_KEY: {"state": 5}},
            {LIFECYCLE_KEY: {"state": "current"}},
            {LIFECYCLE_KEY: {"state": "demoted"}},
            {LIFECYCLE_KEY: {"state": "archived"}},
            {LIFECYCLE_KEY: {"state": "superseded"}},
        ],
    )
    def test_both_readers_give_one_answer(self, properties: dict[str, Any]) -> None:
        from trellis.learning.scoring import _precedent_lifecycle_state
        from trellis.mutate.handlers import _current_lifecycle_state

        node = {"node_type": "precedent", "properties": properties}

        assert _current_lifecycle_state(node) == _precedent_lifecycle_state(node)

    def test_an_empty_state_reads_as_current(self) -> None:
        from trellis.mutate.handlers import _current_lifecycle_state

        node = {"properties": {LIFECYCLE_KEY: {"state": ""}}}

        assert _current_lifecycle_state(node) == "current"
