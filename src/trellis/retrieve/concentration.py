"""How much of a pack comes from a single source document.

A long document is stored as one parent row plus N chunk rows
(:mod:`trellis.ingest_corpus.chunker`), and every chunk is independently
retrievable — chunks are the retrievable unit, which is the whole point of
chunking. So one document can contribute several servings to one pack.

This module *measures* that concentration. It deliberately does not act on
it. The backlog proposed rolling duplicate-parent servings up at assembly
("two chunks of one document both enter a pack and spend the budget
twice"); measurement on the reference deployment refused that fix, and the
refusal is recorded in :func:`measure_parent_concentration`'s docstring
rather than in a commit message, because the numbers are what make it
re-checkable.

Over the 30 days to 2026-08-28 (n=37 packs, 17 carrying citations):

* 16 of 37 packs served two or more items from one parent document; 26 such
  groups, 51 extra servings, 6319 of 53875 injected tokens (11.7%).
* Those extras are **not** duplicated text. Chunk overlap is 200 chars
  against a 3000-char target (~6.7%), and each chunk's excerpt is cut from
  its own head, so no two chunk excerpts in a pack repeat each other. The
  F14/#259 MinHash pass (0.85 Jaccard over excerpts) therefore cannot
  collapse them, and should not: it would be collapsing distinct content.
* The extras are top-ranked, not tail. All five cited-helpful extra
  servings sat at ranks 3, 3, 4, 5 and 5. A per-parent body cap costs
  cited-helpful bodies before it saves much: ``K=1`` demotes 32 body
  servings (4012 tokens) and **all 5** cited-helpful extras; ``K=2``
  demotes 14 (1758 tokens) and 4 of 5; ``K=3`` demotes 6 (755 tokens) and
  2 of 5.
* When a document is on-topic its chunks are *jointly* useful, not
  redundantly so. Of the two groups that earned any helpful citation, both
  had two or more helpful members (4 of 4, and 3 of 5); no group had
  exactly one helpful member. Keeping "the best chunk" is not a
  lossless summary of that.
* The saving would not materialise anyway. ``max_tokens`` behaves as a
  quota, not a ceiling — 20 of 37 packs hit ``max_items`` and 436
  candidates went unserved, of which 55 shared a parent already in the
  pack. A slot freed by demoting a chunk largely refills with another
  chunk of the same or another conversation.
* #359's graduated disclosure already banks the tail half of the effect:
  19 of the 51 extras ranked past ``body_items`` and are already served as
  one-line pointers.

What remains is a genuine question the numbers cannot yet settle — the
cited-helpful evidence rests on two attributed groups. That is why this is
an instrument and not a prose claim: ``PACK_ASSEMBLED.payload[
"parent_concentration"]`` records the quantity per pack, so the same
question can be re-asked at a larger ``n`` without re-deriving it from
``item_id`` string-matching in an ad-hoc script.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Any

from trellis.schemas.pack import PackItem

__all__ = [
    "ParentConcentration",
    "measure_parent_concentration",
    "resolve_parent_id",
]

#: Servings from one parent below which there is nothing to concentrate.
#: A single serving is the normal case and is not a "group".
_MIN_GROUP_SIZE = 2


def resolve_parent_id(item: PackItem) -> str:
    """The source document *item* is a serving of.

    ``metadata["parent_doc_id"]`` is authoritative: every chunk row is
    written with it (:func:`trellis.ingest_corpus.sync._write_chunks`), and
    both the keyword and semantic axes splat full document metadata onto
    the :class:`~trellis.schemas.pack.PackItem`.

    The id-scheme fallback exists because the semantic axis builds its item
    from the *vector row's* metadata snapshot, which is taken at embed time
    (#338) — a row embedded before a metadata key existed will not carry
    it, and reading the id is then the only signal left. An item that is
    neither a chunk nor carries the key is its own parent, so a pack of
    unrelated items reports zero concentration rather than one giant group.
    """
    metadata = item.metadata or {}
    parent = metadata.get("parent_doc_id")
    if isinstance(parent, str) and parent:
        return parent
    # Deferred: ``trellis.ingest_corpus.__init__`` pulls ``sync``, which
    # imports back into ``trellis.retrieve`` (the embed hook). At module
    # level this would make the two packages mutually importing — the same
    # cycle ``retrieve.file_context`` documents at its own deferred import.
    from trellis.ingest_corpus.models import CHUNK_ID_SEPARATOR  # noqa: PLC0415

    if CHUNK_ID_SEPARATOR in item.item_id:
        return item.item_id.split(CHUNK_ID_SEPARATOR)[0]
    return item.item_id


@dataclass(frozen=True)
class ParentConcentration:
    """How many of a pack's servings came from repeat source documents.

    Counted per ``(pack, item)`` **serving**, never per distinct id: one
    document bodied in one pack and withheld in another is two different
    facts, and collapsing them to a distinct-id count understated a real
    cost by an order of magnitude when this repo last tried it.

    ``extra_*`` fields count servings *beyond the first* from each parent —
    the ones a rollup would have merged away. The ``body`` variants
    restrict that to servings still carrying an excerpt after graduated
    disclosure, which is the population a rollup would actually change:
    an extra already demoted to a pointer costs a pointer, not a body.
    """

    #: Parents contributing two or more servings to this pack.
    groups: int = 0
    #: Servings beyond the first from each such parent.
    extra_servings: int = 0
    #: Charged excerpt cost of those extra servings.
    extra_tokens: int = 0
    #: Largest number of servings any one parent contributed.
    max_group_size: int = 0
    #: Extra servings still served as bodies (not demoted to pointers).
    extra_body_servings: int = 0
    #: Charged cost of the extra servings still served as bodies.
    extra_body_tokens: int = 0

    def as_telemetry(self) -> dict[str, Any]:
        """Payload fragment for the ``PACK_ASSEMBLED`` event.

        Emitted even when a pack has no repeat parents, so a consumer can
        distinguish "measured, and the pack was clean" from "never
        measured" — the same contract
        :meth:`~trellis.retrieve.disclosure.DisclosureResult.as_telemetry`
        keeps.
        """
        return {
            "groups": self.groups,
            "extra_servings": self.extra_servings,
            "extra_tokens": self.extra_tokens,
            "max_group_size": self.max_group_size,
            "extra_body_servings": self.extra_body_servings,
            "extra_body_tokens": self.extra_body_tokens,
        }


def measure_parent_concentration(
    items: list[PackItem],
    *,
    pointer_item_ids: frozenset[str] | set[str] | None = None,
) -> ParentConcentration:
    """Measure repeat-source concentration across *items*.

    Pure and read-only — it never reorders, drops or rewrites an item. See
    the module docstring for why measuring is the whole intervention here
    and what the production numbers said about the rollup that was
    proposed instead.

    *items* are taken in served order; the first serving of each parent is
    the one a rollup would have kept, so every later serving from that
    parent is counted as "extra". *pointer_item_ids* names the servings
    graduated disclosure already reduced to one-line pointers (#359), which
    are excluded from the ``extra_body_*`` totals.
    """
    pointers = frozenset(pointer_item_ids or ())
    by_parent: dict[str, list[PackItem]] = collections.defaultdict(list)
    for item in items:
        by_parent[resolve_parent_id(item)].append(item)

    groups = 0
    extra_servings = 0
    extra_tokens = 0
    max_group_size = 0
    extra_body_servings = 0
    extra_body_tokens = 0

    for members in by_parent.values():
        max_group_size = max(max_group_size, len(members))
        if len(members) < _MIN_GROUP_SIZE:
            continue
        groups += 1
        for item in members[1:]:
            tokens = item.estimated_tokens or 0
            extra_servings += 1
            extra_tokens += tokens
            if item.item_id not in pointers:
                extra_body_servings += 1
                extra_body_tokens += tokens

    return ParentConcentration(
        groups=groups,
        extra_servings=extra_servings,
        extra_tokens=extra_tokens,
        max_group_size=max_group_size,
        extra_body_servings=extra_body_servings,
        extra_body_tokens=extra_body_tokens,
    )
