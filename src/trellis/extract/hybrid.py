"""HybridJSONExtractor — deterministic-first, LLM-for-residue.

Wraps any two :class:`Extractor` instances into a tier=HYBRID extractor
that runs the deterministic half first and only falls through to the
LLM half for residue.  This is the default shape for sources that
have *some* stable rules (so we want the deterministic win) but
benefit from LLM coverage on the ambiguous remainder.

Design decisions
----------------

* **Deterministic-first is load-bearing.**  The wrapper never calls
  the LLM stage if the deterministic extractor fully covers the input
  (``overall_confidence >= threshold`` **and** ``unparsed_residue is
  None``).  This preserves the "LLM is opt-in residue handling, never
  silent substitution" rule from the Phase 2 plan.

* **Budget gates are explicit.**  Because HYBRID tier isn't gated by
  ``allow_llm_fallback`` at the dispatcher, the wrapper checks it
  itself before firing the LLM stage.  ``max_llm_calls=0`` and
  ``allow_llm_fallback=False`` both cause a graceful fall-back:
  the deterministic result is returned with a structlog warning, not
  a silent drop.

* **Deterministic wins in merges.**  When the same
  ``(entity_type, entity_id|name)`` or ``(source_id, target_id,
  edge_kind)`` key appears in both stages, the deterministic draft is
  kept.  Confidence on merged results is the **minimum** of the two
  stage confidences — a hybrid is only as trustworthy as its weakest
  contributing stage.

* **Provenance reflects the composition.**  ``extractor_used`` and
  ``ExtractionProvenance.extractor_name`` both become
  ``"hybrid(<det>+<llm>)"`` so effectiveness analysis can attribute
  output to the right pair.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from trellis.extract.base import ExtractorTier
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)

if TYPE_CHECKING:
    from trellis.extract.base import Extractor
    from trellis.extract.context import ExtractionContext

logger = structlog.get_logger(__name__)


ResidueSelector = Callable[[Any, ExtractionResult], Any]
"""``(raw_input, deterministic_result) -> raw_input_for_llm``.

