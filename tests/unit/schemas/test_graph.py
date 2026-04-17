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
