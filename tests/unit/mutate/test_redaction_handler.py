"""Tests for RedactionApplyHandler.

Covers the ``redaction.apply`` gap: the verb shipped in the Operation enum
with no registered handler (the same gap ``entity.update`` had before
``EntityUpdateHandler``, issue #260), so defect-minted entities could only
be *neutralized* via ``entity.update``, never removed. These tests pin the
purge semantics (all SCD-2 versions, edge cascade, alias + vector cleanup),
the content-free ``REDACTION_APPLIED`` audit payload, the no-cascade rules
(documents, observations, measurements — pointers instead), the audit
preconditions (non-empty bounded reason, persisting event log), and the
failure paths including the concurrent-purge race and a failing emit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.errors import NotFoundError, StoreError, ValidationError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.handlers import (
    MAX_REDACTION_REASON_CHARS,
    RedactionApplyHandler,
    create_curate_handlers,
)
from trellis.schemas.well_known import MEASUREMENT, OBSERVATION
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
        node_type=kwargs.get("node_type", "person"),
        properties=kwargs.get("properties", {"name": "Defect Mint"}),
        document_ids=kwargs.get("document_ids"),
    )


def _redact(target_id: str, reason: str = "defect-minted entity (#299)") -> Command:
    return Command(
        operation=Operation.REDACTION_APPLY,
        args={"target_id": target_id, "reason": reason},
    )


class TestRedactionApplyHandler:
    def test_registered_in_curate_handlers(self, registry: StoreRegistry) -> None:
        handlers = create_curate_handlers(registry)
        assert Operation.REDACTION_APPLY in handlers
        assert isinstance(handlers[Operation.REDACTION_APPLY], RedactionApplyHandler)

    def test_purges_node_edges_aliases_and_vector(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        node_id = _create_node(registry)
        # Second SCD-2 version — the purge must take history with it.
        graph.upsert_node(node_id, "person", {"name": "Defect Mint", "v": 2})
        _create_node(registry, node_id="bystander", properties={"name": "Keep"})
        graph.upsert_edge(node_id, "bystander", "mentions")
        graph.upsert_alias(node_id, "github", "defect-raw")
        registry.knowledge.vector_store.upsert(node_id, [0.1, 0.2, 0.3])

        returned_id, message = RedactionApplyHandler(registry).handle(_redact(node_id))

        assert returned_id == node_id
        assert node_id in message
        assert graph.get_node(node_id) is None
        assert graph.get_node_history(node_id) == []
        assert graph.get_edges("bystander", direction="both") == []
        assert graph.get_aliases(node_id) == []
        assert registry.knowledge.vector_store.get(node_id) is None
        # The bystander node itself survives the cascade.
        assert graph.get_node("bystander") is not None

        events = registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["node_versions_purged"] == 2
        assert payload["edges_purged"] == 1
        assert payload["aliases_purged"] == 1
        assert payload["vector_deleted"] is True

    def test_emits_content_free_audit_event(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry, document_ids=["doc-1"])

        cmd = _redact(node_id, reason="defect-minted Person (#299)")
        RedactionApplyHandler(registry).handle(cmd)

        events = registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )
        assert len(events) == 1
        event = events[0]
        assert event.entity_id == node_id
        # The entity type rides the event column, not the payload.
        assert event.entity_type == "person"
        payload = event.payload
        assert payload["target_id"] == node_id
        assert payload["target_kind"] == "entity"
        assert payload["reason"] == "defect-minted Person (#299)"
        # Joins the semantic event to the executor's MUTATION_EXECUTED audit
        # event even when the surface left Command.target_id unset (MCP does).
        assert payload["command_id"] == cmd.command_id
        assert payload["requested_by"] == cmd.requested_by
        assert payload["vector_deleted"] is False
        # Pointer, not prose: linked ids ride the payload for follow-up.
        assert payload["document_ids"] == ["doc-1"]
        assert payload["linked_observation_ids"] == []
        assert payload["linked_measurement_ids"] == []
        # The audit trail must never re-contain the purged content.
        assert "name" not in payload
        assert "properties" not in payload
        assert "entity_type" not in payload

    def test_document_ids_union_across_versions(self, registry: StoreRegistry) -> None:
        graph = registry.knowledge.graph_store
        node_id = _create_node(registry, document_ids=["doc-1"])
        # A later version REPLACES the link; the purge removes both version
        # rows, so the payload must carry the union, not just the current.
        graph.upsert_node(
            node_id, "person", {"name": "Defect Mint"}, document_ids=["doc-2"]
        )

        RedactionApplyHandler(registry).handle(_redact(node_id))

        events = registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )
        assert events[0].payload["document_ids"] == ["doc-1", "doc-2"]

    def test_linked_document_not_cascaded(self, registry: StoreRegistry) -> None:
        doc_id = registry.knowledge.document_store.put(
            None, "source conversation text", {"kind": "conversation"}
        )
        node_id = _create_node(registry, document_ids=[doc_id])

        RedactionApplyHandler(registry).handle(_redact(node_id))

        # A document may back many entities — entity redaction never
        # cascades to it. Its id is preserved in the audit payload instead.
        assert registry.knowledge.document_store.get(doc_id) is not None
        events = registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )
        assert events[0].payload["document_ids"] == [doc_id]

    def test_surviving_observations_and_measurements_ride_payload(
        self, registry: StoreRegistry
    ) -> None:
        graph = registry.knowledge.graph_store
        node_id = _create_node(registry)
        graph.upsert_node(
            node_id="obs-1",
            node_type=OBSERVATION,
            properties={"subject_entity_id": node_id, "content": "about subject"},
        )
        graph.upsert_node(
            node_id="meas-1",
            node_type=MEASUREMENT,
            properties={"subject_entity_id": node_id, "metric_name": "m"},
        )

        RedactionApplyHandler(registry).handle(_redact(node_id))

        # Observations/measurements are independent governed nodes: not
        # cascaded, but surfaced as pointers so the operator can redact
        # each individually (property-based queries keep serving them).
        assert graph.get_node("obs-1") is not None
        assert graph.get_node("meas-1") is not None
        events = registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )
        payload = events[0].payload
        assert payload["linked_observation_ids"] == ["obs-1"]
        assert payload["linked_measurement_ids"] == ["meas-1"]

    def test_blank_reason_rejected_before_any_purge(
        self, registry: StoreRegistry
    ) -> None:
        node_id = _create_node(registry)
        with pytest.raises(ValidationError) as exc_info:
            RedactionApplyHandler(registry).handle(_redact(node_id, reason="   "))
        assert exc_info.value.code == "redaction_reason_required"
        # Nothing purged on rejection.
        assert registry.knowledge.graph_store.get_node(node_id) is not None

    def test_overlong_reason_rejected(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        with pytest.raises(ValidationError) as exc_info:
            RedactionApplyHandler(registry).handle(
                _redact(node_id, reason="x" * (MAX_REDACTION_REASON_CHARS + 1))
            )
        assert exc_info.value.code == "redaction_reason_too_long"
        assert registry.knowledge.graph_store.get_node(node_id) is not None

    def test_blank_reason_through_executor_is_rejected(
        self, registry: StoreRegistry
    ) -> None:
        node_id = _create_node(registry)
        result = build_curate_executor(registry).execute(_redact(node_id, reason=""))
        assert result.status == CommandStatus.REJECTED

    def test_refuses_null_event_log(self, tmp_path: Path) -> None:
        # The REDACTION_APPLIED event is the only record that survives the
        # purge; the sanctioned knowledge-plane-only no-op log would drop
        # it, so redaction refuses rather than purging unrecorded.
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        kp_registry = StoreRegistry(
            stores_dir=stores_dir,
            config={"event_log": {"backend": "null"}},
        )
        node_id = _create_node(kp_registry)
        with pytest.raises(ValidationError) as exc_info:
            RedactionApplyHandler(kp_registry).handle(_redact(node_id))
        assert exc_info.value.code == "redaction_requires_event_log"
        assert kp_registry.knowledge.graph_store.get_node(node_id) is not None

    def test_missing_target_raises_not_found(self, registry: StoreRegistry) -> None:
        with pytest.raises(NotFoundError):
            RedactionApplyHandler(registry).handle(_redact("nonexistent"))

    def test_missing_target_through_executor_is_failed(
        self, registry: StoreRegistry
    ) -> None:
        result = build_curate_executor(registry).execute(_redact("nope"))
        assert result.status == CommandStatus.FAILED
        assert "not found" in result.message

    def test_concurrent_purge_loser_fails_without_event(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # delete_node returning False means another writer purged the node
        # between our read and the delete — the loser must not emit a
        # second REDACTION_APPLIED carrying counts it did not purge.
        node_id = _create_node(registry)
        graph = registry.knowledge.graph_store
        monkeypatch.setattr(graph, "delete_node", lambda _nid: False)

        with pytest.raises(NotFoundError):
            RedactionApplyHandler(registry).handle(_redact(node_id))

        events = registry.operational.event_log.get_events(
            event_type=EventType.REDACTION_APPLIED
        )
        assert events == []

    def test_emit_failure_reports_success_with_warning(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The purge already happened when the emit runs; a raising emit
        # must not convert a completed redaction into FAILED (which would
        # also lose the executor's own record — same failing log). The
        # payload lands in operator logs instead.
        node_id = _create_node(registry)
        event_log = registry.operational.event_log

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            msg = "event log down"
            raise StoreError(msg, store="event_log")

        monkeypatch.setattr(event_log, "emit", _boom)

        returned_id, message = RedactionApplyHandler(registry).handle(_redact(node_id))

        assert returned_id == node_id
        assert "audit emit failed" in message
        assert registry.knowledge.graph_store.get_node(node_id) is None

    def test_re_redaction_fails_not_found(self, registry: StoreRegistry) -> None:
        # Redaction is not idempotent by design — a second submission
        # names a target that no longer exists. Callers wanting
        # at-most-once semantics supply Command.idempotency_key.
        node_id = _create_node(registry)
        executor = build_curate_executor(registry)
        assert executor.execute(_redact(node_id)).status == CommandStatus.SUCCESS
        assert executor.execute(_redact(node_id)).status == CommandStatus.FAILED

    def test_happy_path_through_executor(self, registry: StoreRegistry) -> None:
        node_id = _create_node(registry)
        result = build_curate_executor(registry).execute(_redact(node_id))
        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == node_id
