"""Tests for the shared flag-gated memory-extraction ingest hook."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.extract.memory_ingest_hook import (
    MEMORY_EXTRACTION_FLAG,
    _emit_judged_extractions,
    build_memory_extractor,
    memory_extraction_env_enabled,
    run_memory_extraction,
)
from trellis.llm.types import LLMResponse, TokenUsage
from trellis.schemas.extraction import (
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
    LLMJudgedDraftRecord,
)
from trellis.schemas.memory_op import JudgedOpType, MemoryOpJudgedPayload
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


class _FakeExtractor:
    """Async extractor double returning a preset result (or raising)."""

    def __init__(self, result: ExtractionResult | None = None, *, boom: bool = False):
        self.name = "fake"
        self._result = result
        self._boom = boom

    async def extract(self, raw, *, source_hint, context):
        if self._boom:
            msg = "extractor exploded"
            raise RuntimeError(msg)
        return self._result


class _CapturingLLM:
    """LLM double that records the messages the extractor hands it."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def generate(self, **kwargs: Any) -> LLMResponse:
        self.messages = list(kwargs["messages"])
        return LLMResponse(
            content='{"entities": [], "edges": []}',
            model="fake",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def _result_with(entities: list[EntityDraft]) -> ExtractionResult:
    return ExtractionResult(
        entities=entities,
        edges=[],
        extractor_used="fake",
        tier="deterministic",
        provenance=ExtractionProvenance(extractor_name="fake"),
    )


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores = tmp_path / "stores"
    stores.mkdir()
    return StoreRegistry(stores_dir=stores)


class TestEnvFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(MEMORY_EXTRACTION_FLAG, raising=False)
        assert memory_extraction_env_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "on", "YES"])
    def test_truthy(self, monkeypatch, val):
        monkeypatch.setenv(MEMORY_EXTRACTION_FLAG, val)
        assert memory_extraction_env_enabled() is True


class TestBuildMemoryExtractor:
    def test_none_without_opt_in(self, monkeypatch):
        monkeypatch.setenv(MEMORY_EXTRACTION_FLAG, "1")
        assert build_memory_extractor(MagicMock(), opt_in=False) is None

    def test_none_without_env_flag(self, monkeypatch):
        monkeypatch.delenv(MEMORY_EXTRACTION_FLAG, raising=False)
        assert build_memory_extractor(MagicMock(), opt_in=True) is None

    def test_none_when_no_llm_client(self, monkeypatch):
        monkeypatch.setenv(MEMORY_EXTRACTION_FLAG, "1")
        reg = MagicMock()
        reg.build_llm_client.return_value = None
        assert build_memory_extractor(reg, opt_in=True) is None

    def test_builds_when_enabled_with_llm(self, monkeypatch):
        monkeypatch.setenv(MEMORY_EXTRACTION_FLAG, "1")
        reg = MagicMock()
        reg.build_llm_client.return_value = MagicMock()
        extractor = build_memory_extractor(reg, opt_in=True)
        assert extractor is not None


