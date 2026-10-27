"""Tier mapping — heuristic rules for section eligibility.

Maps content properties to retrieval tiers when items lack explicit
``retrieval_affinity`` tags. Items with explicit affinity bypass heuristics.

The default rules are:

- **domain_knowledge**: org/universal scope constraints, decisions, documentation;
  precedent/owner/team entities
- **technical_pattern**: patterns, procedures, code, configurations
- **operational**: traces, error-resolutions; empirical observations / measurements
- **reference**: entity metadata, configurations

Observations and measurements (Item 1 Phase 2 of the self-improvement
program — see ``docs/design/plan-observation-entity-type.md``) land in the
**operational** affinity by default: they describe runtime behaviour of a
subject entity ("table X was queried N times", "column Y has null_rate
0.03") which is the same shape of signal as traces and error-resolutions.
Applications that want their own bucket (e.g., a dedicated ``metric``
sub-tier) pass a custom heuristics dict to :class:`TierMapper`.
"""

from __future__ import annotations

from typing import Any

from trellis.core.precedent_document import is_promoted_precedent_document
from trellis.schemas.pack import PackItem, SectionRequest
from trellis.schemas.well_known import canonicalize_entity_type

#: The node type a promoted precedent's document row stands in for.
PRECEDENT_NODE_TYPE = "precedent"

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
        # Observations and measurements (Item 1 Phase 2) ride the
        # operational affinity — they're empirical claims about an
        # entity's runtime behaviour, same shape of signal as traces.
        # Both share the ``"observation"`` ``PackItem.item_type``
        # produced by :class:`ObservationSearch`.
        "item_types": {"trace", "observation"},
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
        node_types = _get_node_types(item)
        # A promoted precedent's *document row* (the copy the keyword and
        # semantic axes serve) is not a graph candidate, so ``node_types`` is
        # empty for it by design, and its tags carry a content type but no
        # scope, which the ``domain_knowledge`` rule also requires. Read from
        # its id, the one thing a caller cannot write (#957).
        if is_promoted_precedent_document(item.item_id):
            node_types = node_types | {PRECEDENT_NODE_TYPE}

        for affinity, rules in self._heuristics.items():
            # Check item_type match. A graph candidate is always
            # ``item_type="entity"`` and carries what it is in
            # ``metadata["node_type"]``, so ``item_types`` is matched against
            # both -- otherwise a rule naming ``precedent`` is unreachable.
            if rules["item_types"] and (
                item.item_type in rules["item_types"]
                or _node_type_in(node_types, rules["item_types"])
            ):
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


#: ``metadata["source_strategy"]`` values whose ``node_type`` stamp is the
#: strategy's own claim about a graph row. ``GraphSearch`` and
#: ``ObservationSearch`` write it after the property spread. ``KeywordSearch``
#: and ``SemanticSearch`` spread the *document's* open metadata bag instead,
#: so a ``node_type`` key there is author-supplied text, not a graph fact, and
#: must not pick a section (#945 is the same class of defect).
_NODE_TYPE_SOURCES = frozenset({"graph", "observation"})


def _get_node_types(item: PackItem) -> set[str]:
    """Canonical node type(s) of a graph-sourced item; empty for any other.

    Only an item the graph or observation strategy collected is read. For
    those, ``node_type`` (the stored spelling) and ``node_type_canonical`` are
    stamped by the strategy after the property spread, so a stored property
    cannot forge them. An item with no ``source_strategy``, or one from the
    keyword or semantic axis, returns empty whatever its metadata says.
    Canonicalising makes ``team`` and ``Team`` one bucket; a non-string value
    is ignored rather than raised on.
    """
    if item.metadata.get("source_strategy") not in _NODE_TYPE_SOURCES:
        return set()
    found: set[str] = set()
    for key in ("node_type", "node_type_canonical"):
        value = item.metadata.get(key)
        if isinstance(value, str) and value:
            found.add(canonicalize_entity_type(value))
    return found


def _node_type_in(node_types: set[str], item_types: set[str]) -> bool:
    """Whether any node type is named by a rule's ``item_types``.

    The rule side is canonicalised too, so a rule written ``"team"`` matches
    a row stored ``"Team"`` (and the reverse). Types outside the well-known
    set pass through unchanged, so ``precedent`` compares as itself.
    """
    if not node_types:
        return False
    return not node_types.isdisjoint(canonicalize_entity_type(t) for t in item_types)


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
