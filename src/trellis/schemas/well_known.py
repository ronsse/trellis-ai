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

from typing import TYPE_CHECKING, Final

import structlog

from trellis.schemas.enums import NodeRole

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

# ---------------------------------------------------------------------------
# Version of the canonical registry
# ---------------------------------------------------------------------------
#
# Per ``plan-self-improvement-program.md`` §5.6 and
# ``adr-observation-entity-type.md`` §5: adding a new canonical name is
# a **minor** version bump (additive change; names are reserved forever
# per ``adr-graph-ontology.md`` §5.4). Removing or renaming a canonical
# would be a major bump.
#
# Version log:
#   1.0.0 — original schema.org / PROV-O alignment (adr-graph-ontology.md)
#   1.1.0 — add ``Observation`` / ``Measurement`` entity types and
#           ``hasObservation`` edge kind (adr-observation-entity-type.md)
#   1.2.0 — add ``hasMeasurement`` edge kind so Measurement edges no
#           longer overload ``hasObservation`` (adr-observation-entity-
#           type.md §2.2).

WELL_KNOWN_VERSION: Final = "1.2.0"

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

# Empirical-observation classes — see adr-observation-entity-type.md
OBSERVATION: Final = "Observation"
MEASUREMENT: Final = "Measurement"

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
        OBSERVATION,
        MEASUREMENT,
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
    "dataset": DATASET,
    # ``domain`` is intentionally *not* aliased. It is removed from the
    # well-known defaults (per ADR) because it collides with the
    # ContentTags.domain classification facet. Existing data using
    # ``entity_type="domain"`` continues to work as an open string.
}

# ---------------------------------------------------------------------------
# Recommended properties — cross-database routing (Dataset / Table)
# ---------------------------------------------------------------------------
#
# Entities representing queryable datasets — canonical type ``Dataset``
# (plus the lowercase ``"dataset"`` alias and extractor-specific shapes
# like ``dbt_model`` / ``dbt_source``) — SHOULD carry the routing
# properties below so query-engine agents can dispatch queries without
# consulting the prompt or out-of-band config.
#
# These are *recommended convention*, not enforced schema: Trellis entity
# properties are open bags by design (the storage layer accepts anything).
# Extractors that claim Dataset shape populate these when the upstream
# system supplies the information. Consumers read with ``.get(...)`` and
# fall back gracefully when a property is absent — well-modeled metadata
# (Unity Catalog, dbt) fills all of them; ad-hoc sources may fill only
# ``source_system``.
#
# - ``source_system``: short identifier of the data platform
#   (``"snowflake"``, ``"postgres"``, ``"bigquery"``, ``"databricks"``,
#   ``"duckdb"``, etc.). Maps directly to dbt's ``metadata.adapter_type``
#   and to the URI scheme of OpenLineage namespaces.
# - ``connection_ref``: env-var name (or secrets-manager reference)
#   resolving to a connection string or client config. Never inline
#   credentials. Optional — many entities are read-only metadata records
#   that don't need a connection.
# - ``database_name``: physical database / catalog name.
# - ``schema_name``: physical schema / namespace within the database.
#   (Distinct from the dbt ``schema`` property, which historically encodes
#   both physical schema and logical layer convention; both keys coexist.)
# - ``physical_uri``: optional fully-qualified locator, e.g.
#   ``"snowflake://account/db/schema/table"`` or
#   ``"postgres://host:port/db.schema.table"``. Extractors construct this
#   only when the upstream source supplies enough information; agents
#   prefer this over recomposing from the parts.

DATASET_PROP_SOURCE_SYSTEM: Final = "source_system"
DATASET_PROP_CONNECTION_REF: Final = "connection_ref"
DATASET_PROP_DATABASE_NAME: Final = "database_name"
DATASET_PROP_SCHEMA_NAME: Final = "schema_name"
DATASET_PROP_PHYSICAL_URI: Final = "physical_uri"

DATASET_ROUTING_PROPERTIES: Final[frozenset[str]] = frozenset(
    {
        DATASET_PROP_SOURCE_SYSTEM,
        DATASET_PROP_CONNECTION_REF,
        DATASET_PROP_DATABASE_NAME,
        DATASET_PROP_SCHEMA_NAME,
        DATASET_PROP_PHYSICAL_URI,
    }
)


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

# Trellis-specific verbs for empirical observations (no clean PROV-O
# equivalent — schema.org/observationAbout points the wrong direction).
# See adr-observation-entity-type.md §2.2. ``hasMeasurement`` is the
# Measurement counterpart so Measurement edges don't overload
# ``hasObservation`` and consumers can route on edge kind alone.
HAS_OBSERVATION: Final = "hasObservation"
HAS_MEASUREMENT: Final = "hasMeasurement"

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
        HAS_OBSERVATION,
        HAS_MEASUREMENT,
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
    OBSERVATION: "schema.org/Observation",
    MEASUREMENT: "schema.org/PropertyValue",
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


