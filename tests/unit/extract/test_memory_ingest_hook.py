"""Tests for the shared flag-gated memory-extraction ingest hook."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.extract.memory_ingest_hook import (
    MEMORY_EXTRACTION_FLAG,
    build_memory_extractor,
    memory_extraction_env_enabled,
    run_memory_extraction,
)
from trellis.schemas.extraction import (
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.stores.registry import StoreRegistry


class _FakeExtractor:
    """Async extractor double returning a preset result (or raising)."""

    def __init__(self, result: ExtractionResult | None = None, *, boom: bool = False):
        self._result = result
        self._boom = boom

    async def extract(self, raw, *, source_hint, context):
        if self._boom:
            msg = "extractor exploded"
            raise RuntimeError(msg)
        return self._result


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
