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

    - STRUCTURAL: fine-grained, machine-generated plumbing that is regenerated
      from source (e.g., columns, function parameters). Excluded from
      retrieval by default — surfaced only as part of their parent's context.
    - SEMANTIC (default): represents a real thing in the world, ingested with
      a source-of-truth. Standard retrieval and standalone-discoverable.
    - CURATED: synthesized/derived from the graph itself (e.g., precedents,
      community clusters, domain rollups). Carries a ``generation_spec``
      describing how it was produced and can be regenerated.

    See ``docs/agent-guide/modeling-guide.md`` for the full three-role
    taxonomy and guidance on when to use each role.
    """

    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    CURATED = "curated"


class EntityType(StrEnum):
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