class TestRunMemoryExtraction:
    def test_none_extractor_is_noop(self, registry):
        assert run_memory_extraction(
            registry, None, "doc-1", "text", requested_by="test"
        ) == (0, 0)

    def test_routes_drafts_to_executor(self, registry):
        extractor = _FakeExtractor(
            _result_with([EntityDraft(entity_type="person", name="Mira")])
        )
        entities, edges = run_memory_extraction(
            registry, extractor, "doc-1", "My daughter Mira is 7.", requested_by="test"
        )
        assert (entities, edges) == (1, 0)
        # The draft actually became a graph node via the governed executor.
        nodes = registry.knowledge.graph_store.query(limit=50)
        assert len(nodes) == 1
        assert nodes[0]["node_type"] == "person"

    def test_empty_result_is_zero(self, registry):
        extractor = _FakeExtractor(_result_with([]))
        assert run_memory_extraction(
            registry, extractor, "doc-1", "text", requested_by="test"
        ) == (0, 0)

    def test_extractor_exception_is_swallowed(self, registry):
        extractor = _FakeExtractor(boom=True)
        assert run_memory_extraction(
            registry, extractor, "doc-1", "text", requested_by="test"
        ) == (0, 0)

    def test_fresh_mints_carry_doc_link_and_claim_floor(self, registry):
        """#299/#300: every stored mint links its source doc and is unconfirmed."""
        from trellis.schemas.extraction import (
            EPISTEMIC_STATUS_PROPERTY,
            EXTRACTION_STATUS_PROPERTY,
        )

        extractor = _FakeExtractor(
            _result_with([EntityDraft(entity_type="Device", name="Oura ring")])
        )
        entities, _ = run_memory_extraction(
            registry, extractor, "doc-9", "Comparing ring options.", requested_by="test"
        )
        assert entities == 1
        (node,) = registry.knowledge.graph_store.query(limit=50)
        assert node["properties"][EXTRACTION_STATUS_PROPERTY] == "unconfirmed"
        assert node["properties"][EPISTEMIC_STATUS_PROPERTY] == "mentioned"
        assert node.get("document_ids") == ["doc-9"]

    def test_participant_drafts_never_reach_the_executor(self, registry):
        """#299: person-typed speaker drafts are dropped before the batch."""
        extractor = _FakeExtractor(
            _result_with(
                [
                    EntityDraft(entity_type="Person", name="You"),
                    EntityDraft(entity_type="Person", name="Nate"),
                ]
            )
        )
        entities, edges = run_memory_extraction(
            registry,
            extractor,
            "doc-2",
            "**You:** hi\n**Claude:** hello",
            requested_by="test",
            participant_names=["Nate"],
        )
        assert (entities, edges) == (0, 0)
        assert registry.knowledge.graph_store.query(limit=50) == []

    def test_emits_only_proven_llm_fresh_mints(self, registry) -> None:
        from trellis.extract.base import ExtractorTier
        from trellis.extract.hybrid import HybridJSONExtractor
        from trellis.extract.llm import LLMExtractor

        content = "Alice introduced Acme to Bob."
        deterministic = _FakeExtractor(
            ExtractionResult(
                entities=[EntityDraft(entity_type="Person", name="Alice")],
                extractor_used="alias",
                tier="deterministic",
                overall_confidence=0.5,
                unparsed_residue=content,
                provenance=ExtractionProvenance(extractor_name="alias"),
            )
        )
        deterministic.name = "alias"
        deterministic.tier = ExtractorTier.DETERMINISTIC

        class MixedLLM:
            async def generate(self, **kwargs: Any) -> LLMResponse:
                return LLMResponse(
                    content=json.dumps(
                        {
                            "entities": [
                                {
                                    "entity_type": "Organization",
                                    "name": "Acme",
                                    "confidence": 0.9,
                                },
                                {
                                    "entity_type": "Person",
                                    "name": "Bob",
                                    "confidence": 0.8,
                                },
                            ],
                            "edges": [],
                        }
                    ),
                    model="test-model-v1",
                )

        llm = LLMExtractor(name="llm", llm_client=MixedLLM())
        extractor = HybridJSONExtractor(
            deterministic=deterministic,
            llm=llm,
            confidence_threshold=0.7,
        )

        assert run_memory_extraction(
            registry,
            extractor,
            "doc-264",
            content,
            requested_by="test",
        ) == (3, 0)

        events = registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_OP_JUDGED
        )
        payloads = [
            MemoryOpJudgedPayload.model_validate(event.payload) for event in events
        ]
        assert len(payloads) == 2
        assert {payload.op_type for payload in payloads} == {JudgedOpType.EXTRACTION}
        assert {payload.decision for payload in payloads} == {
            "Organization",
            "Person",
        }
        assert {payload.model_id for payload in payloads} == {"test-model-v1"}
        assert {event.source for event in events} == {"save_memory.extract"}
        assert all(payload.subject_ref.ref_id for payload in payloads)

    def test_failed_mint_emits_correlation_failure_not_judged(self, registry) -> None:
        from trellis.mutate.commands import (
            Command,
            CommandBatch,
            CommandResult,
            CommandStatus,
            Operation,
        )

        record = LLMJudgedDraftRecord(
            entity_type="Person",
            name="Bob",
            confidence=0.8,
            model_id="test-model-v1",
            input_hash="input-hash",
            input_length=10,
        )
        result = ExtractionResult(
            entities=[EntityDraft(entity_type="Person", name="Bob")],
            extractor_used="llm",
            tier="llm",
            judged_drafts=[record],
            provenance=ExtractionProvenance(extractor_name="llm"),
        )
        command = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "Person", "name": "Bob"},
        )
        batch = CommandBatch(commands=[command])
        command_result = CommandResult(
            command_id=command.command_id,
            operation=Operation.ENTITY_CREATE,
            status=CommandStatus.FAILED,
        )

        _emit_judged_extractions(
            registry,
            result=result,
            batch=batch,
            results=[command_result],
            doc_id="doc-264",
        )

        assert (
            registry.operational.event_log.get_events(
                event_type=EventType.MEMORY_OP_JUDGED
            )
            == []
        )
        failures = registry.operational.event_log.get_events(
            event_type=EventType.EXTRACTION_FAILED
        )
        assert len(failures) == 1
        assert failures[0].payload["failure_kind"] == "judged_op_correlation_failed"


