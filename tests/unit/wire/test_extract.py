"""Tests for wire extract DTOs + draft translators.

Mirror structure of :mod:`tests.unit.wire.test_parity` — verify the
wire-side extract DTOs stay in sync with core, and that the
translators produce well-formed core objects.
"""

from __future__ import annotations

import pytest

from trellis.schemas.extraction import (
    EdgeDraft as CoreEdgeDraft,
)
from trellis.schemas.extraction import (
    EntityDraft as CoreEntityDraft,
)
from trellis.schemas.extraction import (
    ExtractionResult as CoreExtractionResult,
)
from trellis.wire.translate import (
    edge_draft_to_core,
    entity_draft_to_core,
    extraction_batch_to_core_result,
)
from trellis_wire import (
    EdgeDraft,
    EntityDraft,
    ExtractionBatch,
    ExtractorTier,
    NodeRole,
)


class TestEntityDraftTranslation:
    def test_minimal_draft_round_trips(self):
        wire = EntityDraft(entity_type="unity_catalog.table", name="sales.orders")
        core = entity_draft_to_core(wire)
        assert isinstance(core, CoreEntityDraft)
        assert core.entity_type == "unity_catalog.table"
        assert core.name == "sales.orders"
        assert core.entity_id is None
        assert core.confidence == 1.0
        assert core.generation_spec is None

    def test_all_fields_translate(self):
        wire = EntityDraft(
            entity_type="unity_catalog.table",
            name="sales.orders",
            entity_id="uc://sales.orders",
            properties={"owner": "data-team", "columns": ["id", "amount"]},
            node_role=NodeRole.STRUCTURAL,
            confidence=0.85,
        )
        core = entity_draft_to_core(wire)
        assert core.entity_id == "uc://sales.orders"
        assert core.properties == {"owner": "data-team", "columns": ["id", "amount"]}
        assert core.node_role.value == "structural"
        assert core.confidence == 0.85

    def test_properties_are_copied_not_shared(self):
        """Mutating the core properties must not leak into the wire draft."""
        wire = EntityDraft(
            entity_type="t",
            name="n",
            properties={"a": 1},
        )
        core = entity_draft_to_core(wire)
        core.properties["b"] = 2
        # Wire frozen model — attribute reassignment would raise — but
        # the properties dict is a separate copy so we're safe.
        assert wire.properties == {"a": 1}

    def test_generation_spec_never_set_from_wire(self):
        """Client drafts don't carry curated-node provenance."""
        wire = EntityDraft(entity_type="t", name="n")
        core = entity_draft_to_core(wire)
        assert core.generation_spec is None


class TestEdgeDraftTranslation:
    def test_round_trip(self):
        wire = EdgeDraft(
            source_id="a",
            target_id="b",
            edge_kind="unity_catalog.contains",
            properties={"depth": 2},
            confidence=0.9,
        )
        core = edge_draft_to_core(wire)
        assert isinstance(core, CoreEdgeDraft)
        assert core.source_id == "a"
        assert core.target_id == "b"
        assert core.edge_kind == "unity_catalog.contains"
        assert core.properties == {"depth": 2}
        assert core.confidence == 0.9


class TestExtractionBatchTranslation:
    def test_empty_batch(self):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="trellis_unity_catalog.reader",
            extractor_version="0.1.0",
        )
        result = extraction_batch_to_core_result(batch)
        assert isinstance(result, CoreExtractionResult)
        assert result.entities == []
        assert result.edges == []
        assert result.extractor_used == "trellis_unity_catalog.reader@0.1.0"
        assert result.tier == "deterministic"
        assert result.llm_calls == 0
        assert result.tokens_used == 0
        assert result.provenance.extractor_name == "trellis_unity_catalog.reader"
        assert result.provenance.extractor_version == "0.1.0"
        assert result.provenance.source_hint == "unity_catalog"

    def test_batch_with_drafts(self):
        batch = ExtractionBatch(
            source="unity_catalog",
            extractor_name="trellis_unity_catalog.reader",
            extractor_version="0.3.1",
            entities=[
                EntityDraft(entity_type="unity_catalog.table", name="sales.orders"),
                EntityDraft(entity_type="unity_catalog.table", name="sales.refunds"),
            ],
            edges=[
                EdgeDraft(
                    source_id="sales.orders",
                    target_id="sales.refunds",
                    edge_kind="unity_catalog.derived_from",
                ),
            ],
            tier=ExtractorTier.HYBRID,
        )
        result = extraction_batch_to_core_result(batch)
        assert len(result.entities) == 2
        assert len(result.edges) == 1
        assert result.tier == "hybrid"
        # Spot-check translated values
        assert result.entities[0].entity_type == "unity_catalog.table"
        assert result.edges[0].edge_kind == "unity_catalog.derived_from"

    def test_tier_enum_values_match_core(self):
        """Wire ExtractorTier values must match core ExtractorTier."""
        from trellis.extract.base import ExtractorTier as CoreTier

        assert {t.value for t in ExtractorTier} == {t.value for t in CoreTier}


class TestImmutability:
    """Request DTOs are frozen; mutation after construction raises."""

    def test_entity_draft_frozen(self):
        draft = EntityDraft(entity_type="t", name="n")
        with pytest.raises((ValueError, TypeError)):
            draft.name = "changed"  # type: ignore[misc]

    def test_edge_draft_frozen(self):
        draft = EdgeDraft(source_id="a", target_id="b", edge_kind="k")
        with pytest.raises((ValueError, TypeError)):
            draft.edge_kind = "changed"  # type: ignore[misc]

    def test_extraction_batch_frozen(self):
        batch = ExtractionBatch(
            source="s", extractor_name="e", extractor_version="0.1.0"
        )
        with pytest.raises((ValueError, TypeError)):
            batch.source = "changed"  # type: ignore[misc]


class TestExtraFieldsForbidden:
    def test_entity_draft_forbids_extras(self):
        with pytest.raises(ValueError, match="Extra inputs"):
            EntityDraft(
                entity_type="t",
                name="n",
                unknown_field="boom",  # type: ignore[call-arg]
            )

    def test_extraction_batch_forbids_extras(self):
        with pytest.raises(ValueError, match="Extra inputs"):
            ExtractionBatch(
                source="s",
                extractor_name="e",
                extractor_version="0.1.0",
                wat="nope",  # type: ignore[call-arg]
            )
