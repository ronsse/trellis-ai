"""Tests for TierMapper — heuristic section eligibility rules."""

from __future__ import annotations

from trellis.retrieve.tier_mapping import TierMapper
from trellis.schemas.pack import PackItem, SectionRequest


def _item(
    item_id: str = "item1",
    item_type: str = "document",
    content_type: str | None = None,
    scope: str | None = None,
    retrieval_affinity: list[str] | None = None,
) -> PackItem:
    """Build a PackItem with optional content_tags metadata."""
    tags: dict[str, object] = {}
    if content_type is not None:
        tags["content_type"] = content_type
    if scope is not None:
        tags["scope"] = scope
    if retrieval_affinity is not None:
        tags["retrieval_affinity"] = retrieval_affinity
    metadata: dict[str, object] = {"content_tags": tags} if tags else {}
    return PackItem(
        item_id=item_id,
        item_type=item_type,
        excerpt="test",
        metadata=metadata,
    )


class TestTierMapper:
    """Tests for TierMapper.matches_section()."""

    def test_domain_knowledge_matches_org_constraint(self) -> None:
        """Item with content_type=constraint, scope=org matches domain_knowledge."""
        mapper = TierMapper()
        item = _item(content_type="constraint", scope="org")
        section = SectionRequest(
            name="domain", retrieval_affinities=["domain_knowledge"]
        )
        assert mapper.matches_section(item, section) is True

    def test_technical_pattern_matches_code(self) -> None:
        """Item with content_type=code matches technical_pattern."""
        mapper = TierMapper()
        item = _item(content_type="code")
        section = SectionRequest(
            name="patterns", retrieval_affinities=["technical_pattern"]
        )
        assert mapper.matches_section(item, section) is True

    def test_operational_matches_trace_item_type(self) -> None:
        """Item with item_type=trace matches operational."""
        mapper = TierMapper()
        item = _item(item_type="trace")
        section = SectionRequest(name="ops", retrieval_affinities=["operational"])
        assert mapper.matches_section(item, section) is True

    def test_reference_matches_entity_item_type(self) -> None:
        """Item with item_type=entity matches reference."""
        mapper = TierMapper()
        item = _item(item_type="entity")
        section = SectionRequest(name="ref", retrieval_affinities=["reference"])
        assert mapper.matches_section(item, section) is True

    def test_explicit_affinity_bypasses_heuristics(self) -> None:
        """Explicit affinity matches even if content_type doesn't."""
        mapper = TierMapper()
        # content_type=code heuristically matches technical_pattern,
        # but explicit affinity overrides that.
        item = _item(
            content_type="code",
            retrieval_affinity=["domain_knowledge"],
        )
        section = SectionRequest(
            name="domain", retrieval_affinities=["domain_knowledge"]
        )
        assert mapper.matches_section(item, section) is True

    def test_unclassified_item_matched_by_heuristic(self) -> None:
        """No affinity tags, but matching properties → heuristic match."""
        mapper = TierMapper()
        item = _item(content_type="procedure")
        section = SectionRequest(
            name="patterns", retrieval_affinities=["technical_pattern"]
        )
        assert mapper.matches_section(item, section) is True

    def test_wildcard_section_matches_everything(self) -> None:
        """SectionRequest with no filters matches all items."""
        mapper = TierMapper()
        item = _item(content_type="code", scope="project")
        section = SectionRequest(name="everything")  # no filters
        assert mapper.matches_section(item, section) is True

    def test_entity_id_direct_match(self) -> None:
        """entity_ids match by item_id regardless of other properties."""
        mapper = TierMapper()
        item = _item(item_id="uc://catalog.schema.table")
        section = SectionRequest(
            name="direct",
            entity_ids=["uc://catalog.schema.table", "uc://other"],
        )
        assert mapper.matches_section(item, section) is True

    def test_no_match_returns_false(self) -> None:
        """Item that doesn't match any criteria returns False."""
        mapper = TierMapper()
        # Plain document with no tags — heuristics won't match domain_knowledge
        # because domain_knowledge requires scope match too
        item = _item(item_type="document")
        section = SectionRequest(
            name="domain", retrieval_affinities=["domain_knowledge"]
        )
        assert mapper.matches_section(item, section) is False
