"""Tier mapping — heuristic rules for section eligibility.

Maps content properties to retrieval tiers when items lack explicit
``retrieval_affinity`` tags. Items with explicit affinity bypass heuristics.

The default rules are:

- **domain_knowledge**: org/universal scope constraints, decisions, documentation;
  precedent/owner/team entities
- **technical_pattern**: patterns, procedures, code, configurations
- **operational**: traces, error-resolutions
- **reference**: entity metadata, configurations

Applications can override these rules by passing a custom ``TierMapper``
to ``PackBuilder.build_sectioned()``.
"""

from __future__ import annotations

from typing import Any

from trellis.schemas.pack import PackItem, SectionRequest

# Default affinity → (content_types, scopes, item_types) mapping.
# Used when an item has no explicit retrieval_affinity tags.
_DEFAULT_HEURISTICS: dict[str, dict[str, Any]] = {
    "domain_knowledge": {
        "content_types": {"constraint", "decision", "documentation"},
        "scopes": {"universal", "org"},
        "item_types": {"precedent", "owner", "team"},
        "require_scope": True,  # must match BOTH content_type AND scope
    },
    "technical_pattern": {
        "content_types": {"pattern", "procedure", "code", "configuration"},
        "scopes": set(),  # any scope
        "item_types": set(),
        "require_scope": False,
    },
    "operational": {
        "content_types": {"error-resolution"},
        "scopes": set(),
        "item_types": {"trace"},
        "require_scope": False,
    },
    "reference": {
        "content_types": {"configuration"},
        "scopes": set(),
        "item_types": {"entity"},
        "require_scope": False,
    },
}


class TierMapper:
    """Maps pack items to section eligibility using heuristic rules.

    Items with explicit ``retrieval_affinity`` tags in their metadata
    are matched directly. Items without tags are matched using configurable
    heuristic rules based on ``content_type``, ``scope``, and ``item_type``.
    """

    def __init__(
        self,
        heuristics: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._heuristics = heuristics or _DEFAULT_HEURISTICS

    def matches_section(
        self,
        item: PackItem,
        section: SectionRequest,
    ) -> bool:
        """Check if an item is eligible for a section.

        Matching logic:
        1. If section has no filters (no affinities, content_types, scopes,
           entity_ids), the item matches (wildcard section).
        2. If section specifies ``entity_ids`` and item's ``item_id`` is in
           the list, the item matches immediately.
        3. If item has explicit ``retrieval_affinity`` in metadata and section
           specifies ``retrieval_affinities``, match on overlap.
        4. Otherwise, use heuristic rules to infer affinity from item properties.
        5. Content type and scope filters on the section are applied as
           additional constraints.
        """
        has_any_filter = bool(
            section.retrieval_affinities
            or section.content_types
            or section.scopes
            or section.entity_ids
        )
        if not has_any_filter:
            return True

        # Entity ID direct match
        if section.entity_ids and item.item_id in section.entity_ids:
            return True

        # Content type filter
        if section.content_types:
            item_ct = _get_content_type(item)
            if item_ct and item_ct not in section.content_types:
                return False

        # Scope filter
        if section.scopes:
            item_scope = _get_scope(item)
            if item_scope and item_scope not in section.scopes:
                return False

        # Affinity matching
        if not section.retrieval_affinities:
            return True

        # Use explicit affinity if present, otherwise infer from heuristics
        item_affinities = _get_affinities(item) or self._infer_affinities(item)
        return bool(set(item_affinities) & set(section.retrieval_affinities))

    def _infer_affinities(self, item: PackItem) -> list[str]:
        """Infer retrieval affinities from item properties using heuristic rules."""
        inferred: list[str] = []
        item_ct = _get_content_type(item)
        item_scope = _get_scope(item)

        for affinity, rules in self._heuristics.items():
            # Check item_type match
            if rules["item_types"] and item.item_type in rules["item_types"]:
                inferred.append(affinity)
                continue

            # Check content_type match
            ct_match = not rules["content_types"] or (
                item_ct and item_ct in rules["content_types"]
            )

            # Check scope match (only if required)
            if rules["require_scope"]:
                scope_match = not rules["scopes"] or (
                    item_scope and item_scope in rules["scopes"]
                )
                if ct_match and scope_match:
                    inferred.append(affinity)
            elif ct_match and rules["content_types"]:
                inferred.append(affinity)

        return inferred


def _get_content_type(item: PackItem) -> str | None:
    """Extract content_type from item metadata or content_tags."""
    tags = item.metadata.get("content_tags", {})
    if isinstance(tags, dict):
        return tags.get("content_type")
    return item.metadata.get("content_type")


def _get_scope(item: PackItem) -> str | None:
    """Extract scope from item metadata or content_tags."""
    tags = item.metadata.get("content_tags", {})
    if isinstance(tags, dict):
        return tags.get("scope")
    return item.metadata.get("scope")


def _get_affinities(item: PackItem) -> list[str]:
    """Extract explicit retrieval_affinity from item metadata."""
    tags = item.metadata.get("content_tags", {})
    if isinstance(tags, dict):
        affinities = tags.get("retrieval_affinity", [])
        if isinstance(affinities, list):
            return affinities
    affinities = item.metadata.get("retrieval_affinity", [])
    if isinstance(affinities, list):
        return affinities
    return []
