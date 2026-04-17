"""Prompt templates for LLM-tier extractors.

Intentionally minimal: plain dataclass + ``str.format`` templating.  The
full "prompt library" with Jinja2 rendering, versioning registry, and
per-call parameter binding is deferred (see
``docs/design/adr-llm-client-abstraction.md`` §2.4) — it becomes worth
building when a fourth extractor or an external consumer needs it.

The scaffolding here covers exactly the two templates Phase 2 needs:

* :data:`ENTITY_EXTRACTION_V1` — generic entity+edge extraction from
  unstructured text, used by ``LLMExtractor``.
* :data:`MEMORY_EXTRACTION_V1` — short, mention-focused extraction for
  the ``save_memory`` path.
"""

from trellis.extract.prompts.base import PromptTemplate, render
from trellis.extract.prompts.extraction import (
    ENTITY_EXTRACTION_V1,
    MEMORY_EXTRACTION_V1,
)

__all__ = [
    "ENTITY_EXTRACTION_V1",
    "MEMORY_EXTRACTION_V1",
    "PromptTemplate",
    "render",
]
