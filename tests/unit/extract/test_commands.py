"""Tests for result_to_batch — drafts → CommandBatch conversion."""

from __future__ import annotations

from unittest.mock import MagicMock

from trellis.extract.commands import (
    CONFIDENCE_PROPERTY,
    batch_draft_counts,
    reconcile_node_roles,
    result_to_batch,
)
from trellis.mutate.commands import BatchStrategy, CommandBatch, Operation
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)


def _result(
    *,
    entities: list[EntityDraft] | None = None,
    edges: list[EdgeDraft] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        entities=entities or [],
        edges=edges or [],
        extractor_used="test",
        tier="deterministic",
        provenance=ExtractionProvenance(
            extractor_name="test",
            extractor_version="0.0.0",
            source_hint=None,
        ),
    )


class TestEntityConversion:
    def test_basic_entity(self) -> None:
        ent = EntityDraft(
            entity_id="ent-a",
            entity_type="person",
            name="Alice",
            properties={"team": "platform"},
            node_role=NodeRole.SEMANTIC,
        )
        batch = result_to_batch(_result(entities=[ent]), requested_by="test")
        assert len(batch.commands) == 1
        cmd = batch.commands[0]
        assert cmd.operation == Operation.ENTITY_CREATE
        assert cmd.args["entity_id"] == "ent-a"
        assert cmd.args["entity_type"] == "person"
        assert cmd.args["name"] == "Alice"
        assert cmd.args["properties"] == {
            "team": "platform",
            CONFIDENCE_PROPERTY: 1.0,
        }
        assert cmd.args["node_role"] == "semantic"
        assert cmd.target_type == "entity"
        assert cmd.requested_by == "test"

    def test_entity_without_id_skips_entity_id_arg(self) -> None:
        """LLM extractor emits entities with entity_id=None; handler assigns."""
        ent = EntityDraft(
            entity_id=None,
            entity_type="person",
            name="Bob",
        )
        batch = result_to_batch(_result(entities=[ent]), requested_by="t")
        cmd = batch.commands[0]
        assert "entity_id" not in cmd.args
        assert cmd.args["name"] == "Bob"


class TestEdgeConversion:
    def test_basic_edge(self) -> None:
        edge = EdgeDraft(
            source_id="ent-a",
            target_id="ent-b",
            edge_kind="mentions",
            properties={"confidence_note": "llm"},
        )
        batch = result_to_batch(_result(edges=[edge]), requested_by="t")
        assert len(batch.commands) == 1
        cmd = batch.commands[0]
        assert cmd.operation == Operation.LINK_CREATE
        assert cmd.args["source_id"] == "ent-a"
        assert cmd.args["target_id"] == "ent-b"
        assert cmd.args["edge_kind"] == "mentions"
        assert cmd.args["properties"] == {
            "confidence_note": "llm",
            CONFIDENCE_PROPERTY: 1.0,
        }
        assert cmd.target_id == "ent-a"  # router key
        assert cmd.target_type == "entity"
        # Default: flag is absent — strict FK pre-flight applies.
        assert "allow_dangling" not in cmd.args

    def test_allow_dangling_edge_forwards_flag(self) -> None:
        """Drafts opting out of FK validation propagate the flag to args."""
        edge = EdgeDraft(
            source_id="ent-a",
            target_id="ent-b",
            edge_kind="depends_on",
            allow_dangling=True,
        )
        batch = result_to_batch(_result(edges=[edge]), requested_by="t")
        cmd = batch.commands[0]
        assert cmd.args["allow_dangling"] is True

    def test_strict_edge_omits_flag(self) -> None:
        """Strict (default) drafts must not leak ``allow_dangling`` into args."""
        edge = EdgeDraft(
            source_id="ent-a",
            target_id="ent-b",
            edge_kind="mentions",
            allow_dangling=False,
        )
        batch = result_to_batch(_result(edges=[edge]), requested_by="t")
        cmd = batch.commands[0]
        assert "allow_dangling" not in cmd.args


