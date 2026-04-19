"""Translation functions: core ↔ wire.

Wire enums carry the same string values as core enums (enforced by a
parity test — see ``tests/unit/wire/test_parity.py``), so translation
reduces to ``WireEnum(core.value)`` and vice versa.  Helpers are still
named explicitly so call sites read clearly and so future non-trivial
translations have an obvious home.

No DTO-level translators exist yet: routes currently consume wire DTOs
directly and convert to core :class:`Command` / :class:`Entity` /
:class:`Edge` objects at the handler boundary.  Add DTO translators
here when a route needs the same conversion twice.
"""

from __future__ import annotations

from trellis import mutate  # re-export surface for BatchStrategy
from trellis.schemas import enums as core_enums
from trellis.schemas import extraction as core_extraction
from trellis_wire import enums as wire_enums
from trellis_wire import extract as wire_extract


def batch_strategy_to_core(wire: wire_enums.BatchStrategy) -> mutate.BatchStrategy:
    """Wire :class:`BatchStrategy` → core :class:`BatchStrategy`.

    Trivial value-copy; values are kept identical by a parity test.
    """
    return mutate.BatchStrategy(wire.value)


def batch_strategy_to_wire(core: mutate.BatchStrategy) -> wire_enums.BatchStrategy:
    """Core :class:`BatchStrategy` → wire :class:`BatchStrategy`."""
    return wire_enums.BatchStrategy(core.value)


def node_role_to_core(wire: wire_enums.NodeRole) -> core_enums.NodeRole:
    """Wire :class:`NodeRole` → core :class:`NodeRole`."""
    return core_enums.NodeRole(wire.value)


def node_role_to_wire(core: core_enums.NodeRole) -> wire_enums.NodeRole:
    """Core :class:`NodeRole` → wire :class:`NodeRole`."""
    return wire_enums.NodeRole(core.value)


# ---------------------------------------------------------------------------
# Extraction drafts (wire → core)
# ---------------------------------------------------------------------------
#
# Wire drafts deliberately omit ``generation_spec`` (curated-node provenance
# flows through /api/v1/entities, not the extraction path).  The translators
# below copy fields 1:1 and never fabricate a spec.  Wire drafts retain the
# client's self-reported confidence — downstream effectiveness analysis
# combines it with classifier scores.


def entity_draft_to_core(wire: wire_extract.EntityDraft) -> core_extraction.EntityDraft:
    """Wire :class:`EntityDraft` → core :class:`EntityDraft`.

    Direct field copy.  ``node_role`` is translated through the enum
    helper so the mapping stays auditable.
    """
    return core_extraction.EntityDraft(
        entity_id=wire.entity_id,
        entity_type=wire.entity_type,
        name=wire.name,
        properties=dict(wire.properties),
        node_role=node_role_to_core(wire.node_role),
        generation_spec=None,  # client-side extractors don't emit curated provenance
        confidence=wire.confidence,
    )


def edge_draft_to_core(wire: wire_extract.EdgeDraft) -> core_extraction.EdgeDraft:
    """Wire :class:`EdgeDraft` → core :class:`EdgeDraft`."""
    return core_extraction.EdgeDraft(
        source_id=wire.source_id,
        target_id=wire.target_id,
        edge_kind=wire.edge_kind,
        properties=dict(wire.properties),
        confidence=wire.confidence,
    )


def extraction_batch_to_core_result(
    batch: wire_extract.ExtractionBatch,
) -> core_extraction.ExtractionResult:
    """Wire :class:`ExtractionBatch` → core :class:`ExtractionResult`.

    Used by the ``POST /api/v1/extract/drafts`` route to funnel
    client-submitted drafts through the same
    :func:`trellis.extract.commands.result_to_batch` bridge as
    server-side extractors.  That keeps the "drafts never touch a
    store directly" invariant intact with exactly one enforcement
    point.

    The wire batch doesn't carry LLM cost accounting (clients
    self-report tier, not token counts) so ``llm_calls`` and
    ``tokens_used`` default to ``0``.  Effectiveness analysis
    can distinguish client-submitted extractions from server-side
    ones by looking at ``provenance.source_hint``, which is set to
    ``batch.source`` here.
    """
    provenance = core_extraction.ExtractionProvenance(
        extractor_name=batch.extractor_name,
        extractor_version=batch.extractor_version,
        source_hint=batch.source,
    )
    return core_extraction.ExtractionResult(
        entities=[entity_draft_to_core(e) for e in batch.entities],
        edges=[edge_draft_to_core(e) for e in batch.edges],
        extractor_used=f"{batch.extractor_name}@{batch.extractor_version}",
        tier=batch.tier.value,
        llm_calls=0,
        tokens_used=0,
        overall_confidence=1.0,
        provenance=provenance,
        unparsed_residue=None,
    )


__all__ = [
    "batch_strategy_to_core",
    "batch_strategy_to_wire",
    "edge_draft_to_core",
    "entity_draft_to_core",
    "extraction_batch_to_core_result",
    "node_role_to_core",
    "node_role_to_wire",
]
