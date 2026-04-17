"""Tiered extraction pipeline — raw input -> entity/edge drafts.

The ``trellis.extract`` package defines a generic, domain-agnostic
extraction layer that sits *before* the governed mutation pipeline.
Callers hand raw input to :class:`ExtractionDispatcher`, which routes to
the right :class:`Extractor` and returns an
:class:`~trellis.schemas.extraction.ExtractionResult`.  The result's
:class:`~trellis.schemas.extraction.EntityDraft` and
:class:`~trellis.schemas.extraction.EdgeDraft` records are then converted
to :class:`~trellis.mutate.commands.Command` objects and executed through
:class:`~trellis.mutate.executor.MutationExecutor`.

Only generic building blocks live here.  Source-specific extractors (dbt
manifests, OpenLineage events, ...) live in ``trellis_workers.extract``
and serve as reference implementations for consumer packages.
"""

from trellis.extract.alias_match import AliasMatchExtractor, AliasResolver
from trellis.extract.base import Extractor, ExtractorTier, NoExtractorAvailableError
from trellis.extract.context import ExtractionContext
from trellis.extract.dispatcher import ExtractionDispatcher
from trellis.extract.hybrid import HybridJSONExtractor, ResidueSelector
from trellis.extract.json_rules import (
    EdgeRule,
    EntityRule,
    ExtractionRuleBundle,
    JSONRulesExtractor,
)
from trellis.extract.llm import LLMExtractor
from trellis.extract.registry import ExtractorRegistry
from trellis.extract.save_memory import build_save_memory_extractor

__all__ = [
    "AliasMatchExtractor",
    "AliasResolver",
    "EdgeRule",
    "EntityRule",
    "ExtractionContext",
    "ExtractionDispatcher",
    "ExtractionRuleBundle",
    "Extractor",
    "ExtractorRegistry",
    "ExtractorTier",
    "HybridJSONExtractor",
    "JSONRulesExtractor",
    "LLMExtractor",
    "NoExtractorAvailableError",
    "ResidueSelector",
    "build_save_memory_extractor",
]
