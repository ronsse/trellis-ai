"""Tests for curate command handlers."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.errors import ValidationError
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers import (
    EntityCreateHandler,
    FeedbackRecordHandler,
    LabelAddHandler,
    LabelRemoveHandler,
    LinkCreateHandler,
    PrecedentPromoteHandler,
    TraceIngestHandler,
    create_curate_handlers,
)
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


class TestTraceIngestHandler:
    @staticmethod
    def _trace() -> Trace:
        return Trace(
            source=TraceSource.AGENT,
            intent="diagnose",
            steps=[],
            context=TraceContext(agent_id="agent-1", domain="platform"),
        )

    def test_persists_trace_and_emits_event(self, registry: StoreRegistry) -> None:
        handler = TraceIngestHandler(registry)
        trace = self._trace()
        cmd = Command(
            operation=Operation.TRACE_INGEST,
            args={"trace": trace},
            target_id=trace.trace_id,
            target_type="trace",
        )
        created_id, message = handler.handle(cmd)

        assert created_id == trace.trace_id
        assert trace.trace_id in message
        assert registry.operational.trace_store.get(trace.trace_id) is not None
        events = registry.operational.event_log.get_events(
            event_type=EventType.TRACE_INGESTED
        )
        assert any(ev.entity_id == trace.trace_id for ev in events)

    def test_accepts_dict_payload(self, registry: StoreRegistry) -> None:
        handler = TraceIngestHandler(registry)
        trace = self._trace()
        cmd = Command(
            operation=Operation.TRACE_INGEST,
            args={"trace": trace.model_dump()},
        )
        created_id, _message = handler.handle(cmd)
        assert created_id == trace.trace_id

    def test_idempotent_on_duplicate_trace_id(self, registry: StoreRegistry) -> None:
        """Submitting the same trace twice returns the existing id without
        re-emitting an event — the handler's race-recovery / idempotency path."""
        handler = TraceIngestHandler(registry)
        trace = self._trace()
        cmd = Command(
            operation=Operation.TRACE_INGEST,
            args={"trace": trace},
            target_id=trace.trace_id,
        )
        handler.handle(cmd)
        created_id, message = handler.handle(cmd)
        assert created_id == trace.trace_id
        assert "already" in message.lower()
        events = registry.operational.event_log.get_events(
            event_type=EventType.TRACE_INGESTED,
            entity_id=trace.trace_id,
        )
        assert len(events) == 1


class TestPrecedentPromoteHandler:
    def test_emits_event(self, registry: StoreRegistry) -> None:
        handler = PrecedentPromoteHandler(registry)
        cmd = Command(
            operation=Operation.PRECEDENT_PROMOTE,
            args={"trace_id": "t1", "title": "My Precedent", "description": "Desc"},
            target_id="t1",
        )
        created_id, message = handler.handle(cmd)
        assert created_id is not None
        assert "My Precedent" in message
        # Trace-mined payload is unchanged: entity_type "trace", carries trace_id.
        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED, entity_id="t1"
        )
        assert len(events) == 1
        assert events[0].entity_type == "trace"
        assert events[0].payload["trace_id"] == "t1"

    def test_emits_event_for_entity_sourced_promotion(
        self, registry: StoreRegistry
    ) -> None:
        """Learning-scoring promotions carry no trace_id but still emit a
        PRECEDENT_PROMOTED so get_lessons surfaces them."""
        handler = PrecedentPromoteHandler(registry)
        cmd = Command(
            operation=Operation.PRECEDENT_PROMOTE,
            args={
                "title": "Learning: source_analysis",
                "description": "Reviewed learning for source_analysis.",
                "domain": "billing",
                "entity_type": "precedent",
                "source_item_id": "doc:123",
            },
            target_id="precedent://learning/abc",
            target_type="entity",
        )
        created_id, _message = handler.handle(cmd)
        assert created_id is not None

        events = registry.operational.event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED,
            entity_id="precedent://learning/abc",
        )
        assert len(events) == 1
        event = events[0]
        assert event.entity_type == "precedent"
        assert event.payload["title"] == "Learning: source_analysis"
        assert event.payload["domain"] == "billing"
        assert event.payload["source_item_id"] == "doc:123"
        # No trace for an entity-sourced promotion — trace_id must be absent,
        # not None, so trace-only consumers can branch on presence.
        assert "trace_id" not in event.payload