class TestBatchShape:
    def test_entities_precede_edges(self) -> None:
        """Order matters — entities must be created before edges reference them."""
        ent = EntityDraft(entity_id="a", entity_type="p", name="A")
        edge = EdgeDraft(source_id="a", target_id="b", edge_kind="e")
        batch = result_to_batch(_result(entities=[ent], edges=[edge]), requested_by="t")
        ops = [c.operation for c in batch.commands]
        assert ops == [Operation.ENTITY_CREATE, Operation.LINK_CREATE]

    def test_default_strategy_is_continue_on_error(self) -> None:
        batch = result_to_batch(_result(), requested_by="t")
        assert batch.strategy == BatchStrategy.CONTINUE_ON_ERROR

    def test_strategy_override(self) -> None:
        batch = result_to_batch(
            _result(), requested_by="t", strategy=BatchStrategy.STOP_ON_ERROR
        )
        assert batch.strategy == BatchStrategy.STOP_ON_ERROR

    def test_empty_result_empty_batch(self) -> None:
        batch = result_to_batch(_result(), requested_by="t")
        assert batch.commands == []
        assert batch.requested_by == "t"

    def test_requested_by_propagated(self) -> None:
        ent = EntityDraft(entity_id="a", entity_type="p", name="A")
        batch = result_to_batch(
            _result(entities=[ent]), requested_by="save_memory_extractor"
        )
        assert batch.requested_by == "save_memory_extractor"
        assert batch.commands[0].requested_by == "save_memory_extractor"


class TestConfidencePersistence:
    """Draft confidence must reach the store, not die in the bridge."""

    def test_entity_confidence_lands_in_properties(self) -> None:
        ent = EntityDraft(entity_id="a", entity_type="p", name="A", confidence=0.42)
        batch = result_to_batch(_result(entities=[ent]), requested_by="t")
        assert batch.commands[0].args["properties"][CONFIDENCE_PROPERTY] == 0.42

    def test_edge_confidence_lands_in_properties(self) -> None:
        edge = EdgeDraft(source_id="a", target_id="b", edge_kind="e", confidence=0.33)
        batch = result_to_batch(_result(edges=[edge]), requested_by="t")
        assert batch.commands[0].args["properties"][CONFIDENCE_PROPERTY] == 0.33

    def test_default_confidence_is_still_recorded(self) -> None:
        """Absent == 1.0; record it so the field is always queryable."""
        ent = EntityDraft(entity_id="a", entity_type="p", name="A")
        batch = result_to_batch(_result(entities=[ent]), requested_by="t")
        assert batch.commands[0].args["properties"][CONFIDENCE_PROPERTY] == 1.0

    def test_draft_properties_are_not_mutated(self) -> None:
        """The bridge copies — a draft handed to two batches stays clean."""
        ent = EntityDraft(
            entity_id="a", entity_type="p", name="A", properties={"team": "platform"}
        )
        result_to_batch(_result(entities=[ent]), requested_by="t")
        assert ent.properties == {"team": "platform"}


class TestConfidenceGate:
    """``min_confidence`` is opt-in and drops nothing until asked."""

    def test_gate_off_by_default_keeps_low_confidence_drafts(self) -> None:
        ent = EntityDraft(entity_id="a", entity_type="p", name="A", confidence=0.1)
        edge = EdgeDraft(source_id="a", target_id="b", edge_kind="e", confidence=0.1)
        batch = result_to_batch(_result(entities=[ent], edges=[edge]), requested_by="t")
        assert len(batch.commands) == 2

    def test_gate_drops_sub_threshold_entity(self) -> None:
        keep = EntityDraft(entity_id="a", entity_type="p", name="A", confidence=0.9)
        drop = EntityDraft(entity_id="b", entity_type="p", name="B", confidence=0.2)
        batch = result_to_batch(
            _result(entities=[keep, drop]), requested_by="t", min_confidence=0.5
        )
        assert [c.args["entity_id"] for c in batch.commands] == ["a"]

    def test_gate_drops_sub_threshold_edge(self) -> None:
        keep = EdgeDraft(source_id="a", target_id="b", edge_kind="e", confidence=0.8)
        drop = EdgeDraft(source_id="a", target_id="c", edge_kind="e", confidence=0.4)
        batch = result_to_batch(
            _result(edges=[keep, drop]), requested_by="t", min_confidence=0.5
        )
        assert [c.args["target_id"] for c in batch.commands] == ["b"]

    def test_gate_drops_edges_orphaned_by_a_dropped_entity(self) -> None:
        """A confident edge onto a dropped node is not a survivor."""
        dropped = EntityDraft(entity_id="b", entity_type="p", name="B", confidence=0.1)
        edge = EdgeDraft(source_id="a", target_id="b", edge_kind="e", confidence=1.0)
        batch = result_to_batch(
            _result(entities=[dropped], edges=[edge]),
            requested_by="t",
            min_confidence=0.5,
        )
        assert batch.commands == []

    def test_threshold_is_inclusive(self) -> None:
        ent = EntityDraft(entity_id="a", entity_type="p", name="A", confidence=0.5)
        batch = result_to_batch(
            _result(entities=[ent]), requested_by="t", min_confidence=0.5
        )
        assert len(batch.commands) == 1


