"""Post-extraction draft policy for the memory path (#299 / #300).

Every entity the memory extractor mints from prose is a *claim*, and two
production defects showed the raw drafts making claims the source never
did:

* **Participants were minted as entities** (#299). A conversation's
  speakers ("You", "Claude", the account holder's name) are the frame of
  the document, not its subject matter — a ``Person`` node per speaker
  per conversation is pure duplication.
* **Modality was flattened** (#300). A conversation *evaluating* devices
  produced ``Device`` nodes indistinguishable from devices the owner
  actually has. A bare node in a personal memory graph reads as "exists
  in my world"; extraction cannot honestly assert that.

:func:`apply_memory_draft_policy` is the one gate both memory-extraction
call sites (the CLI ingest hook and the MCP ``save_memory`` path) run an
:class:`~trellis.schemas.extraction.ExtractionResult` through before
:func:`~trellis.extract.commands.result_to_batch`:

1. **Participant filter** — person-typed drafts whose name matches a
   conversation participant are dropped, along with any edge that
   referenced them (both halves go, or neither — the same rule the
   confidence gate applies).
2. **Provenance stamp** — freshly minted drafts (``entity_id is None``)
   get ``document_ids=[doc_id]``, the graph↔document link
   ``EntityCreateHandler`` has accepted since Phase 4 but no extractor
   ever supplied.
3. **Claim floor** — the same fresh mints are stamped
   ``extraction_status="unconfirmed"`` and ``epistemic_status="mentioned"``
   so retrieval can gate them (``GraphSearch`` excludes unconfirmed
   nodes by default) and so the strongest claim a node carries matches
   the strongest claim extraction can actually make.

Drafts that reference an *existing* node (``entity_id`` set — the
alias-match document anchor, resolver hits) are passed through
untouched: stamping those would open a new SCD-2 version that downgrades
a confirmed entity back to unconfirmed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from trellis.schemas.extraction import (
    EPISTEMIC_STATUS_MENTIONED,
    EPISTEMIC_STATUS_PROPERTY,
    EXTRACTION_STATUS_PROPERTY,
    EXTRACTION_STATUS_UNCONFIRMED,
    ExtractionResult,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.schemas.extraction import EdgeDraft, EntityDraft

logger = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_PARTICIPANT_LABELS",
    "apply_memory_draft_policy",
]

#: Speaker labels every memory source can produce, lowercased. Covers the
#: turn labels the conversation reader renders (``**You:**`` /
#: ``**Claude:**`` from ``_SPEAKER_LABELS``, ``"Unknown"`` for
#: unrecognised senders) plus the generic role words an LLM reaches for
#: when it names a speaker instead. Callers extend this per run via
#: ``participant_names`` — e.g. sender labels a non-Claude export
#: renders, or the account holder's display name when the export carries
#: one.
DEFAULT_PARTICIPANT_LABELS = frozenset(
    {
        "you",
        "claude",
        "assistant",
        "user",
        "human",
        "model",
        "system",
        "me",
        "unknown",
    }
)

#: Entity types the participant filter applies to, lowercased. Only
#: person-shaped drafts are filtered: ``Software: Claude`` (the product)
#: is a legitimate entity; ``Person: Claude`` (the speaker) is not.
_PERSON_TYPES = frozenset({"person", "people"})


def apply_memory_draft_policy(
    result: ExtractionResult,
    *,
    doc_id: str,
    participant_names: Iterable[str] = (),
) -> ExtractionResult:
    """Filter participant drafts and stamp provenance + claim floor.

    Args:
        result: The extractor's raw output.
        doc_id: The document the text came from — becomes the
            ``document_ids`` link on every freshly minted entity.
        participant_names: Extra speaker names for this run, merged with
            :data:`DEFAULT_PARTICIPANT_LABELS` (case-insensitive).

    Returns:
        A new :class:`ExtractionResult`; the input is not mutated.
    """
    participants = DEFAULT_PARTICIPANT_LABELS | {
        name.strip().lower() for name in participant_names if name and name.strip()
    }

    kept: list[EntityDraft] = []
    dropped_names: list[str] = []
    dropped_keys: set[str] = set()
    for entity in result.entities:
        if _is_participant_draft(entity, participants):
            dropped_names.append(entity.name)
            dropped_keys.add(entity.name)
            if entity.entity_id is not None:
                dropped_keys.add(entity.entity_id)
            continue
        kept.append(_stamp_fresh_mint(entity, doc_id))

    edges: list[EdgeDraft] = [
        edge
        for edge in result.edges
        if edge.source_id not in dropped_keys and edge.target_id not in dropped_keys
    ]
    kept_entity_keys = {(entity.entity_type, entity.name) for entity in kept}
    judged_drafts = [
        record
        for record in result.judged_drafts
        if (record.entity_type, record.name) in kept_entity_keys
    ]

    if dropped_names:
        logger.info(
            "memory_draft_policy_dropped_participants",
            doc_id=doc_id,
            dropped=sorted(dropped_names),
            dropped_edges=len(result.edges) - len(edges),
        )

    return result.model_copy(
        update={"entities": kept, "edges": edges, "judged_drafts": judged_drafts}
    )


def _is_participant_draft(
    entity: EntityDraft, participants: frozenset[str] | set[str]
) -> bool:
    """True when the draft is a person-typed mention of a speaker."""
    if entity.entity_type.strip().lower() not in _PERSON_TYPES:
        return False
    return entity.name.strip().lower() in participants


def _stamp_fresh_mint(entity: EntityDraft, doc_id: str) -> EntityDraft:
    """Attach provenance + claim floor to a freshly minted draft.

    Drafts carrying an ``entity_id`` reference an existing node and pass
    through untouched — an explicit ``document_ids`` replaces the stored
    link and a status stamp would downgrade a confirmed entity, so both
    stay omission-semantics for those.
    """
    if entity.entity_id is not None:
        return entity

    properties = dict(entity.properties)
    properties.setdefault(EXTRACTION_STATUS_PROPERTY, EXTRACTION_STATUS_UNCONFIRMED)
    properties.setdefault(EPISTEMIC_STATUS_PROPERTY, EPISTEMIC_STATUS_MENTIONED)

    update: dict[str, object] = {"properties": properties}
    if entity.document_ids is None:
        update["document_ids"] = [doc_id]
    return entity.model_copy(update=update)