class TestLabelAddHandler:
    def test_adds_label(self, registry: StoreRegistry) -> None:
        node_id = registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="concept", properties={"name": "test"}
        )
        handler = LabelAddHandler(registry)
        cmd = Command(
            operation=Operation.LABEL_ADD,
            args={"target_id": node_id, "label": "important"},
        )
        result_id, _message = handler.handle(cmd)
        assert result_id == node_id

        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert "important" in node["properties"]["labels"]

    def test_idempotent_label(self, registry: StoreRegistry) -> None:
        node_id = registry.knowledge.graph_store.upsert_node(
            node_id=None,
            node_type="concept",
            properties={"name": "test", "labels": ["existing"]},
        )
        handler = LabelAddHandler(registry)
        cmd = Command(
            operation=Operation.LABEL_ADD,
            args={"target_id": node_id, "label": "existing"},
        )
        handler.handle(cmd)
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["properties"]["labels"].count("existing") == 1

    def test_missing_node(self, registry: StoreRegistry) -> None:
        handler = LabelAddHandler(registry)
        cmd = Command(
            operation=Operation.LABEL_ADD,
            args={"target_id": "nonexistent", "label": "x"},
        )
        result_id, message = handler.handle(cmd)
        assert result_id is None
        assert "not found" in message.lower()


class TestLabelRemoveHandler:
    def test_removes_label(self, registry: StoreRegistry) -> None:
        node_id = registry.knowledge.graph_store.upsert_node(
            node_id=None,
            node_type="concept",
            properties={"name": "test", "labels": ["a", "b"]},
        )
        handler = LabelRemoveHandler(registry)
        cmd = Command(
            operation=Operation.LABEL_REMOVE,
            args={"target_id": node_id, "label": "a"},
        )
        handler.handle(cmd)
        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert "a" not in node["properties"]["labels"]
        assert "b" in node["properties"]["labels"]


class TestFeedbackRecordHandler:
    def test_emits_event(self, registry: StoreRegistry) -> None:
        handler = FeedbackRecordHandler(registry)
        cmd = Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": "t1", "rating": 0.9},
            target_id="t1",
        )
        created_id, message = handler.handle(cmd)
        assert created_id is not None
        assert "0.9" in message

    def _payload(self, registry: StoreRegistry) -> dict:
        (event,) = registry.operational.event_log.get_events(
            event_type=EventType.FEEDBACK_RECORDED, limit=5
        )
        return event.payload

    def test_forwards_pack_id_so_the_event_can_join(
        self, registry: StoreRegistry
    ) -> None:
        """``pack_id`` is the join key and was being dropped here.

        ``POST /feedback`` has always accepted a ``pack_id`` ("Link
        feedback to a context pack") and put it in ``command.args``; this
        handler emitted a payload of three fixed keys, so the link the
        caller asked for was silently discarded and
        ``join_pack_feedback`` — which reads ``payload["pack_id"]`` and
        skips events without it — could never see the event.
        """
        handler = FeedbackRecordHandler(registry)
        cmd = Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": "t1", "rating": 0.9, "pack_id": "pack_1"},
            target_id="t1",
        )
        handler.handle(cmd)

        assert self._payload(registry)["pack_id"] == "pack_1"

    def test_success_is_derived_from_the_rating(self, registry: StoreRegistry) -> None:
        """Without ``success`` the join reads any governed grade as failure.

        ``_join_one`` resolves an absent ``success`` to ``"failure"``, so
        forwarding the join key alone would have wired a *wrong* signal
        into the learning loop — worse than the unjoinable silence it
        replaces.
        """
        handler = FeedbackRecordHandler(registry)
        expected = {0.9: True, 0.5: True, 0.49: False, 0.0: False}
        for rating in expected:
            handler.handle(
                Command(
                    operation=Operation.FEEDBACK_RECORD,
                    args={"target_id": "t1", "rating": rating},
                    target_id="t1",
                )
            )

        recorded = {
            event.payload["rating"]: event.payload["success"]
            for event in registry.operational.event_log.get_events(
                event_type=EventType.FEEDBACK_RECORDED, limit=50
            )
        }
        assert recorded == expected

    def test_blank_pack_id_stays_absent(self, registry: StoreRegistry) -> None:
        """An absent pack is not invented — the key simply is not there."""
        handler = FeedbackRecordHandler(registry)
        cmd = Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": "t1", "rating": 0.9, "pack_id": "   "},
            target_id="t1",
        )
        handler.handle(cmd)

        assert "pack_id" not in self._payload(registry)

    def test_pack_id_omitted_entirely(self, registry: StoreRegistry) -> None:
        handler = FeedbackRecordHandler(registry)
        cmd = Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": "t1", "rating": 0.9},
            target_id="t1",
        )
        handler.handle(cmd)

        assert "pack_id" not in self._payload(registry)


class TestEntityCreateHandler:
    def test_creates_entity(self, registry: StoreRegistry) -> None:
        handler = EntityCreateHandler(registry)
        cmd = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "concept", "name": "Test Entity"},
        )
        node_id, message = handler.handle(cmd)
        assert node_id is not None
        assert "Test Entity" in message

        node = registry.knowledge.graph_store.get_node(node_id)
        assert node is not None
        assert node["node_type"] == "concept"
        assert node["properties"]["name"] == "Test Entity"


