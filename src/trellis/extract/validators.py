"""Extraction validators — deterministic gate at the dispatch boundary.

Closes Logic Gap 1.3. See ``docs/design/adr-extraction-validation.md`` for
the full design.

The :class:`ExtractionValidator` Protocol lets callers plug deterministic,
microsecond-order checks into :class:`~trellis.extract.dispatcher.ExtractionDispatcher`.
When any validator returns at least one :class:`ValidationFinding`, the
dispatcher quarantines the original drafts into
``unparsed_residue["rejected_by_validators"]`` and emits an
``EXTRACTION_REJECTED`` event — no Commands flow downstream, so junk never
lands in stores.

Three default validators ship and cover the deterministic failure shapes
named in the ADR:

* :class:`EmptyResultValidator` — zero entities AND zero edges.
* :class:`OrphanProvenanceValidator` — ``EntityDraft`` with
  ``node_role=curated`` but no ``generation_spec``.
* :class:`DraftLocalReferenceValidator` — ``EdgeDraft`` whose source/target
  references a draft-local id absent from the same batch and the edge is
  not ``allow_dangling``.

Validators are pure: no I/O, no LLM, no network. Domain-specific rules can
be added by implementing the Protocol and passing them via
``ExtractionDispatcher(..., validators=[...])``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from trellis.schemas.enums import NodeRole

if TYPE_CHECKING:
    from trellis.schemas.extraction import ExtractionResult


@dataclass
class ValidationFinding:
    """One finding emitted by one :class:`ExtractionValidator`.

    ``code`` is a short, stable identifier (e.g. ``"empty_result"``) used by
    telemetry aggregation. ``affected`` is an opaque dict — validators can
    stamp draft indexes / ids there for debugging without committing to a
    schema.
    """

    validator_name: str
    code: str
    message: str
    affected: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExtractionValidator(Protocol):
    """Plug-in deterministic check on an :class:`ExtractionResult`.

    Implementations MUST be deterministic, microsecond-order, and free of
    I/O. They run synchronously per dispatch — a slow validator slows every
    extraction.
    """

    name: str

    def validate(
        self,
        result: ExtractionResult,
        *,
        source_hint: str | None = None,
    ) -> list[ValidationFinding]:
        """Inspect the result and return findings (empty list = OK)."""
        ...


# ---------------------------------------------------------------------------
# Default validators
# ---------------------------------------------------------------------------


class EmptyResultValidator:
    """Flag results with zero entities AND zero edges.

    The dispatcher already emits ``EXTRACTOR_FALLBACK { reason="empty_result" }``
    for graduation tracking. This validator emits ``EXTRACTION_REJECTED`` for
    the same shape but in the validation namespace, so retrieval-quality
    analyzers don't have to understand both event types. Mild redundancy for
    clean separation.
    """

    name: str = "empty_result"

    def validate(
        self,
        result: ExtractionResult,
        *,
        source_hint: str | None = None,  # noqa: ARG002 (Protocol contract)
    ) -> list[ValidationFinding]:
        if result.entities or result.edges:
            return []
        return [
            ValidationFinding(
                validator_name=self.name,
                code="empty_result",
                message=(
                    "extractor produced zero entities and zero edges; "
                    "nothing to ingest"
                ),
            )
        ]


class OrphanProvenanceValidator:
    """Flag ``EntityDraft`` with ``node_role=curated`` but no ``generation_spec``.

    The graph store's ``validate_node_role_args`` rejects the same shape at
    handler time, but firing here gives retrieval-quality telemetry visibility
    even when the per-Command rejection happens — the entity was never
    persisted, never tagged.
    """

    name: str = "orphan_provenance"

    def validate(
        self,
        result: ExtractionResult,
        *,
        source_hint: str | None = None,  # noqa: ARG002 (Protocol contract)
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        for idx, draft in enumerate(result.entities):
            if draft.node_role == NodeRole.CURATED and draft.generation_spec is None:
                findings.append(
                    ValidationFinding(
                        validator_name=self.name,
                        code="missing_generation_spec",
                        message=(
                            f"EntityDraft[{idx}] name={draft.name!r} has "
                            "node_role=curated but no generation_spec"
                        ),
                        affected={
                            "draft_index": idx,
                            "name": draft.name,
                            "entity_id": draft.entity_id,
                        },
                    )
                )
        return findings


class DraftLocalReferenceValidator:
    """Flag ``EdgeDraft`` whose endpoints reference unknown draft-local ids.

    Catches the "extractor emitted edges referring to entities it didn't
    emit" case at the extraction boundary, before
    :class:`~trellis.mutate.handlers.LinkCreateHandler` fails the FK check at
    handler time. ``allow_dangling=True`` opts the edge out — that's the
    bootstrap / cross-batch escape hatch.

    "Unknown" here means the id matches no ``EntityDraft.entity_id`` in the
    same result. References that point at already-persisted entities by id
    look unknown to this validator too — keep the validator off (or filter
    the result before validation) when extractors emit cross-batch edges
    without ``allow_dangling``.
    """

    name: str = "orphan_edge"

    def validate(
        self,
        result: ExtractionResult,
        *,
        source_hint: str | None = None,  # noqa: ARG002 (Protocol contract)
    ) -> list[ValidationFinding]:
        # Build the set of draft-local entity ids in this batch. ``None``
        # entity_ids are skipped — the dispatcher / commands layer assigns
        # them at create time, so they can't be referenced from the same
        # batch by name anyway.
        local_ids = {
            draft.entity_id for draft in result.entities if draft.entity_id is not None
        }
        findings: list[ValidationFinding] = []
        for idx, edge in enumerate(result.edges):
            if edge.allow_dangling:
                continue
            missing: list[str] = []
            if edge.source_id not in local_ids:
                missing.append(f"source_id={edge.source_id!r}")
            if edge.target_id not in local_ids:
                missing.append(f"target_id={edge.target_id!r}")
            if missing:
                findings.append(
                    ValidationFinding(
                        validator_name=self.name,
                        code="orphan_edge",
                        message=(
                            f"EdgeDraft[{idx}] edge_kind={edge.edge_kind!r} "
                            f"references unknown draft-local entities: "
                            f"{'; '.join(missing)}"
                        ),
                        affected={
                            "edge_index": idx,
                            "source_id": edge.source_id,
                            "target_id": edge.target_id,
                            "edge_kind": edge.edge_kind,
                        },
                    )
                )
        return findings


def default_validators() -> list[ExtractionValidator]:
    """Build the three default validators.

    Convenience for callers wiring an :class:`ExtractionDispatcher` with the
    blessed defaults — equivalent to constructing each validator inline.
    """
    return [
        EmptyResultValidator(),
        OrphanProvenanceValidator(),
        DraftLocalReferenceValidator(),
    ]


__all__ = [
    "DraftLocalReferenceValidator",
    "EmptyResultValidator",
    "ExtractionValidator",
    "OrphanProvenanceValidator",
    "ValidationFinding",
    "default_validators",
]