Callers override the default when their residue shape needs
transformation before the LLM stage sees it (e.g. dropping already-
matched spans).  The default implementation is fine for ``str`` and
dict-shaped inputs — see :func:`_default_residue_selector`.
"""


class HybridJSONExtractor:
    """Compose a deterministic and an LLM extractor as a single HYBRID.

    The two inner extractors don't have to be strictly DETERMINISTIC /
    LLM at the enum level — a HYBRID of two HYBRIDs is legal — but the
    wrapper is named and tuned for the common case of ``rules + LLM``.
    """

    tier = ExtractorTier.HYBRID

    def __init__(
        self,
        name: str = "hybrid",
        *,
        deterministic: Extractor,
        llm: Extractor,
        supported_sources: list[str] | None = None,
        residue_selector: ResidueSelector | None = None,
        confidence_threshold: float = 0.7,
        version: str = "0.1.0",
    ) -> None:
        self.name = name
        self._deterministic = deterministic
        self._llm = llm
        self.supported_sources = list(supported_sources or [])
        self._residue_selector = residue_selector or _default_residue_selector
        self._confidence_threshold = confidence_threshold
        self.version = version

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        det_result = await self._deterministic.extract(
            raw_input, source_hint=source_hint, context=context
        )

        # Full deterministic win — skip the LLM stage entirely.
        if (
            det_result.overall_confidence >= self._confidence_threshold
            and det_result.unparsed_residue is None
        ):
            return _rewrap(det_result, self._composite_name(), self.version, self.tier)

        # Budget / policy gates.  HYBRID tier isn't gated by
        # allow_llm_fallback at the dispatcher, so we enforce it here.
        if not self._llm_allowed(context):
            logger.warning(
                "hybrid_llm_stage_skipped",
                extractor=self.name,
                reason=self._llm_skip_reason(context),
                det_confidence=det_result.overall_confidence,
            )
            return _rewrap(det_result, self._composite_name(), self.version, self.tier)

        residue_input = self._residue_selector(raw_input, det_result)
        llm_result = await self._llm.extract(
            residue_input, source_hint=source_hint, context=context
        )

        return _merge(
            det_result=det_result,
            llm_result=llm_result,
            composite_name=self._composite_name(),
            version=self.version,
            tier=self.tier,
            source_hint=source_hint,
        )

    def _composite_name(self) -> str:
        return f"hybrid({self._deterministic.name}+{self._llm.name})"

    def _llm_allowed(self, context: ExtractionContext | None) -> bool:
        if context is None:
            # No context means no explicit opt-in — refuse LLM to keep
            # the "LLM is opt-in" posture intact.
            return False
        if not context.allow_llm_fallback:
            return False
        return context.max_llm_calls > 0

    def _llm_skip_reason(self, context: ExtractionContext | None) -> str:
        if context is None:
            return "no ExtractionContext provided (LLM requires explicit opt-in)"
        if not context.allow_llm_fallback:
            return "allow_llm_fallback=False"
        if context.max_llm_calls <= 0:
            return "max_llm_calls<=0"
        return "unknown"


# ----------------------------------------------------------------------
# Residue selection
# ----------------------------------------------------------------------


def _default_residue_selector(
    raw_input: Any,
    det_result: ExtractionResult,
) -> Any:
    """Derive the LLM-stage input from ``raw_input`` and the det result.

    Preserves ``doc_id`` from the original ``raw_input`` when the
    residue itself doesn't carry one — so edges the LLM produces can
    still reference the memory document.
    """
    residue = det_result.unparsed_residue
    doc_id: str | None = None
    if isinstance(raw_input, dict):
        doc_id_raw = raw_input.get("doc_id")
        if isinstance(doc_id_raw, str):
            doc_id = doc_id_raw

    if residue is None:
        # Deterministic claimed full coverage.  Shouldn't normally
        # reach here (the caller short-circuits on confidence+residue),
        # but if confidence alone pushed us past the gate we fall back
        # to re-scanning the original input.
        return raw_input

    if isinstance(residue, str):
        if doc_id is not None:
            return {"doc_id": doc_id, "text": residue}
        return residue

    if isinstance(residue, dict):
        merged = dict(residue)
        if doc_id is not None and "doc_id" not in merged:
            merged["doc_id"] = doc_id
        return merged

    # Unknown residue shape — let the LLM see the original input.
    return raw_input


# ----------------------------------------------------------------------
# Merge
# ----------------------------------------------------------------------


def _merge(
    *,
    det_result: ExtractionResult,
    llm_result: ExtractionResult,
    composite_name: str,
    version: str,
    tier: ExtractorTier,
    source_hint: str | None,
) -> ExtractionResult:
    """Combine deterministic + LLM results into a single HYBRID result.

    Dedup rules:
      * Entities keyed by ``(entity_type, entity_id or name)``;
        deterministic wins on collision.
      * Edges keyed by ``(source_id, target_id, edge_kind)``;
        deterministic wins on collision.

    Confidence is the minimum of the two stage confidences when both
    contributed drafts; otherwise it's the non-zero stage's confidence
    (or ``0.0`` if neither produced anything).
    """
    entities: list[EntityDraft] = []
    seen_entity_keys: set[tuple[str, str]] = set()
    for ent_draft in (*det_result.entities, *llm_result.entities):
        ent_key = (ent_draft.entity_type, ent_draft.entity_id or ent_draft.name)
        if ent_key in seen_entity_keys:
            continue
        seen_entity_keys.add(ent_key)
        entities.append(ent_draft)

    edges: list[EdgeDraft] = []
    seen_edge_keys: set[tuple[str, str, str]] = set()
    for edge_draft in (*det_result.edges, *llm_result.edges):
        edge_key = (edge_draft.source_id, edge_draft.target_id, edge_draft.edge_kind)
        if edge_key in seen_edge_keys:
            continue
        seen_edge_keys.add(edge_key)
        edges.append(edge_draft)

    det_contributed = bool(det_result.entities or det_result.edges)
    llm_contributed = bool(llm_result.entities or llm_result.edges)
    if det_contributed and llm_contributed:
        confidence = min(det_result.overall_confidence, llm_result.overall_confidence)
    elif det_contributed:
        confidence = det_result.overall_confidence
    elif llm_contributed:
        confidence = llm_result.overall_confidence
    else:
        confidence = 0.0

    return ExtractionResult(
        entities=entities,
        edges=edges,
        extractor_used=composite_name,
        tier=tier.value,
        llm_calls=det_result.llm_calls + llm_result.llm_calls,
        tokens_used=det_result.tokens_used + llm_result.tokens_used,
        overall_confidence=confidence,
        unparsed_residue=llm_result.unparsed_residue,
        provenance=ExtractionProvenance(
            extractor_name=composite_name,
            extractor_version=version,
            source_hint=source_hint,
        ),
    )


def _rewrap(
    result: ExtractionResult,
    composite_name: str,
    version: str,
    tier: ExtractorTier,
) -> ExtractionResult:
    """Re-label a deterministic-only result under the hybrid's identity."""
    return ExtractionResult(
        entities=list(result.entities),
        edges=list(result.edges),
        extractor_used=composite_name,
        tier=tier.value,
        llm_calls=result.llm_calls,
        tokens_used=result.tokens_used,
        overall_confidence=result.overall_confidence,
        unparsed_residue=result.unparsed_residue,
        provenance=ExtractionProvenance(
            extractor_name=composite_name,
            extractor_version=version,
            source_hint=result.provenance.source_hint,
        ),
    )
