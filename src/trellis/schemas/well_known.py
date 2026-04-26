"""Well-known canonical entity types and edge kinds.

Canonical names align with **schema.org** for entity types and **PROV-O**
for trace/provenance edge kinds. See
:doc:`docs/design/adr-graph-ontology.md` for the full decision rationale.

This module is the source of truth for canonical names. The
``EntityType`` and ``EdgeKind`` ``StrEnum``\\ s in :mod:`trellis.schemas.enums`
remain unchanged as the legacy registry — the lowercase values
(``"person"``, ``"trace_used_evidence"``, …) keep working forever as
permanent aliases. New code should prefer the canonical constants
defined here.

Example
-------

>>> from trellis.schemas import well_known as wk
>>> wk.PERSON
'Person'
>>> wk.canonicalize_entity_type("person")
'Person'
>>> wk.canonicalize_entity_type("Person")
'Person'
>>> wk.canonicalize_entity_type("dbt_model")  # unknown — pass-through
'dbt_model'
>>> wk.is_canonical_entity_type("Person")
True
>>> wk.is_known_entity_type("person")  # alias is "known", not "canonical"
True

The ``canonicalize_*`` helpers are pass-through for unknown strings
because Trellis explicitly supports open-string entity / edge types.
This module narrows the *well-known* defaults; it does not close the
type system. Domain extensions (e.g., ``trellis_workers.extract`` for
data-platform types) define their own values in their own packages.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Canonical entity types — schema.org + PROV-O
# ---------------------------------------------------------------------------

# schema.org classes
PERSON: Final = "Person"
ORGANIZATION: Final = "Organization"
TEAM: Final = "Team"  # schema.org subset of Organization; kept distinct
SOFTWARE_APPLICATION: Final = "SoftwareApplication"
DATASET: Final = "Dataset"
CREATIVE_WORK: Final = "CreativeWork"
PRODUCT: Final = "Product"
EVENT: Final = "Event"
PLACE: Final = "Place"
FILE: Final = "File"  # schema.org MediaObject subtype
PROJECT: Final = "Project"  # no exact schema.org match; kept Trellis-specific
CONCEPT: Final = "Concept"  # no exact schema.org match; kept Trellis-specific

# PROV-O classes
AGENT: Final = "Agent"
ACTIVITY: Final = "Activity"

CANONICAL_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {
        PERSON,
        ORGANIZATION,
        TEAM,
        SOFTWARE_APPLICATION,
        DATASET,
        CREATIVE_WORK,
        PRODUCT,
        EVENT,
        PLACE,
        FILE,
        PROJECT,
        CONCEPT,
        AGENT,
        ACTIVITY,
    }
)

# Legacy lowercase → canonical PascalCase. Multiple legacy values can
# map to the same canonical (e.g., ``system``/``service``/``tool`` all
# collapse onto ``SoftwareApplication``).
ENTITY_TYPE_ALIASES: Final[dict[str, str]] = {
    "person": PERSON,
    "team": TEAM,
    "system": SOFTWARE_APPLICATION,
    "service": SOFTWARE_APPLICATION,
    "tool": SOFTWARE_APPLICATION,
    "document": CREATIVE_WORK,
    "file": FILE,
    "project": PROJECT,
    "concept": CONCEPT,
    # ``domain`` is intentionally *not* aliased. It is removed from the
    # well-known defaults (per ADR) because it collides with the
    # ContentTags.domain classification facet. Existing data using
    # ``entity_type="domain"`` continues to work as an open string.
}

# ---------------------------------------------------------------------------
# Canonical edge kinds — PROV-O verbs + Trellis-specific kept values
# ---------------------------------------------------------------------------

# PROV-O verbs (camelCase, verbatim from the spec)
USED: Final = "used"
WAS_GENERATED_BY: Final = "wasGeneratedBy"
WAS_INFORMED_BY: Final = "wasInformedBy"
WAS_DERIVED_FROM: Final = "wasDerivedFrom"
WAS_ATTRIBUTED_TO: Final = "wasAttributedTo"
WAS_ASSOCIATED_WITH: Final = "wasAssociatedWith"

# schema.org / SKOS / universal verbs
PART_OF: Final = "partOf"
DEPENDS_ON: Final = "dependsOn"
RELATED_TO: Final = "relatedTo"

# Trellis-specific verbs kept (no clean PROV-O equivalent)
ATTACHED_TO: Final = "attachedTo"
SUPPORTS: Final = "supports"
APPLIES_TO: Final = "appliesTo"

CANONICAL_EDGE_KINDS: Final[frozenset[str]] = frozenset(
    {
        USED,
        WAS_GENERATED_BY,
        WAS_INFORMED_BY,
        WAS_DERIVED_FROM,
        WAS_ATTRIBUTED_TO,
        WAS_ASSOCIATED_WITH,
        PART_OF,
        DEPENDS_ON,
        RELATED_TO,
        ATTACHED_TO,
        SUPPORTS,
        APPLIES_TO,
    }
)

# Legacy snake_case → canonical camelCase. Multiple legacy values can
# map to the same canonical (e.g., both ``trace_promoted_to_precedent``
# and ``precedent_derived_from`` collapse onto ``wasDerivedFrom``).
EDGE_KIND_ALIASES: Final[dict[str, str]] = {
    "trace_used_evidence": USED,
    "trace_produced_artifact": WAS_GENERATED_BY,
    "trace_touched_entity": WAS_INFORMED_BY,
    "trace_promoted_to_precedent": WAS_DERIVED_FROM,
    "precedent_derived_from": WAS_DERIVED_FROM,
    "entity_related_to": RELATED_TO,
    "entity_part_of": PART_OF,
    "entity_depends_on": DEPENDS_ON,
    "evidence_attached_to": ATTACHED_TO,
    "evidence_supports": SUPPORTS,
    "precedent_applies_to": APPLIES_TO,
}


# ---------------------------------------------------------------------------
# schema_alignment URIs — Phase 1 of adr-graph-ontology.md
# ---------------------------------------------------------------------------
#
# Maps each canonical name to the standards URI it aligns with, exactly
# as enumerated in the ADR's vocabulary tables (§3.1, §3.2). Trellis-
# specific canonicals (``Project``, ``Concept``, ``dependsOn``,
# ``attachedTo``, ``supports``, ``appliesTo``) intentionally have no
# alignment URI — there's no standard to point at and inventing one
# would mislead downstream RDF/JSON-LD consumers. ``Team`` aligns with
# ``schema.org/Organization`` because it is a schema.org subset of
# Organization (per the ADR), and ``File`` aligns with
# ``schema.org/MediaObject`` for the same reason.
#
# These dicts are the single source of truth for Phase 1 alignment
# population. Callers route through :func:`schema_alignment_for_entity_type`
# / :func:`schema_alignment_for_edge_kind` rather than reading the
# constants directly so future Phase 4 RDF/JSON-LD export tooling can
# layer on without touching every callsite.

_ENTITY_SCHEMA_ALIGNMENT: Final[dict[str, str]] = {
    PERSON: "schema.org/Person",
    ORGANIZATION: "schema.org/Organization",
    TEAM: "schema.org/Organization",
    SOFTWARE_APPLICATION: "schema.org/SoftwareApplication",
    DATASET: "schema.org/Dataset",
    CREATIVE_WORK: "schema.org/CreativeWork",
    PRODUCT: "schema.org/Product",
    EVENT: "schema.org/Event",
    PLACE: "schema.org/Place",
    FILE: "schema.org/MediaObject",
    AGENT: "prov:Agent",
    ACTIVITY: "prov:Activity",
}

_EDGE_SCHEMA_ALIGNMENT: Final[dict[str, str]] = {
    USED: "prov:used",
    WAS_GENERATED_BY: "prov:wasGeneratedBy",
    WAS_INFORMED_BY: "prov:wasInformedBy",
    WAS_DERIVED_FROM: "prov:wasDerivedFrom",
    WAS_ATTRIBUTED_TO: "prov:wasAttributedTo",
    WAS_ASSOCIATED_WITH: "prov:wasAssociatedWith",
    PART_OF: "schema.org/isPartOf",
    RELATED_TO: "schema.org/relatedTo",
}

# Reverse map of ``ENTITY_TYPE_ALIASES`` — given a canonical name, list
# every legacy alias that resolves to it. Used at retrieval time to
# expand a query for a canonical type into the union of canonical +
# aliases so legacy data still buckets together (Phase 2).
ENTITY_TYPE_ALIAS_INVERSE: Final[dict[str, frozenset[str]]] = {
    canonical: frozenset(
        legacy for legacy, target in ENTITY_TYPE_ALIASES.items() if target == canonical
    )
    for canonical in CANONICAL_ENTITY_TYPES
}

EDGE_KIND_ALIAS_INVERSE: Final[dict[str, frozenset[str]]] = {
    canonical: frozenset(
        legacy for legacy, target in EDGE_KIND_ALIASES.items() if target == canonical
    )
    for canonical in CANONICAL_EDGE_KINDS
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def canonicalize_entity_type(value: str) -> str:
    """Return the canonical form for an entity type.

    Returns the canonical PascalCase name if *value* is a known legacy
    alias or already canonical. Returns *value* unchanged for any
    unknown string (Trellis supports open-string types).

    Idempotent: applying ``canonicalize_entity_type`` twice yields the
    same result as applying it once.
    """
    if value in CANONICAL_ENTITY_TYPES:
        return value
    return ENTITY_TYPE_ALIASES.get(value, value)


def canonicalize_edge_kind(value: str) -> str:
    """Return the canonical form for an edge kind.

    Same contract as :func:`canonicalize_entity_type`.
    """
    if value in CANONICAL_EDGE_KINDS:
        return value
    return EDGE_KIND_ALIASES.get(value, value)


def is_canonical_entity_type(value: str) -> bool:
    """``True`` iff *value* is a canonical entity type."""
    return value in CANONICAL_ENTITY_TYPES


def is_canonical_edge_kind(value: str) -> bool:
    """``True`` iff *value* is a canonical edge kind."""
    return value in CANONICAL_EDGE_KINDS


def is_known_entity_type(value: str) -> bool:
    """``True`` iff *value* is canonical or a registered legacy alias."""
    return value in CANONICAL_ENTITY_TYPES or value in ENTITY_TYPE_ALIASES


def is_known_edge_kind(value: str) -> bool:
    """``True`` iff *value* is canonical or a registered legacy alias."""
    return value in CANONICAL_EDGE_KINDS or value in EDGE_KIND_ALIASES


def schema_alignment_for_entity_type(value: str) -> str | None:
    """Return the standards URI for *value*, or ``None``.

    Canonicalises *value* first, so ``"person"`` and ``"Person"`` both
    yield ``"schema.org/Person"``. Returns ``None`` for canonical names
    that have no standards alignment (Trellis-specific types like
    ``Project`` / ``Concept``) and for unknown / open-string types
    (Trellis explicitly supports those — emitting a fake URI for
    ``"dbt_model"`` would mislead downstream RDF/JSON-LD consumers).
    """
    return _ENTITY_SCHEMA_ALIGNMENT.get(canonicalize_entity_type(value))


def schema_alignment_for_edge_kind(value: str) -> str | None:
    """Return the standards URI for *value*, or ``None``.

    Same contract as :func:`schema_alignment_for_entity_type`.
    """
    return _EDGE_SCHEMA_ALIGNMENT.get(canonicalize_edge_kind(value))


def expand_entity_type_query(value: str) -> tuple[str, ...]:
    """Return the canonical name plus every legacy alias resolving to it.

    Used by retrieval/analytics code that wants a query for ``"Person"``
    to bucket alongside legacy ``"person"`` rows during the migration
    period (per ADR Phase 2). The result is always non-empty: at
    minimum it contains the input value itself (deduped, canonicalised).

    Unknown / open-string types pass through as a single-element tuple
    — there are no aliases to expand and the storage layer will exact-
    match against whatever the caller had in mind.
    """
    canonical = canonicalize_entity_type(value)
    aliases = ENTITY_TYPE_ALIAS_INVERSE.get(canonical, frozenset())
    return (canonical, *sorted(aliases))


def expand_edge_kind_query(value: str) -> tuple[str, ...]:
    """Return the canonical edge kind plus every legacy alias.

    Same contract as :func:`expand_entity_type_query`.
    """
    canonical = canonicalize_edge_kind(value)
    aliases = EDGE_KIND_ALIAS_INVERSE.get(canonical, frozenset())
    return (canonical, *sorted(aliases))
