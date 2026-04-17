"""Tests for reference extractors in ``trellis_workers.extract``.

The previous :class:`IngestionWorker` pipeline has been replaced by the
tiered extraction model: extractors are pure functions that return
:class:`ExtractionResult`, and the CLI (or other callers) submits the
resulting drafts through :class:`MutationExecutor`.
"""

from __future__ import annotations

import pytest

from trellis.extract.base import ExtractorTier
from trellis_workers.extract import (
    DbtManifestExtractor,
    OpenLineageExtractor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DBT_MANIFEST: dict = {
    "nodes": {
        "model.my_project.stg_orders": {
            "unique_id": "model.my_project.stg_orders",
            "resource_type": "model",
            "name": "stg_orders",
            "schema": "staging",
            "database": "analytics",
            "description": "Staged orders from source",
            "depends_on": {"nodes": ["source.my_project.raw.orders"]},
            "config": {"materialized": "view"},
            "tags": ["staging"],
        },
        "model.my_project.fct_orders": {
            "unique_id": "model.my_project.fct_orders",
            "resource_type": "model",
            "name": "fct_orders",
            "schema": "marts",
            "description": "Fact table for orders",
            "depends_on": {"nodes": ["model.my_project.stg_orders"]},
            "config": {"materialized": "table"},
            "tags": ["marts"],
        },
        "test.my_project.not_null_stg_orders_id": {
            "unique_id": "test.my_project.not_null_stg_orders_id",
            "resource_type": "test",
            "name": "not_null_stg_orders_id",
            "depends_on": {"nodes": ["model.my_project.stg_orders"]},
            "tags": [],
        },
    },
    "sources": {
        "source.my_project.raw.orders": {
            "unique_id": "source.my_project.raw.orders",
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "public",
            "description": "Raw orders table",
        },
    },
}


SAMPLE_OL_EVENTS: list[dict] = [
    {
        "eventType": "COMPLETE",
        "job": {"namespace": "spark", "name": "etl_job"},
        "inputs": [{"namespace": "warehouse", "name": "raw.events"}],
        "outputs": [{"namespace": "warehouse", "name": "analytics.daily_events"}],
    },
    {
        "eventType": "COMPLETE",
        "job": {"namespace": "spark", "name": "agg_job"},
        "inputs": [{"namespace": "warehouse", "name": "analytics.daily_events"}],
        "outputs": [{"namespace": "warehouse", "name": "analytics.weekly_summary"}],
    },
]


# ---------------------------------------------------------------------------
# DbtManifestExtractor
# ---------------------------------------------------------------------------


class TestDbtManifestExtractor:
    def test_metadata(self) -> None:
        ext = DbtManifestExtractor()
        assert ext.name == "dbt_manifest"
        assert ext.tier is ExtractorTier.DETERMINISTIC
        assert "dbt-manifest" in ext.supported_sources

    async def test_produces_entities_for_nodes_and_sources(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST, source_hint="dbt-manifest")

        entity_ids = {e.entity_id for e in result.entities}
        assert "model.my_project.stg_orders" in entity_ids
        assert "source.my_project.raw.orders" in entity_ids
        assert "test.my_project.not_null_stg_orders_id" in entity_ids

        type_map = {e.entity_id: e.entity_type for e in result.entities}
        assert type_map["model.my_project.stg_orders"] == "dbt_model"
        assert type_map["source.my_project.raw.orders"] == "dbt_source"
        assert type_map["test.my_project.not_null_stg_orders_id"] == "dbt_test"

    async def test_model_properties(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST)

        stg = next(
            e for e in result.entities if e.entity_id == "model.my_project.stg_orders"
        )
        assert stg.name == "stg_orders"
        assert stg.properties["schema"] == "staging"
        assert stg.properties["materialized"] == "view"
        assert stg.properties["description"] == "Staged orders from source"

    async def test_source_properties(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST)

        src = next(
            e for e in result.entities if e.entity_id == "source.my_project.raw.orders"
        )
        assert src.properties["source_name"] == "raw"

    async def test_dependency_edges(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST)

        pairs = {(e.source_id, e.target_id, e.edge_kind) for e in result.edges}
        assert (
            "model.my_project.stg_orders",
            "source.my_project.raw.orders",
            "depends_on",
        ) in pairs
        assert (
            "model.my_project.fct_orders",
            "model.my_project.stg_orders",
            "depends_on",
        ) in pairs

    async def test_empty_manifest(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract({"nodes": {}, "sources": {}})
        assert result.entities == []
        assert result.edges == []

    async def test_missing_unique_id_skipped(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(
            {
                "nodes": {
                    "bad": {"resource_type": "model", "name": "no_id"},
                    "good": {
                        "unique_id": "model.p.good",
                        "resource_type": "model",
                        "name": "good",
                    },
                },
            }
        )
        ids = {e.entity_id for e in result.entities}
        assert ids == {"model.p.good"}

    async def test_non_dict_input_raises(self) -> None:
        ext = DbtManifestExtractor()
        with pytest.raises(TypeError):
            await ext.extract([1, 2, 3])

    async def test_provenance(self) -> None:
        ext = DbtManifestExtractor()
        result = await ext.extract(SAMPLE_DBT_MANIFEST, source_hint="dbt-manifest")
        assert result.extractor_used == "dbt_manifest"
        assert result.tier == ExtractorTier.DETERMINISTIC.value
        assert result.provenance.extractor_name == "dbt_manifest"
        assert result.provenance.source_hint == "dbt-manifest"


# ---------------------------------------------------------------------------
# OpenLineageExtractor
# ---------------------------------------------------------------------------


class TestOpenLineageExtractor:
    def test_metadata(self) -> None:
        ext = OpenLineageExtractor()
        assert ext.name == "openlineage"
        assert ext.tier is ExtractorTier.DETERMINISTIC
        assert "openlineage" in ext.supported_sources

    async def test_produces_job_and_dataset_entities(self) -> None:
        ext = OpenLineageExtractor()
        result = await ext.extract(SAMPLE_OL_EVENTS)

        ids = {e.entity_id for e in result.entities}
        assert "job:spark:etl_job" in ids
        assert "job:spark:agg_job" in ids
        assert "dataset:warehouse:raw.events" in ids
        assert "dataset:warehouse:analytics.daily_events" in ids
        assert "dataset:warehouse:analytics.weekly_summary" in ids

        type_map = {e.entity_id: e.entity_type for e in result.entities}
        assert type_map["job:spark:etl_job"] == "job"
        assert type_map["dataset:warehouse:raw.events"] == "dataset"

    async def test_reads_and_writes_edges(self) -> None:
        ext = OpenLineageExtractor()
        result = await ext.extract(SAMPLE_OL_EVENTS)

        pairs = {(e.source_id, e.target_id, e.edge_kind) for e in result.edges}
        assert (
            "job:spark:etl_job",
            "dataset:warehouse:raw.events",
            "reads_from",
        ) in pairs
        assert (
            "job:spark:etl_job",
            "dataset:warehouse:analytics.daily_events",
            "writes_to",
        ) in pairs

    async def test_deduplicates_edges(self) -> None:
        ext = OpenLineageExtractor()
        duplicate_events = [SAMPLE_OL_EVENTS[0], SAMPLE_OL_EVENTS[0]]
        result = await ext.extract(duplicate_events)
        reads = [e for e in result.edges if e.edge_kind == "reads_from"]
        writes = [e for e in result.edges if e.edge_kind == "writes_to"]
        assert len(reads) == 1
        assert len(writes) == 1

    async def test_skips_events_without_job(self) -> None:
        ext = OpenLineageExtractor()
        result = await ext.extract(
            [
                {"eventType": "COMPLETE", "job": {"namespace": "", "name": ""}},
                {"eventType": "COMPLETE"},
            ]
        )
        assert result.entities == []
        assert result.edges == []

    async def test_non_list_input_raises(self) -> None:
        ext = OpenLineageExtractor()
        with pytest.raises(TypeError):
            await ext.extract({"not": "a list"})

    async def test_provenance(self) -> None:
        ext = OpenLineageExtractor()
        result = await ext.extract(SAMPLE_OL_EVENTS, source_hint="openlineage")
        assert result.extractor_used == "openlineage"
        assert result.tier == ExtractorTier.DETERMINISTIC.value
        assert result.provenance.source_hint == "openlineage"
