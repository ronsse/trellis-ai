"""Tests for result_to_batch — drafts → CommandBatch conversion."""

from __future__ import annotations

from trellis.extract.commands import result_to_batch
from trellis.mutate.commands import BatchStrategy, Operation
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
        assert cmd.args["properties"] == {"team": "platform"}
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
        assert cmd.args["properties"] == {"confidence_note": "llm"}
        assert cmd.target_id == "ent-a"  # router key
        assert cmd.target_type == "entity"


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
