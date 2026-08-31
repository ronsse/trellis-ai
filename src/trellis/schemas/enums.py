"""Shared enums for Trellis schemas."""

from __future__ import annotations

from enum import StrEnum


class TraceSource(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    WORKFLOW = "workflow"
    SYSTEM = "system"


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class NodeRole(StrEnum):
    """Graph-invariant role distinguishing the three kinds of nodes.

    - STRUCTURAL: machine-generated plumbing that is not standalone memory
      and is reproducible from something outside this graph. Two shapes
      qualify: rows *regenerated from source* (columns, function parameters,
      the ``tool:<slug>`` nodes trace extraction mints), and rows that
      *restate an Operational-Plane fact* (the per-invocation ``Activity``
      the meta-recorder writes, which the event log and trace store already
      hold). Excluded from retrieval by default — surfaced only as part of
      their parent's context, or via ``include_structural=True``.
    - SEMANTIC (default): represents a real thing in the world, ingested with
      a source-of-truth. Standard retrieval and standalone-discoverable.
    - CURATED: synthesized/derived from the graph itself (e.g., precedents,
      community clusters, domain rollups). Carries a ``generation_spec``
      describing how it was produced and can be regenerated.

    **On the STRUCTURAL wording** (#375). It used to read "regenerated from
    source (e.g., columns, function parameters)", which named one *example*
    of a non-standalone machine-generated row and read as the definition.
    The meta-recorder's per-invocation ``Activity`` is the same kind of thing
    — never standalone memory, reconstructible from the operational plane —
    but is not regenerated from a source system, so the old wording excluded
    it by accident. The behavioural contract is a single bit ("is this row
    standalone-discoverable?"); both cases want that bit; and a fourth role
    would oblige every backend, filter, doc and consumer to learn a value
    whose retrieval semantics are identical to this one. So the definition is
    restated at the altitude the behaviour already sits at, rather than a
    role being added or a case being wedged in.

    The risk taken is the #325 / #326 one — a key widened to fit a new case
    until two writers mean different things by it. What bounds it here:
    ``node_role`` is a closed three-value enum with one write path
    (``upsert_node``), it is **immutable across SCD-2 versions**, and every
    consumer branches on the same predicate (``!= "structural"``). There is
    no second vocabulary for the two readings to drift apart into.

    See ``docs/agent-guide/modeling-guide.md`` for the full three-role
    taxonomy and guidance on when to use each role.
    """

    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    CURATED = "curated"


class EntityType(StrEnum):
    """Legacy entity-type registry — kept for back-compat.

    New code should prefer the canonical schema.org-aligned constants in
    :mod:`trellis.schemas.well_known` (``Person``, ``Organization``,
    ``SoftwareApplication``, ``Dataset``, ``CreativeWork``, ``Product``,
    ``Event``, ``Place``, plus PROV-O's ``Agent`` and ``Activity``). The
    lowercase values here remain permanent aliases — every value in
    this enum maps to a canonical via
    :func:`trellis.schemas.well_known.canonicalize_entity_type`, except
    ``DOMAIN`` which is intentionally dropped from the canonical
    defaults (collides with ``ContentTags.domain``).

    See ``docs/design/adr-graph-ontology.md`` for the full decision.
    """

    PERSON = "person"
    SYSTEM = "system"
    SERVICE = "service"
    TEAM = "team"
    DOCUMENT = "document"
    CONCEPT = "concept"
    DOMAIN = "domain"
    FILE = "file"
    PROJECT = "project"
    TOOL = "tool"


class EvidenceType(StrEnum):
    DOCUMENT = "document"
    SNIPPET = "snippet"
    LINK = "link"
    CONFIG = "config"
    IMAGE = "image"
    FILE_POINTER = "file_pointer"


class PolicyType(StrEnum):
    MUTATION = "mutation"
    ACCESS = "access"
    RETENTION = "retention"
    REDACTION = "redaction"


class Enforcement(StrEnum):
    ENFORCE = "enforce"
    WARN = "warn"
    AUDIT_ONLY = "audit_only"


class EdgeKind(StrEnum):
    """Legacy edge-kind registry — kept for back-compat.

    New code should prefer the canonical PROV-O verbs in
    :mod:`trellis.schemas.well_known` (``used``, ``wasGeneratedBy``,
    ``wasInformedBy``, ``wasDerivedFrom``, ``wasAttributedTo``,
    ``wasAssociatedWith``, plus ``partOf`` / ``dependsOn`` / ``relatedTo``
    and the Trellis-specific ``attachedTo`` / ``supports`` /
    ``appliesTo``). Every value in this enum maps to a canonical via
    :func:`trellis.schemas.well_known.canonicalize_edge_kind`.

    See ``docs/design/adr-graph-ontology.md`` for the full decision.
    """

    # Trace relationships
    TRACE_USED_EVIDENCE = "trace_used_evidence"
    TRACE_PRODUCED_ARTIFACT = "trace_produced_artifact"
    TRACE_TOUCHED_ENTITY = "trace_touched_entity"
    TRACE_PROMOTED_TO_PRECEDENT = "trace_promoted_to_precedent"
    # Entity relationships
    ENTITY_RELATED_TO = "entity_related_to"
    ENTITY_PART_OF = "entity_part_of"
    ENTITY_DEPENDS_ON = "entity_depends_on"
    # Evidence relationships
    EVIDENCE_ATTACHED_TO = "evidence_attached_to"
    EVIDENCE_SUPPORTS = "evidence_supports"
    # Precedent relationships
    PRECEDENT_APPLIES_TO = "precedent_applies_to"
    PRECEDENT_DERIVED_FROM = "precedent_derived_from"
