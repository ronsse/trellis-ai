"""ExtractionContext — per-call knobs for dispatcher + extractors.

Carries the caller's preferences and cost budget across an extraction run.
Extractors read the context to decide how aggressively to spend LLM calls;
the dispatcher reads it to decide whether LLM-tier extractors are
eligible.
"""

from __future__ import annotations

from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.extract.base import ExtractorTier


class ExtractionContext(TrellisModel):
    """Per-call configuration for a single extraction run.

    Defaults are conservative: ``allow_llm_fallback=False`` means the
    dispatcher will never route to an LLM-tier extractor unless the caller
    explicitly opts in.  Use ``prefer_tier`` to force a specific tier (for
    A/B comparisons or testing).
    """

    allow_llm_fallback: bool = False
    max_llm_calls: int = Field(default=5, ge=0)
    max_tokens: int = Field(default=8000, ge=0)
    prefer_tier: ExtractorTier | None = None
    domain: str | None = None
    source_system: str | None = None
