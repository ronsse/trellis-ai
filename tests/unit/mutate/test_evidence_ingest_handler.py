"""Core contracts for governed document and vector creation (#360 PR2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandStatus
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import EvidenceIngestHandler
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    return StoreRegistry(stores_dir=stores_dir)


def _executed_evidence_events(registry: StoreRegistry) -> list:
    return [
        event
        for event in registry.operational.event_log.get_events(
            event_type=EventType.MUTATION_EXECUTED,
            limit=50,
        )
        if event.payload.get("operation") == "evidence.ingest"
    ]


class TestEvidenceCommandPreparation:
    def test_allocates_document_id_before_submit_and_replays(
        self, registry: StoreRegistry
    ) -> None:
        command = build_evidence_ingest_command(
            content="stable body",
            requested_by="test:evidence",
        )
        assert command.target_id
        assert command.args["evidence"]["doc_id"] == command.target_id
        assert command.idempotency_key

        executor = build_curate_executor(registry)
        assert executor.execute(command).status is CommandStatus.SUCCESS
        assert executor.execute(command).status is CommandStatus.DUPLICATE
        assert registry.knowledge.document_store.count() == 1

    def test_changed_content_for_same_id_is_a_new_operation(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_curate_executor(registry)
        first = build_evidence_ingest_command(
            doc_id="doc-1",
            content="first body",
            requested_by="test:evidence",
        )
        second = build_evidence_ingest_command(
            doc_id="doc-1",
            content="changed body",
            requested_by="test:evidence",
        )

        assert first.idempotency_key != second.idempotency_key
        assert executor.execute(first).status is CommandStatus.SUCCESS
        assert executor.execute(second).status is CommandStatus.SUCCESS
        assert (
            registry.knowledge.document_store.get("doc-1")["content"] == "changed body"
        )

    def test_identical_content_with_different_ids_stays_distinct(
        self, registry: StoreRegistry
    ) -> None:
        executor = build_curate_executor(registry)
        commands = [
            build_evidence_ingest_command(
                doc_id=doc_id,
                content="same body",
                requested_by="test:evidence",
            )
            for doc_id in ("doc-1", "doc-2")
        ]

        assert commands[0].idempotency_key != commands[1].idempotency_key
        assert [executor.execute(command).status for command in commands] == [
            CommandStatus.SUCCESS,
            CommandStatus.SUCCESS,
        ]
        assert registry.knowledge.document_store.count() == 2

    def test_caller_key_wins_and_stabilizes_an_omitted_id(self) -> None:
        first = build_evidence_ingest_command(
            content="body",
            requested_by="test:evidence",
            idempotency_key="request-123",
        )
        second = build_evidence_ingest_command(
            content="body",
            requested_by="test:evidence",
            idempotency_key="request-123",
        )
        assert first.idempotency_key == second.idempotency_key == "request-123"
        assert first.target_id == second.target_id

    def test_repair_mode_is_explicitly_keyless(self) -> None:
        command = build_evidence_ingest_command(
            doc_id="trace-summary:1",
            content="body",
            requested_by="worker:embed-traces",
            embed_mode="strict",
            derive_idempotency=False,
        )
        assert command.idempotency_key is None


class TestEvidenceIngestHandler:
    def test_default_handler_writes_and_emits_audited_success(
        self, registry: StoreRegistry
    ) -> None:
        command = build_evidence_ingest_command(
            doc_id="doc-1",
            content="govern this memory",
            metadata={"source": "test"},
            requested_by="test:evidence",
        )
        result = build_curate_executor(registry).execute(command)

        assert result.status is CommandStatus.SUCCESS
        assert result.created_id == "doc-1"
        assert registry.knowledge.document_store.get("doc-1") is not None
        events = _executed_evidence_events(registry)
        assert len(events) == 1
        assert events[0].entity_id == "doc-1"
        assert events[0].payload["requested_by"] == "test:evidence"

    def test_soft_embedding_failure_keeps_document_success(
        self,
        registry: StoreRegistry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken_embedder(_content: str) -> list[float]:
            msg = "embedder unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            "trellis.retrieve.embed_ingest_hook.embed_on_ingest_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            type(registry),
            "embedding_fn",
            property(lambda _self: broken_embedder),
        )
        command = build_evidence_ingest_command(
            doc_id="doc-soft",
            content="document remains authoritative",
            requested_by="test:evidence",
        )

        result = build_curate_executor(registry).execute(command)

        assert result.status is CommandStatus.SUCCESS
        assert registry.knowledge.document_store.get("doc-soft") is not None
        assert registry.knowledge.vector_store.get("doc-soft") is None

    def test_strict_embedding_failure_is_failed_but_doc_first(
        self, registry: StoreRegistry
    ) -> None:
        def broken_embedder(_content: str) -> list[float]:
            msg = "embedder unavailable"
            raise RuntimeError(msg)

        executor = MutationExecutor(
            event_log=registry.operational.event_log,
            handlers={
                "evidence.ingest": EvidenceIngestHandler(
                    registry,
                    embed="strict",
                    embedding_fn=broken_embedder,
                )
            },
        )
        command = build_evidence_ingest_command(
            doc_id="doc-strict",
            content="repairable orphan",
            requested_by="worker:embed-traces",
            embed_mode="strict",
            derive_idempotency=False,
        )

        result = executor.execute(command)

        assert result.status is CommandStatus.FAILED
        assert registry.knowledge.document_store.get("doc-strict") is not None
        assert registry.knowledge.vector_store.get("doc-strict") is None

    def test_none_mode_never_calls_embedding(self, registry: StoreRegistry) -> None:
        def forbidden_embedder(_content: str) -> list[float]:
            message = "none mode called the embedder"
            raise AssertionError(message)

        executor = MutationExecutor(
            event_log=registry.operational.event_log,
            handlers={
                "evidence.ingest": EvidenceIngestHandler(
                    registry,
                    embed="none",
                    embedding_fn=forbidden_embedder,
                )
            },
        )
        command = build_evidence_ingest_command(
            doc_id="doc-none",
            content="embedding happens after the save_memory lock",
            requested_by="mcp:save_memory",
            embed_mode="none",
            derive_idempotency=False,
        )

        assert executor.execute(command).status is CommandStatus.SUCCESS

    def test_uri_only_evidence_is_stored(self, registry: StoreRegistry) -> None:
        command = build_evidence_ingest_command(
            doc_id="evidence-1",
            content="",
            uri="s3://bucket/evidence.json",
            requested_by="api:ingest-evidence",
        )

        result = build_curate_executor(registry).execute(command)

        assert result.status is CommandStatus.SUCCESS
        stored = registry.knowledge.document_store.get("evidence-1")
        assert stored["content"] == ""
        assert stored["metadata"]["uri"] == "s3://bucket/evidence.json"

    def test_empty_content_without_uri_is_rejected(
        self, registry: StoreRegistry
    ) -> None:
        command = build_evidence_ingest_command(
            doc_id="doc-empty",
            content=" ",
            requested_by="test:evidence",
        )

        result = build_curate_executor(registry).execute(command)

        assert result.status is CommandStatus.REJECTED
        assert registry.knowledge.document_store.get("doc-empty") is None
