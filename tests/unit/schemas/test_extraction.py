"""Tests for extraction draft schemas."""

from __future__ import annotations

import pytest

from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)


class TestEntityDraft:
    def test_minimal_fields(self) -> None:
        d = EntityDraft(entity_type="table", name="users")
        assert d.entity_id is None
        assert d.entity_type == "table"
        assert d.name == "users"
        assert d.properties == {}
        assert d.node_role == NodeRole.SEMANTIC
        assert d.generation_spec is None
        assert d.confidence == 1.0

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValueError):
            EntityDraft(entity_type="t", name="n", confidence=1.5)
        with pytest.raises(ValueError):
            EntityDraft(entity_type="t", name="n", confidence=-0.1)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            EntityDraft(entity_type="t", name="n", surprise="boom")

    def test_properties_preserved(self) -> None:
        d = EntityDraft(
            entity_type="column",
            name="id",
            properties={"data_type": "bigint", "nullable": False},
        )
        assert d.properties["data_type"] == "bigint"


class TestEdgeDraft:
    def test_minimal_fields(self) -> None:
        d = EdgeDraft(source_id="a", target_id="b", edge_kind="depends_on")
        assert d.properties == {}
        assert d.confidence == 1.0

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValueError):
            EdgeDraft(source_id="a", target_id="b", edge_kind="k", confidence=2.0)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            EdgeDraft(source_id="a", target_id="b", edge_kind="k", bogus=1)


class TestExtractionProvenance:
    def test_defaults(self) -> None:
        p = ExtractionProvenance(extractor_name="dbt_manifest")
        assert p.extractor_name == "dbt_manifest"
        assert p.extractor_version == "0.0.0"
        assert p.source_hint is None
        assert p.raw_input_hash is None
        assert p.extracted_at is not None

    def test_explicit_values(self) -> None:
        p = ExtractionProvenance(
            extractor_name="x",
            extractor_version="1.2.3",
            source_hint="dbt-manifest",
            raw_input_hash="deadbeef",
        )
        assert p.source_hint == "dbt-manifest"
        assert p.raw_input_hash == "deadbeef"


class TestExtractionResult:
    def test_defaults(self) -> None:
        r = ExtractionResult(
            extractor_used="x",
            tier="deterministic",
            provenance=ExtractionProvenance(extractor_name="x"),
        )
        assert r.entities == []
        assert r.edges == []
        assert r.llm_calls == 0
        assert r.tokens_used == 0
        assert r.overall_confidence == 1.0
        assert r.unparsed_residue is None

    def test_carries_drafts(self) -> None:
        r = ExtractionResult(
            entities=[EntityDraft(entity_type="t", name="users")],
            edges=[EdgeDraft(source_id="a", target_id="b", edge_kind="k")],
            extractor_used="x",
            tier="deterministic",
            provenance=ExtractionProvenance(extractor_name="x"),
        )
        assert len(r.entities) == 1
        assert len(r.edges) == 1

    def test_non_negative_counts(self) -> None:
        with pytest.raises(ValueError):
            ExtractionResult(
                extractor_used="x",
                tier="llm",
                llm_calls=-1,
                provenance=ExtractionProvenance(extractor_name="x"),
            )
        with pytest.raises(ValueError):
            ExtractionResult(
                extractor_used="x",
                tier="llm",
                tokens_used=-5,
                provenance=ExtractionProvenance(extractor_name="x"),
            )

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            ExtractionResult(
                extractor_used="x",
                tier="deterministic",
                provenance=ExtractionProvenance(extractor_name="x"),
                rogue=True,
            )
