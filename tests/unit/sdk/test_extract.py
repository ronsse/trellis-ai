"""Tests for the SDK-side extractor contract surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trellis.testing import in_memory_async_client, in_memory_client
from trellis_sdk.extract import (
    DraftExtractor,
    EdgeDraft,
    EntityDraft,
    ExtractionBatch,
    ExtractorTier,
)


class TestReExports:
    """The SDK's ``extract`` module re-exports wire DTOs so client
    packages can depend on ``trellis_sdk`` alone and get everything
    they need."""

    def test_dtos_importable_from_sdk(self):
        # If these are the wire DTOs, construction works the same way.
        draft = EntityDraft(entity_type="domain.widget", name="w1")
        assert draft.entity_type == "domain.widget"

    def test_tier_enum_has_three_members(self):
        assert {t.value for t in ExtractorTier} == {
            "deterministic",
            "hybrid",
            "llm",
        }


class TestDraftExtractorProtocol:
    """Protocol conformance is structural.  Client classes that have
    the right attributes satisfy the Protocol without inheritance."""

    def test_minimal_extractor_class_conforms(self):
        class ExampleExtractor:
            name = "example_pkg.reader"
            version = "0.1.0"
            tier = ExtractorTier.DETERMINISTIC

            def extract(self, raw: Any) -> ExtractionBatch:
                return ExtractionBatch(
                    source="example",
                    extractor_name=self.name,
                    extractor_version=self.version,
                )

        instance = ExampleExtractor()
        assert isinstance(instance, DraftExtractor)

    def test_missing_attr_breaks_conformance(self):
        class BadExtractor:
            name = "bad"
            # missing version, tier, extract

        instance = BadExtractor()
        assert not isinstance(instance, DraftExtractor)


class TestSubmitDraftsSync:
    @pytest.fixture
    def client(self, tmp_path: Path):
        with in_memory_client(tmp_path / "stores") as c:
            yield c

    def test_submit_end_to_end(self, client):
        batch = ExtractionBatch(
            source="example",
            extractor_name="example.reader",
            extractor_version="0.1.0",
            entities=[
                EntityDraft(entity_type="example.widget", name="widget-1"),
                EntityDraft(entity_type="example.widget", name="widget-2"),
            ],
        )
        result = client.submit_drafts(batch)
        assert result.succeeded == 2
        assert result.extractor == "example.reader@0.1.0"

    def test_entities_then_edges(self, client):
        batch = ExtractionBatch(
            source="example",
            extractor_name="example.reader",
            extractor_version="0.1.0",
            entities=[
                EntityDraft(
                    entity_type="example.widget",
                    name="widget-1",
                    entity_id="w1",
                ),
                EntityDraft(
                    entity_type="example.widget",
                    name="widget-2",
                    entity_id="w2",
                ),
            ],
            edges=[
                EdgeDraft(source_id="w1", target_id="w2", edge_kind="example.likes"),
            ],
        )
        result = client.submit_drafts(batch)
        assert result.entities_submitted == 2
        assert result.edges_submitted == 1


class TestSubmitDraftsAsync:
    async def test_submit_end_to_end(self, tmp_path: Path):
        async with in_memory_async_client(tmp_path / "stores") as client:
            batch = ExtractionBatch(
                source="example",
                extractor_name="example.reader",
                extractor_version="0.1.0",
                entities=[
                    EntityDraft(entity_type="example.widget", name="w1"),
                ],
            )
            result = await client.submit_drafts(batch)
            assert result.succeeded == 1
