"""Extractor Protocol and tier enum for the tiered extraction pipeline.

An *extractor* turns raw input (JSON, text, database rows, ...) into
``ExtractionResult`` objects containing :class:`EntityDraft` and
:class:`EdgeDraft` records.  Extractors never write to stores — the
``ExtractionDispatcher`` and downstream CLI/API layers are responsible for
routing drafts through :class:`MutationExecutor`.

The tier system separates three cost classes:

* :attr:`ExtractorTier.DETERMINISTIC` — zero-LLM, pure parse.  Known sources
  with stable schemas (dbt manifests, OpenLineage events, registered JSON
  rule bundles).
* :attr:`ExtractorTier.HYBRID` — rules handle the easy majority, LLM handles
  ambiguous residue.
* :attr:`ExtractorTier.LLM` — LLM-driven, used for unstructured or
  exploratory input.

See [`adr-llm-client-abstraction.md`](../../docs/design/adr-llm-client-abstraction.md)
for the LLM protocol extractors use, and the Tiered Extraction Pipeline
section of ``TODO.md`` for the graduation path from LLM → Hybrid →
Deterministic.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext
    from trellis.schemas.extraction import ExtractionResult


class ExtractorTier(StrEnum):
    """Cost tier for an extractor.

    Used by :class:`ExtractionDispatcher` for routing priority and by
    effectiveness analysis for graduation tracking (LLM → Hybrid →
    Deterministic as domains stabilize).
    """

    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"
    LLM = "llm"


@runtime_checkable
class Extractor(Protocol):
    """Protocol for all extractors.

    Implementations expose static metadata (``name``, ``tier``,
    ``supported_sources``, ``version``) used by the dispatcher for routing
    and by telemetry for graduation tracking.
    """

    name: str
    tier: ExtractorTier
    supported_sources: list[str]
    version: str

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        """Extract entities and edges from ``raw_input``.

        Args:
            raw_input: Domain-specific raw data.  The dispatcher passes
                through whatever the caller provided — the extractor owns
                input validation.
            source_hint: Routing hint from the caller (e.g.
                ``"dbt-manifest"``).  Extractors may use this to choose
                sub-rules; the dispatcher has already used it for routing
                by the time ``extract`` is called.
            context: Per-call preferences (budget, tier preference).  May
                be ``None`` — extractors should apply sensible defaults.

        Returns:
            :class:`ExtractionResult` carrying drafts, cost accounting,
            and provenance.  Must not raise for recoverable parse errors
            — surface them via ``unparsed_residue`` or reduced
            ``overall_confidence`` instead.
        """


class NoExtractorAvailableError(Exception):
    """Raised by the dispatcher when no extractor can handle an input.

    Explicit failure (vs. silent passthrough) so cold-start gaps for new
    sources are visible immediately.
    """

    def __init__(self, source_hint: str | None, reason: str) -> None:
        self.source_hint = source_hint
        self.reason = reason
        super().__init__(
            f"No extractor available for source_hint={source_hint!r}: {reason}"
        )
