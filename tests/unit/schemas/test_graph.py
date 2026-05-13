"""Tests for graph edge schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trellis.schemas.enums import EdgeKind
from trellis.schemas.graph import Edge


class TestEdgeKindIsOpenString:
    """``edge_kind`` accepts any string — the enum is advisory.

    The storage layer stores ``edge_type`` as a plain string so
    domain-specific integrations can define their own kinds (e.g.,
    ``uc_column_of``, ``dbt_references``) without extending the core
    enum. Lock that contract in at the schema layer.
    """

    def test_custom_edge_kind_string_accepted(self) -> None:
        edge = Edge(source_id="a", target_id="b", edge_kind="uc_depends_on")
        assert edge.edge_kind == "uc_depends_on"
        assert edge.source_id == "a"
        assert edge.target_id == "b"

    def test_enum_value_still_accepted(self) -> None:
        edge = Edge(source_id="a", target_id="b", edge_kind=EdgeKind.ENTITY_DEPENDS_ON)
        assert edge.edge_kind == EdgeKind.ENTITY_DEPENDS_ON
        assert edge.edge_kind == "entity_depends_on"


class TestEdgeForbidsExtras:
    def test_edge_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            Edge(
                source_id="a",
                target_id="b",
                edge_kind="entity_related_to",
                nope="bad",  # type: ignore[call-arg]
            )


class TestEdgeProvenanceFields:
    """The five provenance fields promoted from ``properties`` in Phase 3
    of ``adr-graph-ontology.md`` §6.4 / item 2 of the self-improvement
    program. All five default to ``None``; ``confidence`` is range-
    checked and ``extractor_tier`` is allowlist-checked at the schema
    boundary."""

    def test_defaults_are_all_none(self) -> None:
        edge = Edge(source_id="a", target_id="b", edge_kind="depends_on")
        assert edge.source_trace_id is None
        assert edge.agent_id is None
        assert edge.confidence is None
        assert edge.evidence_ref is None
        assert edge.extractor_tier is None

    def test_populated_round_trips(self) -> None:
        edge = Edge(
            source_id="a",
            target_id="b",
            edge_kind="depends_on",
            source_trace_id="tr_1",
            agent_id="agent-7",
            confidence=0.5,
            evidence_ref="doc-3",
            extractor_tier="DETERMINISTIC",
        )
        assert edge.source_trace_id == "tr_1"
        assert edge.confidence == 0.5
        assert edge.extractor_tier == "DETERMINISTIC"

    def test_confidence_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError, match="less than or equal to 1"):
            Edge(
                source_id="a",
                target_id="b",
                edge_kind="depends_on",
                confidence=1.5,
            )

    def test_confidence_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            Edge(
                source_id="a",
                target_id="b",
                edge_kind="depends_on",
                confidence=-0.1,
            )

    def test_extractor_tier_outside_allowlist_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extractor_tier must be one of"):
            Edge(
                source_id="a",
                target_id="b",
                edge_kind="depends_on",
                extractor_tier="BOGUS",
            )

    def test_extractor_tier_case_sensitive(self) -> None:
        with pytest.raises(ValidationError, match="extractor_tier must be one of"):
            Edge(
                source_id="a",
                target_id="b",
                edge_kind="depends_on",
                extractor_tier="deterministic",
            )