class TestLinkCreateHandler:
    def test_creates_link(self, registry: StoreRegistry) -> None:
        id1 = registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="concept", properties={"name": "A"}
        )
        id2 = registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="concept", properties={"name": "B"}
        )
        handler = LinkCreateHandler(registry)
        cmd = Command(
            operation=Operation.LINK_CREATE,
            args={"source_id": id1, "target_id": id2, "edge_kind": "related_to"},
        )
        edge_id, message = handler.handle(cmd)
        assert edge_id is not None
        assert "related_to" in message

    def test_missing_source(self, registry: StoreRegistry) -> None:
        id2 = registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="concept", properties={"name": "B"}
        )
        handler = LinkCreateHandler(registry)
        cmd = Command(
            operation=Operation.LINK_CREATE,
            args={
                "source_id": "nonexistent",
                "target_id": id2,
                "edge_kind": "related_to",
            },
        )
        with pytest.raises(ValidationError, match="source_id="):
            handler.handle(cmd)

    def test_missing_target(self, registry: StoreRegistry) -> None:
        id1 = registry.knowledge.graph_store.upsert_node(
            node_id=None, node_type="concept", properties={"name": "A"}
        )
        handler = LinkCreateHandler(registry)
        cmd = Command(
            operation=Operation.LINK_CREATE,
            args={
                "source_id": id1,
                "target_id": "nonexistent",
                "edge_kind": "related_to",
            },
        )
        with pytest.raises(ValidationError, match="target_id="):
            handler.handle(cmd)

    def test_orphan_endpoint_carries_orphan_edge_code(
        self, registry: StoreRegistry
    ) -> None:
        """Variant A' from adr-extraction-validation.md §5.5: handler-raised
        ValidationError must carry ``code="orphan_edge"`` so the executor's
        MUTATION_REJECTED event has a structured ``reason`` field."""
        handler = LinkCreateHandler(registry)
        cmd = Command(
            operation=Operation.LINK_CREATE,
            args={
                "source_id": "missing_a",
                "target_id": "missing_b",
                "edge_kind": "related_to",
            },
        )
        with pytest.raises(ValidationError) as exc_info:
            handler.handle(cmd)
        assert exc_info.value.code == "orphan_edge"
        # Both endpoints surface in ``errors`` so callers see the full failure.
        assert any("source_id=" in e for e in exc_info.value.errors)
        assert any("target_id=" in e for e in exc_info.value.errors)


def _make_trace(intent: str = "test intent") -> Trace:
    return Trace(
        source=TraceSource.AGENT,
        intent=intent,
        context=TraceContext(agent_id="agent-1", domain="test"),
    )


class TestEntityCreateDocumentLink:
    """``document_ids`` omission semantics must match EntityUpdateHandler.

    ``ENTITY_CREATE`` on an existing id is an upsert (it opens a new SCD-2
    version). Passing the caller's omitted ``document_ids`` straight
    through wrote NULL, silently destroying a link another writer had
    established — e.g. re-extracting an entity ``save_knowledge`` had
    linked to its document.
    """

    @staticmethod
    def _create(registry: StoreRegistry, **extra_args: object) -> None:
        EntityCreateHandler(registry).handle(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={
                    "entity_id": "e1",
                    "entity_type": "Concept",
                    "name": "E",
                    **extra_args,
                },
                requested_by="test",
            )
        )

    def test_omitted_document_ids_carries_stored_link_forward(
        self, registry: StoreRegistry
    ) -> None:
        self._create(registry, document_ids=["doc-1"])
        self._create(registry)
        node = registry.knowledge.graph_store.get_node("e1")
        assert node is not None
        assert node["document_ids"] == ["doc-1"]

    def test_explicit_document_ids_replaces(self, registry: StoreRegistry) -> None:
        self._create(registry, document_ids=["doc-1"])
        self._create(registry, document_ids=["doc-2"])
        node = registry.knowledge.graph_store.get_node("e1")
        assert node is not None
        assert node["document_ids"] == ["doc-2"]

    def test_explicit_empty_list_clears(self, registry: StoreRegistry) -> None:
        """Unlinking stays possible — omission and ``[]`` are different."""
        self._create(registry, document_ids=["doc-1"])
        self._create(registry, document_ids=[])
        node = registry.knowledge.graph_store.get_node("e1")
        assert node is not None
        assert node["document_ids"] == []

    def test_fresh_node_without_link_is_unaffected(
        self, registry: StoreRegistry
    ) -> None:
        self._create(registry)
        node = registry.knowledge.graph_store.get_node("e1")
        assert node is not None
        assert node["document_ids"] == []


class TestCreateCurateHandlers:
    def test_returns_all_handlers(self, registry: StoreRegistry) -> None:
        handlers = create_curate_handlers(registry)
        assert Operation.TRACE_INGEST in handlers
        assert Operation.PRECEDENT_PROMOTE in handlers
        assert Operation.LABEL_ADD in handlers
        assert Operation.LABEL_REMOVE in handlers
        assert Operation.FEEDBACK_RECORD in handlers
        assert Operation.ENTITY_CREATE in handlers
        assert Operation.LINK_CREATE in handlers
        assert Operation.TRACE_INGEST in handlers
