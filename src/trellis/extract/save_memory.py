"""Factory for the ``save_memory`` extraction pipeline.

Phase 2 Step 6 decision (see TODO.md): the memory path needs no
dedicated ``SaveMemoryExtractor`` class.  Composition of
:class:`AliasMatchExtractor` (deterministic) and :class:`LLMExtractor`
(LLM) inside a :class:`HybridJSONExtractor` covers the requirement:

* AliasMatch resolves explicit ``@mention`` tokens against existing
  entities — the win where most of the useful signal lives.
* The residue (un-matched text, with any unresolved mentions flagged)
  flows to the LLM stage using :data:`MEMORY_EXTRACTION_V1`, which is
  tuned for short natural-language observations and asks for entity
  mentions only (no edges — the hybrid wrapper handles the
  ``mentions`` edge via AliasMatch).

This module exists so callers get one obvious entry point, not so the
behavior diverges from the building blocks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.extract.alias_match import AliasMatchExtractor, AliasResolver
from trellis.extract.hybrid import HybridJSONExtractor
from trellis.extract.llm import LLMExtractor
from trellis.extract.prompts.extraction import MEMORY_EXTRACTION_V1

if TYPE_CHECKING:
    from trellis.llm.protocol import LLMClient


def build_save_memory_extractor(
    *,
    alias_resolver: AliasResolver,
    llm_client: LLMClient,
    model: str | None = None,
    max_tokens: int = 400,
    confidence_threshold: float = 0.7,
    name: str = "save_memory",
    version: str = "0.1.0",
) -> HybridJSONExtractor:
    """Build the default extractor used by the ``save_memory`` MCP path.

    Returns a :class:`HybridJSONExtractor` that wraps:

    * ``AliasMatchExtractor`` as the deterministic stage.
    * ``LLMExtractor`` with :data:`MEMORY_EXTRACTION_V1` as the LLM
      residue stage.

    Both are tagged ``supported_sources=["save_memory"]`` so the
    dispatcher routes to this hybrid when called with
    ``source_hint="save_memory"``.

    Args:
        alias_resolver: Callable mapping mention strings to entity IDs.
            Injected by the MCP wiring layer (Step 7) so ``extract``
            stays decoupled from ``GraphStore``.
        llm_client: Any ``LLMClient`` implementation (e.g.
            :class:`~trellis.llm.providers.openai.OpenAIClient`).
        model: Optional model override for the LLM stage.  Leave
            ``None`` to use the provider's default — for memory
            extraction that's typically a cheaper, lower-latency model.
        max_tokens: Per-call LLM budget.  Defaults to 400 tokens — most
            memories are 1-3 sentences and don't need more.
        confidence_threshold: Passed through to the hybrid wrapper.
            Defaults to 0.7 — AliasMatch typically returns 1.0 when all
            mentions resolve, so the LLM only fires on partial or empty
            matches.
        name / version: Surfaced on the composite extractor's
            provenance.  The inner extractors carry their own
            names and versions.
    """
    alias = AliasMatchExtractor(
        alias_resolver=alias_resolver,
        supported_sources=["save_memory"],
    )
    llm = LLMExtractor(
        name="llm_memory",
        llm_client=llm_client,
        prompt=MEMORY_EXTRACTION_V1,
        model=model,
        max_tokens=max_tokens,
        supported_sources=["save_memory"],
    )
    return HybridJSONExtractor(
        name=name,
        deterministic=alias,
        llm=llm,
        supported_sources=["save_memory"],
        confidence_threshold=confidence_threshold,
        version=version,
    )