class TestSkipDisciplineOnTheWire:
    """#311: the prompt this path *sends* carries the skip-discipline rules.

    The hook has no prompt of its own — it reaches ``MEMORY_EXTRACTION_V1``
    through ``build_save_memory_extractor``, the same factory the MCP
    ``save_memory`` path uses. Asserting the constant's text (in
    ``tests/unit/extract/prompts``) would stay green if this path later
    grew a prompt of its own, so pin the rendered system message instead.
    """

    def test_ingest_hook_transmits_skip_discipline(self, registry, monkeypatch):
        monkeypatch.setenv(MEMORY_EXTRACTION_FLAG, "1")
        llm = _CapturingLLM()
        monkeypatch.setattr(registry, "build_llm_client", lambda: llm)

        extractor = build_memory_extractor(registry, opt_in=True)
        assert extractor is not None
        # The @mention resolves against an empty graph, so the residue
        # reaches the LLM stage and its rendered prompt is observable.
        assert run_memory_extraction(
            registry,
            extractor,
            "doc-311",
            "Rotated the DSN on @nightly-job and the run went green.",
            requested_by="test",
        ) == (0, 0)

        assert llm.messages[0].role == "system"
        # Normalize wrapping — the clauses matter, not the reflow.
        system = " ".join(llm.messages[0].content.split())
        assert "Skip discipline" in system
        assert "never explain the skip in prose" in system
        assert "NEVER what you or the recording process are doing" in system


class TestSyncRecordsWiring:
    """The sync core threads an injected extractor and tallies counts."""

    def test_extractor_counts_flow_into_report(self, registry):
        from trellis.ingest_corpus.models import SyncRecord
        from trellis.ingest_corpus.sync import sync_records

        extractor = _FakeExtractor(
            _result_with([EntityDraft(entity_type="person", name="Theo")])
        )
        record = SyncRecord(
            doc_id="corpus:test:1",
            source_key="note.md",
            content="My son Theo is 4.",
        )
        report = sync_records(
            registry,
            [record],
            source_system="test",
            id_prefix="corpus:test:",
            root_label="test",
            requested_by="test",
            extractor=extractor,
        )
        assert report.counts()["ingested"] == 1
        assert report.counts()["entities_extracted"] == 1

    def test_no_extractor_means_zero(self, registry):
        from trellis.ingest_corpus.models import SyncRecord
        from trellis.ingest_corpus.sync import sync_records

        record = SyncRecord(
            doc_id="corpus:test:1", source_key="note.md", content="Some text."
        )
        report = sync_records(
            registry,
            [record],
            source_system="test",
            id_prefix="corpus:test:",
            root_label="test",
            requested_by="test",
        )
        assert report.counts()["entities_extracted"] == 0