class TestDocumentIds:
    """``document_ids`` is the graph↔document link the handler already takes."""

    def test_document_ids_forwarded_when_set(self) -> None:
        ent = EntityDraft(
            entity_id="a", entity_type="p", name="A", document_ids=["doc-1", "doc-2"]
        )
        batch = result_to_batch(_result(entities=[ent]), requested_by="t")
        assert batch.commands[0].args["document_ids"] == ["doc-1", "doc-2"]

    def test_document_ids_absent_when_none(self) -> None:
        """Omission, not an empty list — that's "leave the link alone"."""
        ent = EntityDraft(entity_id="a", entity_type="p", name="A")
        batch = result_to_batch(_result(entities=[ent]), requested_by="t")
        assert "document_ids" not in batch.commands[0].args

    def test_document_ids_are_copied_not_shared(self) -> None:
        ids = ["doc-1"]
        ent = EntityDraft(entity_id="a", entity_type="p", name="A", document_ids=ids)
        batch = result_to_batch(_result(entities=[ent]), requested_by="t")
        forwarded = batch.commands[0].args["document_ids"]
        assert forwarded == ids
        assert forwarded is not ids


class TestBatchDraftCounts:
    def test_none_batch_is_zero(self) -> None:
        assert batch_draft_counts(None) == (0, 0)

    def test_counts_commands_by_operation(self) -> None:
        batch = result_to_batch(
            _result(
                entities=[EntityDraft(entity_id="a", entity_type="p", name="A")],
                edges=[
                    EdgeDraft(source_id="a", target_id="b", edge_kind="relatesTo"),
                    EdgeDraft(source_id="a", target_id="c", edge_kind="relatesTo"),
                ],
            ),
            requested_by="t",
        )
        assert batch_draft_counts(batch) == (1, 2)


class TestReconcileNodeRoles:
    """Guards the branches the end-to-end store test can't reach cheaply."""

    @staticmethod
    def _batch_with(entity: EntityDraft) -> CommandBatch:
        return result_to_batch(_result(entities=[entity]), requested_by="t")

    def test_auto_generated_id_is_skipped(self) -> None:
        """No entity_id means no node to collide with — never read the store."""
        store = MagicMock()
        batch = self._batch_with(EntityDraft(entity_type="p", name="A"))
        assert reconcile_node_roles(batch, store) == []
        store.get_node.assert_not_called()

    def test_absent_node_is_skipped(self) -> None:
        store = MagicMock()
        store.get_node.return_value = None
        batch = self._batch_with(EntityDraft(entity_id="a", entity_type="p", name="A"))
        assert reconcile_node_roles(batch, store) == []
        assert batch.commands[0].args["node_role"] == "semantic"

    def test_matching_role_is_left_alone(self) -> None:
        store = MagicMock()
        store.get_node.return_value = {"node_role": "semantic"}
        batch = self._batch_with(EntityDraft(entity_id="a", entity_type="p", name="A"))
        assert reconcile_node_roles(batch, store) == []
        assert batch.commands[0].args["node_role"] == "semantic"

    def test_conflicting_role_is_rewritten_to_the_stored_one(self) -> None:
        store = MagicMock()
        store.get_node.return_value = {"node_role": "semantic"}
        batch = self._batch_with(
            EntityDraft(
                entity_id="a",
                entity_type="p",
                name="A",
                node_role=NodeRole.STRUCTURAL,
            )
        )
        assert reconcile_node_roles(batch, store) == ["a"]
        assert batch.commands[0].args["node_role"] == "semantic"

    def test_edges_are_ignored(self) -> None:
        store = MagicMock()
        batch = result_to_batch(
            _result(
                edges=[EdgeDraft(source_id="a", target_id="b", edge_kind="relatesTo")]
            ),
            requested_by="t",
        )
        assert reconcile_node_roles(batch, store) == []
        store.get_node.assert_not_called()