# ---------------------------------------------------------------------------
# Anti-pattern blocklist — soft enforcement (Track G)
# ---------------------------------------------------------------------------
#
# Per ``adr-source-modeling-discipline.md`` (sibling Unit G0 — reference by
# path; do not assume content beyond the policy summarised here), per-column
# entities are an anti-pattern in the Trellis graph: they shred a single
# Dataset / Table into thousands of structural leaves, blow up the node
# count without adding retrieval signal, and force every downstream
# operation (pack assembly, importance scoring, alias matching) to filter
# them out. The recommended shape is a single Dataset / Table entity with
# columns stored as a list in ``properties.columns``.
#
# The blocklist is *soft enforcement*: emitting one of these types logs a
# structlog WARNING that the operator should switch to the properties shape.
# Operators with a genuine need for column-level nodes (e.g., regulated
# PII columns that must carry their own access-control policies and trace
# attachments) can opt in by setting **both** ``allow_structural_leaf=True``
# *and* ``node_role=NodeRole.STRUCTURAL`` on the producing
# :class:`~trellis.extract.json_rules.EntityRule` or
# :class:`~trellis.schemas.extraction.EntityDraft` (two-signal exception,
# adr-source-modeling-discipline.md §2.5). Either signal alone still warns —
# ``allow_structural_leaf=True`` with ``node_role=SEMANTIC`` means the
# operator opted out of the policy but did not declare the column
# structural, which is almost certainly a mistake. The opt-in path emits a
# structlog INFO so audit logs still show the deliberate choice.
#
# This is *advisory only* — the validator never raises and never rejects a
# mutation. ``MutationExecutor`` and policy gates are explicitly out of
# scope (open-string contract preserved per CLAUDE.md). Closing the set
# would break domain integrations and the well-known module documents
# the same openness rationale above.

ENTITY_TYPE_ANTI_PATTERNS: Final[frozenset[str]] = frozenset(
    {
        "Column",
        "column",
        "TableColumn",
        "table_column",
    }
)

# Path reference carried on every anti-pattern WARNING / INFO event so
# operators reading aggregated logs can jump straight to the policy
# (adr-source-modeling-discipline.md §2.3 requires the warning to name
# the governing ADR).
ENTITY_TYPE_ANTI_PATTERN_ADR: Final = "docs/design/adr-source-modeling-discipline.md"


def validate_entity_type_not_anti_pattern(
    entity_type: str,
    *,
    allow_structural_leaf: bool = False,
    node_role: NodeRole | str | None = None,
    logger: BoundLogger | None = None,
) -> None:
    """Warn (or acknowledge) when *entity_type* is on the anti-pattern blocklist.

    Soft enforcement for the per-column anti-pattern documented in
    ``adr-source-modeling-discipline.md``. The check is advisory: this
    function never raises, never rejects, and never mutates the value.

    The regulated-column exception is **two-signal** (ADR §2.5): the
    operator must set ``allow_structural_leaf=True`` *and* declare the
    node ``node_role=STRUCTURAL``. Either signal alone still warns —
    ``allow_structural_leaf=True`` with ``node_role=SEMANTIC`` means the
    operator opted out of the policy but did not declare the column
    structural, which is almost certainly a mistake.

    Behaviour:

    * ``entity_type`` not in :data:`ENTITY_TYPE_ANTI_PATTERNS` — silent.
    * ``entity_type`` in :data:`ENTITY_TYPE_ANTI_PATTERNS` and
      ``allow_structural_leaf=True`` and ``node_role=STRUCTURAL`` —
      emits a ``structlog`` INFO under event
      ``entity_type_anti_pattern_opt_in_acknowledged`` confirming the
      operator opted in deliberately.
    * ``entity_type`` in :data:`ENTITY_TYPE_ANTI_PATTERNS` otherwise
      (no signal, or only one of the two) — emits a ``structlog``
      WARNING under event ``entity_type_anti_pattern_warning`` with a
      ``recommendation`` field pointing the operator at the properties
      shape and the two-signal opt-in.

    Both events carry an ``adr`` field referencing
    :data:`ENTITY_TYPE_ANTI_PATTERN_ADR` plus the offending
    ``entity_type``. Pass ``logger`` to bind structured context (rule
    name, extractor name, batch ID, etc.); when ``None`` a fresh logger
    is fetched via ``structlog.get_logger(__name__)``.
    """
    if entity_type not in ENTITY_TYPE_ANTI_PATTERNS:
        return
    log = logger if logger is not None else structlog.get_logger(__name__)
    role_value = str(node_role) if node_role is not None else None
    if allow_structural_leaf and node_role == NodeRole.STRUCTURAL:
        log.info(
            "entity_type_anti_pattern_opt_in_acknowledged",
            entity_type=entity_type,
            node_role=role_value,
            adr=ENTITY_TYPE_ANTI_PATTERN_ADR,
        )
        return
    log.warning(
        "entity_type_anti_pattern_warning",
        entity_type=entity_type,
        allow_structural_leaf=allow_structural_leaf,
        node_role=role_value,
        adr=ENTITY_TYPE_ANTI_PATTERN_ADR,
        recommendation=(
            "Use a property on the parent entity "
            "(e.g., properties.columns = [...]); "
            "set both allow_structural_leaf=True and "
            "node_role=STRUCTURAL if you genuinely need the "
            "regulated-column exception."
        ),
    )
