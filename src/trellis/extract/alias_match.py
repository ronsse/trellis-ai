"""AliasMatchExtractor — deterministic alias resolution for free-text memories.

Targets the ``save_memory`` path.  Scans raw text for mention tokens
(``@alice`` by default), asks an injected alias resolver for matching
entity IDs, and emits ``mentions`` edges from the memory document to
each unambiguously resolved entity.

Design notes
------------

* **No ``EntityDraft`` output for matches.**  The resolver returns IDs
  of entities that already exist.  Emitting ``EntityDraft`` records for
  them would risk overwriting real entity metadata with the sentinel
  ``entity_type="unknown"`` — instead we rely on the existing entity
  and emit only the edge.  If a mention resolves to nothing, the
  extractor does **not** invent an entity; the unresolved text falls
  through to ``unparsed_residue`` for an LLM-tier extractor downstream.

* **Ambiguous mentions skip silently.**  When the resolver returns more
  than one candidate ID, the extractor refuses to guess and treats the
  mention as unresolved (added to residue).  Guessing would break the
  deterministic contract; an LLM-tier fallback can disambiguate using
  surrounding context.

* **Alias resolution is fully injected.**  The ``Callable[[str], list[str]]``
  signature keeps ``trellis.extract`` decoupled from ``GraphStore``.
  The MCP wiring layer (step 7 of the Phase 2 plan) provides the
  concrete hookup that translates mention strings to entity IDs.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from trellis.extract.base import ExtractorTier
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext


# Default mention pattern: ``@`` followed by word characters, hyphen, or
# underscore.  Captures the alias string without the leading ``@``.
_DEFAULT_MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_\-]+)")


AliasResolver = Callable[[str], list[str]]
"""Function that maps a mention string to zero-or-more entity IDs.

Returns an empty list for no match, a single-element list for an
unambiguous match, and multiple elements when the mention is ambiguous.
The extractor treats anything other than a single-element return as
unresolved.
"""


class AliasMatchExtractor:
    """Deterministic alias-based mention extraction.

    Tier: :attr:`ExtractorTier.DETERMINISTIC`.  Safe to compose inside
    a :class:`HybridJSONExtractor`-style wrapper where an LLM-tier
    extractor handles the residue.
    """

    tier = ExtractorTier.DETERMINISTIC

    def __init__(
        self,
        name: str = "alias_match",
        *,
        alias_resolver: AliasResolver,
        mention_pattern: re.Pattern[str] | None = None,
        edge_kind: str = "mentions",
        supported_sources: list[str] | None = None,
        version: str = "0.1.0",
    ) -> None:
        self.name = name
        self._resolver = alias_resolver
        self._pattern = mention_pattern or _DEFAULT_MENTION_PATTERN
        self._edge_kind = edge_kind
        self.supported_sources = list(supported_sources or ["save_memory"])
        self.version = version

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        del context  # deterministic — no cost budget

        text, doc_id = _parse_input(raw_input)

        mentions = [m.group(1) for m in self._pattern.finditer(text)]

        entities: list[EntityDraft] = []  # AliasMatch never creates
        edges: list[EdgeDraft] = []
        unmatched: list[str] = []
        matched_count = 0
        seen_targets: set[str] = set()

        for candidate in mentions:
            resolved = self._resolver(candidate)
            if len(resolved) != 1:
                # 0 = no match, >1 = ambiguous — both go to residue.
                unmatched.append(candidate)
                continue
            entity_id = resolved[0]
            matched_count += 1
            if doc_id is None:
                # Without a source doc, we have no edge to emit; the
                # match is still "counted" for confidence purposes but
                # produces no draft.
                continue
            if entity_id in seen_targets:
                continue
            seen_targets.add(entity_id)
            edges.append(
                EdgeDraft(
                    source_id=doc_id,
                    target_id=entity_id,
                    edge_kind=self._edge_kind,
                    confidence=1.0,
                )
            )

        overall_confidence, residue = _summarize(
            text=text,
            matched_count=matched_count,
            unmatched=unmatched,
            total_mentions=len(mentions),
        )

        return ExtractionResult(
            entities=entities,
            edges=edges,
            extractor_used=self.name,
            tier=self.tier.value,
            overall_confidence=overall_confidence,
            unparsed_residue=residue,
            provenance=ExtractionProvenance(
                extractor_name=self.name,
                extractor_version=self.version,
                source_hint=source_hint,
            ),
        )


# ----------------------------------------------------------------------
# Input + residue helpers
# ----------------------------------------------------------------------


def _parse_input(raw_input: Any) -> tuple[str, str | None]:
    """Normalize ``raw_input`` into ``(text, doc_id)``.

    Accepts either a plain string (no source document) or a dict with
    ``text`` and optional ``doc_id`` keys.  Any other shape raises
    ``TypeError`` — matching the explicit-failure convention of the
    other extractors.
    """
    if isinstance(raw_input, str):
        return raw_input, None
    if isinstance(raw_input, dict):
        text_value = raw_input.get("text", "")
        if not isinstance(text_value, str):
            msg = (
                "AliasMatchExtractor: dict input requires a string 'text' "
                f"field; got {type(text_value).__name__}"
            )
            raise TypeError(msg)
        doc_id_value = raw_input.get("doc_id")
        doc_id: str | None
        if doc_id_value is None:
            doc_id = None
        elif isinstance(doc_id_value, str):
            doc_id = doc_id_value
        else:
            msg = (
                "AliasMatchExtractor: 'doc_id' must be a string or None; "
                f"got {type(doc_id_value).__name__}"
            )
            raise TypeError(msg)
        return text_value, doc_id
    msg = (
        "AliasMatchExtractor expects a str or dict with 'text' / 'doc_id' "
        f"keys; got {type(raw_input).__name__}"
    )
    raise TypeError(msg)


def _summarize(
    *,
    text: str,
    matched_count: int,
    unmatched: list[str],
    total_mentions: int,
) -> tuple[float, Any | None]:
    """Compute ``(overall_confidence, unparsed_residue)`` for the result.

    * **No mentions found at all** → confidence 0.0, residue = full text.
      Signals to the hybrid wrapper that the LLM tier should process
      the whole input.
    * **All mentions resolved** → confidence 1.0, residue = None.  The
      deterministic extractor handled it cleanly.
    * **Partial resolution** → confidence 0.5, residue = structured
      ``{"text": ..., "unmatched_mentions": [...]}``.  Lets the LLM
      wrapper target the unmatched candidates while keeping full
      surrounding context.
    """
    if total_mentions == 0:
        return 0.0, text
    if matched_count > 0 and not unmatched:
        return 1.0, None
    if matched_count > 0 and unmatched:
        return 0.5, {"text": text, "unmatched_mentions": unmatched}
    # matched_count == 0 but we *did* find mention tokens — all were
    # unresolved (either unknown or ambiguous).
    return 0.0, {"text": text, "unmatched_mentions": unmatched}
