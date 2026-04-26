"""JSONRulesExtractor — declarative, deterministic extraction from JSON.

Consumers describe their source with a small rule bundle (entity rules +
edge rules) and hand it to :class:`JSONRulesExtractor`.  The extractor
walks any nested JSON/dict structure, emits :class:`EntityDraft` records
at each matched path, and emits :class:`EdgeDraft` records for each
cross-reference expressed by an edge rule.

This is the generic "structured source, no Python needed" extractor.
Domain-specific extractors (dbt, OpenLineage, ...) live in
``trellis_workers.extract``.

Path language
-------------

An entity rule's ``path`` is a list of string components.  Each component
is either a literal key or the wildcard ``"*"``.  ``"*"`` iterates the
*values* of the current container (works for both lists and dicts), so
a rule like::

    path=["nodes", "*"]

matches every value of ``raw["nodes"]`` whether ``nodes`` is a list or a
dict.  Field extraction within a matched item uses dotted paths
(``"depends_on.nodes"``).

Edge rules
----------

Two edge patterns are supported:

1. **Field-reference** (``source_field`` set).  The edge rule reads
   ``source_field`` on each source item (scalar or list), looks up
   target entities whose ``id_field`` value matches, and emits one edge
   per match.

2. **Ancestor** (``via_ancestor=True``).  The edge's target is an
   ancestor container of the source in the JSON tree — e.g. a column's
   enclosing table.  No field reference is needed: the extractor tracks
   which wildcard-matched items enclose each source match and emits one
   edge per source to the closest ancestor whose rule is ``target_rule``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import Field, model_validator

from trellis.core.base import TrellisModel
from trellis.extract.base import ExtractorTier
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.schemas.well_known import (
    canonicalize_edge_kind,
    canonicalize_entity_type,
    schema_alignment_for_edge_kind,
    schema_alignment_for_entity_type,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext


class EntityRule(TrellisModel):
    """Declarative rule producing :class:`EntityDraft` records.

    ``path`` walks into the raw input; ``id_field`` / ``name_field`` /
    ``property_fields`` read from each matched item.  All field paths are
    dotted (``"depends_on.nodes"``).
    """

    name: str
    path: list[str]
    entity_type: str
    id_field: str
    name_field: str | None = None
    node_role: NodeRole = NodeRole.SEMANTIC
    property_fields: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class EdgeRule(TrellisModel):
    """Declarative rule producing :class:`EdgeDraft` records.

    Two modes, exactly one of which must be selected:

    * **Field-reference** — set ``source_field``.  Reads that field from
      each source item (scalar or list) and emits one edge per referenced
      target entity (matched by the target rule's ``id_field``).
    * **Ancestor** — set ``via_ancestor=True`` (and leave ``source_field``
      ``None``).  Emits one edge per source match to the closest ancestor
      in the walk trail whose rule is ``target_rule``.  Useful for
      parent/child relationships implied by JSON nesting (e.g. a column
      and its enclosing table).
    """

    name: str
    source_rule: str
    target_rule: str
    edge_kind: str
    source_field: str | None = None
    via_ancestor: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> EdgeRule:
        if self.via_ancestor and self.source_field is not None:
            msg = "EdgeRule: set either source_field or via_ancestor=True, not both"
            raise ValueError(msg)
        if not self.via_ancestor and self.source_field is None:
            msg = "EdgeRule: must set source_field or via_ancestor=True"
            raise ValueError(msg)
        return self


class ExtractionRuleBundle(TrellisModel):
    """Complete rule bundle handed to :class:`JSONRulesExtractor`."""

    entity_rules: list[EntityRule]
    edge_rules: list[EdgeRule] = Field(default_factory=list)


class JSONRulesExtractor:
    """Deterministic rule-driven extractor for structured JSON input.

    Stateless at call time: the rule bundle is frozen at construction.
    Safe to share across concurrent ``extract()`` calls.
    """

    tier = ExtractorTier.DETERMINISTIC

    def __init__(
        self,
        name: str,
        rules: ExtractionRuleBundle,
        *,
        supported_sources: list[str],
        version: str = "0.1.0",
    ) -> None:
        self.name = name
        self._rules = rules
        self.supported_sources = list(supported_sources)
        self.version = version

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        del context  # unused — deterministic extractor has no cost budget

        # Entity rule outputs, indexed by rule name.  Each match carries
        # its raw item (for field-reference edges), its id value, and the
        # ancestor trail (for via_ancestor edges).  The trail is the
        # tuple of items yielded at each wildcard along the walk, with
        # the matched item itself as the final element.
        matches_by_rule: dict[str, list[_EntityMatch]] = {}
        for rule in self._rules.entity_rules:
            matches_by_rule[rule.name] = self._apply_entity_rule(rule, raw_input)

        entities: list[EntityDraft] = [
            m.draft for matches in matches_by_rule.values() for m in matches
        ]

        edges: list[EdgeDraft] = []
        for edge_rule in self._rules.edge_rules:
            if edge_rule.via_ancestor:
                edges.extend(self._apply_ancestor_edge_rule(edge_rule, matches_by_rule))
            else:
                edges.extend(self._apply_field_edge_rule(edge_rule, matches_by_rule))

        return ExtractionResult(
            entities=entities,
            edges=edges,
            extractor_used=self.name,
            tier=self.tier.value,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )

    # ------------------------------------------------------------------
    # Rule application
    # ------------------------------------------------------------------

    def _apply_entity_rule(
        self,
        rule: EntityRule,
        raw_input: Any,
    ) -> list[_EntityMatch]:
        # ADR Phase 1: emit canonical PascalCase entity types so retrieval
        # buckets agent-readable names and downstream RDF/JSON-LD export
        # has a stable join key. Open-string types (no canonical alias)
        # pass through unchanged — the rule author wins. Schema alignment
        # is populated only when a standards URI exists.
        canonical_type = canonicalize_entity_type(rule.entity_type)
        alignment = schema_alignment_for_entity_type(canonical_type)
        matches: list[_EntityMatch] = []
        for item, trail in _walk(raw_input, rule.path):
            if not isinstance(item, dict):
                continue
            id_value = _get_field(item, rule.id_field)
            if id_value is None:
                continue
            name_value = (
                _get_field(item, rule.name_field) if rule.name_field else id_value
            )
            properties: dict[str, Any] = {}
            for target_prop, source_field in rule.property_fields.items():
                value = _get_field(item, source_field)
                if value is not None:
                    properties[target_prop] = value
            if alignment is not None:
                properties.setdefault("schema_alignment", alignment)
            draft = EntityDraft(
                entity_id=str(id_value),
                entity_type=canonical_type,
                name=str(name_value) if name_value is not None else str(id_value),
                properties=properties,
                node_role=rule.node_role,
                confidence=rule.confidence,
            )
            matches.append(
                _EntityMatch(draft=draft, raw=item, id_value=id_value, trail=trail)
            )
        return matches

    def _apply_field_edge_rule(
        self,
        rule: EdgeRule,
        matches_by_rule: dict[str, list[_EntityMatch]],
    ) -> list[EdgeDraft]:
        assert rule.source_field is not None  # guaranteed by validator
        source_matches = matches_by_rule.get(rule.source_rule, [])
        target_matches = matches_by_rule.get(rule.target_rule, [])
        if not source_matches or not target_matches:
            return []

        target_ids = {str(m.id_value) for m in target_matches}

        canonical_kind = canonicalize_edge_kind(rule.edge_kind)
        edge_props = _edge_alignment_properties(canonical_kind)

        edges: list[EdgeDraft] = []
        for match in source_matches:
            if match.draft.entity_id is None:
                continue
            field_value = _get_field(match.raw, rule.source_field)
            if field_value is None:
                continue
            references = field_value if isinstance(field_value, list) else [field_value]
            for ref in references:
                target_id = str(ref)
                if target_id not in target_ids:
                    continue
                edges.append(
                    EdgeDraft(
                        source_id=match.draft.entity_id,
                        target_id=target_id,
                        edge_kind=canonical_kind,
                        properties=dict(edge_props),
                        confidence=rule.confidence,
                    )
                )
        return edges

    def _apply_ancestor_edge_rule(
        self,
        rule: EdgeRule,
        matches_by_rule: dict[str, list[_EntityMatch]],
    ) -> list[EdgeDraft]:
        source_matches = matches_by_rule.get(rule.source_rule, [])
        target_matches = matches_by_rule.get(rule.target_rule, [])
        if not source_matches or not target_matches:
            return []

        # Index targets by object identity of their raw item so ancestor
        # lookup is O(1) per trail step.  Identity works because the same
        # dict object appears in every descendant's trail.
        target_by_raw_id: dict[int, _EntityMatch] = {
            id(m.raw): m for m in target_matches
        }

        canonical_kind = canonicalize_edge_kind(rule.edge_kind)
        edge_props = _edge_alignment_properties(canonical_kind)

        edges: list[EdgeDraft] = []
        for match in source_matches:
            if match.draft.entity_id is None:
                continue
            # Search trail from closest ancestor outward, excluding self
            # (the source's own raw item sits at trail[-1]).
            for ancestor_item in reversed(match.trail[:-1]):
                target_match = target_by_raw_id.get(id(ancestor_item))
                if target_match is None:
                    continue
                if target_match.draft.entity_id is None:
                    continue
                edges.append(
                    EdgeDraft(
                        source_id=match.draft.entity_id,
                        target_id=target_match.draft.entity_id,
                        edge_kind=canonical_kind,
                        properties=dict(edge_props),
                        confidence=rule.confidence,
                    )
                )
                break
        return edges


# ----------------------------------------------------------------------
# Internal match carrier + path helpers
# ----------------------------------------------------------------------


def _edge_alignment_properties(canonical_kind: str) -> dict[str, Any]:
    """Build the property dict carrying ``schema_alignment`` for an edge.

    Returns an empty dict when the canonical kind has no standards URI
    (Trellis-specific verbs like ``dependsOn`` / ``attachedTo`` and any
    open-string kind). Empty dicts are intentional rather than ``None``
    so callers can ``dict(...)``-copy unconditionally without a branch
    on every match.
    """
    alignment = schema_alignment_for_edge_kind(canonical_kind)
    if alignment is None:
        return {}
    return {"schema_alignment": alignment}


@dataclass(frozen=True, slots=True)
class _EntityMatch:
    """Internal record pairing an ``EntityDraft`` with walk context.

    ``raw`` is the source dict that produced the draft (used for
    field-reference edges).  ``trail`` is the tuple of wildcard-matched
    items along the walk, with ``raw`` itself as the final element; used
    for ``via_ancestor`` edge lookups.

    Purely internal; not serialized, so a plain dataclass avoids the
    overhead of Pydantic validating opaque ancestor dicts on every
    match.
    """

    draft: EntityDraft
    raw: dict[str, Any]
    id_value: Any
    trail: tuple[Any, ...]


def _walk(
    data: Any,
    path: list[str],
    trail: tuple[Any, ...] = (),
) -> Iterator[tuple[Any, tuple[Any, ...]]]:
    """Yield ``(value, trail)`` for every value reachable via ``path``.

    ``"*"`` iterates values of the current container (list or dict) and
    extends ``trail`` with the matched item.  Literal components do an
    exact key lookup in dicts and leave ``trail`` unchanged.  Type
    mismatches silently skip — the rule is a filter, not a schema check.

    The trail contains only wildcard-matched items, in outer-to-inner
    order.  Callers can use object identity on trail elements to
    reconstruct ancestor relationships across rules walking overlapping
    paths.
    """
    if not path:
        yield data, trail
        return
    head, *tail = path
    if head == "*":
        if isinstance(data, list):
            for item in data:
                yield from _walk(item, tail, (*trail, item))
        elif isinstance(data, dict):
            for item in data.values():
                yield from _walk(item, tail, (*trail, item))
        return
    if isinstance(data, dict) and head in data:
        yield from _walk(data[head], tail, trail)


def _get_field(item: Any, field_path: str) -> Any:
    """Read a dotted field path from a nested dict.  ``None`` on miss."""
    if not isinstance(item, dict):
        return None
    current: Any = item
    for part in field_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current
