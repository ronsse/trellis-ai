"""Tests for the default ExtractionValidator implementations.

Closes Logic Gap 1.3 (validation half). See
``docs/design/adr-extraction-validation.md`` §6.4.
"""

from __future__ import annotations

from trellis.extract.validators import (
    DraftLocalReferenceValidator,
    EmptyResultValidator,
    ExtractionValidator,
    OrphanProvenanceValidator,
    ValidationFinding,
    default_validators,
)
from trellis.schemas.entity import GenerationSpec
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
        extractor_used="stub",
        tier="deterministic",
        provenance=ExtractionProvenance(extractor_name="stub"),
    )


def _curated_spec() -> GenerationSpec:
    return GenerationSpec(
        generator_name="rollup",
        generator_version="1.0.0",
    )


class TestEmptyResultValidator:
    def test_fires_on_zero_entities_and_zero_edges(self) -> None:
        v = EmptyResultValidator()
        findings = v.validate(_result())
        assert len(findings) == 1
        assert findings[0].code == "empty_result"
        assert findings[0].validator_name == "empty_result"

    def test_silent_when_entities_present(self) -> None:
        v = EmptyResultValidator()
        result = _result(
            entities=[EntityDraft(entity_type="concept", name="x")],
        )
        assert v.validate(result) == []

    def test_silent_when_only_edges_present(self) -> None:
        v = EmptyResultValidator()
        result = _result(
            edges=[
                EdgeDraft(
                    source_id="a",
                    target_id="b",
                    edge_kind="related_to",
                    allow_dangling=True,
                )
            ],
        )
        assert v.validate(result) == []


class TestOrphanProvenanceValidator:
    def test_fires_on_curated_without_generation_spec(self) -> None:
        v = OrphanProvenanceValidator()
        result = _result(
            entities=[
                EntityDraft(
                    entity_type="precedent",
                    name="rollup",
                    node_role=NodeRole.CURATED,
                )
            ],
        )
        findings = v.validate(result)
        assert len(findings) == 1
        assert findings[0].code == "missing_generation_spec"
        assert findings[0].affected["draft_index"] == 0
        assert findings[0].affected["name"] == "rollup"

    def test_silent_on_curated_with_generation_spec(self) -> None:
        v = OrphanProvenanceValidator()
        result = _result(
            entities=[
                EntityDraft(
                    entity_type="precedent",
                    name="rollup",
                    node_role=NodeRole.CURATED,
                    generation_spec=_curated_spec(),
                )
            ],
        )
        assert v.validate(result) == []

    def test_silent_on_semantic_without_generation_spec(self) -> None:
        v = OrphanProvenanceValidator()
        result = _result(
            entities=[
                EntityDraft(
                    entity_type="concept",
                    name="x",
                    node_role=NodeRole.SEMANTIC,
                )
            ],
        )
        assert v.validate(result) == []

    def test_silent_on_structural_without_generation_spec(self) -> None:
        v = OrphanProvenanceValidator()
        result = _result(
            entities=[
                EntityDraft(
                    entity_type="column",
                    name="x",
                    node_role=NodeRole.STRUCTURAL,
                )
            ],
        )
        assert v.validate(result) == []

    def test_reports_each_offender(self) -> None:
        v = OrphanProvenanceValidator()
        result = _result(
            entities=[
                EntityDraft(
                    entity_type="precedent",
                    name="ok",
                    node_role=NodeRole.CURATED,
                    generation_spec=_curated_spec(),
                ),
                EntityDraft(
                    entity_type="precedent",
                    name="bad",
                    node_role=NodeRole.CURATED,
                ),
                EntityDraft(
                    entity_type="precedent",
                    name="bad2",
                    node_role=NodeRole.CURATED,
                ),
            ],
        )
        findings = v.validate(result)
        assert len(findings) == 2
        names = {f.affected["name"] for f in findings}
        assert names == {"bad", "bad2"}


class TestDraftLocalReferenceValidator:
    def test_silent_when_endpoints_resolve_to_drafts(self) -> None:
        v = DraftLocalReferenceValidator()
        result = _result(
            entities=[
                EntityDraft(entity_id="ent_a", entity_type="x", name="a"),
                EntityDraft(entity_id="ent_b", entity_type="x", name="b"),
            ],
            edges=[
                EdgeDraft(
                    source_id="ent_a",
                    target_id="ent_b",
                    edge_kind="related_to",
                )
            ],
        )
        assert v.validate(result) == []

    def test_fires_on_unknown_target(self) -> None:
        v = DraftLocalReferenceValidator()
        result = _result(
            entities=[
                EntityDraft(entity_id="ent_a", entity_type="x", name="a"),
            ],
            edges=[
                EdgeDraft(
                    source_id="ent_a",
                    target_id="ent_missing",
                    edge_kind="related_to",
                )
            ],
        )
        findings = v.validate(result)
        assert len(findings) == 1
        assert findings[0].code == "orphan_edge"
        assert "target_id='ent_missing'" in findings[0].message

    def test_fires_on_both_endpoints_unknown(self) -> None:
        v = DraftLocalReferenceValidator()
        result = _result(
            edges=[
                EdgeDraft(
                    source_id="missing_a",
                    target_id="missing_b",
                    edge_kind="related_to",
                )
            ],
        )
        findings = v.validate(result)
        # One finding per edge but the message names both missing endpoints.
        assert len(findings) == 1
        assert "source_id='missing_a'" in findings[0].message
        assert "target_id='missing_b'" in findings[0].message

    def test_silent_when_allow_dangling(self) -> None:
        v = DraftLocalReferenceValidator()
        result = _result(
            edges=[
                EdgeDraft(
                    source_id="not_in_batch",
                    target_id="also_not",
                    edge_kind="related_to",
                    allow_dangling=True,
                )
            ],
        )
        assert v.validate(result) == []

    def test_skips_drafts_without_entity_id_for_local_set(self) -> None:
        """An ``EntityDraft`` with ``entity_id=None`` cannot be a draft-local
        reference target because the id is assigned later. Edges referring to
        such a draft by name should still be flagged."""
        v = DraftLocalReferenceValidator()
        result = _result(
            entities=[EntityDraft(entity_type="x", name="anonymous")],
            edges=[
                EdgeDraft(
                    source_id="anonymous",
                    target_id="anonymous",
                    edge_kind="related_to",
                )
            ],
        )
        findings = v.validate(result)
        assert len(findings) == 1
        assert findings[0].code == "orphan_edge"


class TestDefaultValidators:
    def test_returns_three_protocol_conformers(self) -> None:
        validators = default_validators()
        assert len(validators) == 3
        for v in validators:
            assert isinstance(v, ExtractionValidator)

    def test_each_validator_returns_a_list(self) -> None:
        # Smoke: empty extraction triggers exactly EmptyResultValidator.
        empty = _result()
        findings: list[ValidationFinding] = []
        for v in default_validators():
            findings.extend(v.validate(empty))
        assert len(findings) == 1
        assert findings[0].validator_name == "empty_result"
