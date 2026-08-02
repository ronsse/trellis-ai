"""Excerpt hygiene for pack assembly: boundary-aware truncation + content floor.

Two related honesty problems with what a pack actually serves an agent:

* **Mid-word truncation.** The search strategies used to slice
  ``content[:500]``, which cuts wherever the 500th character lands —
  routinely mid-word, sometimes mid-token in a code identifier. The agent
  reads a mangled last word and cannot tell whether the text ended or was
  cut. :func:`truncate_excerpt` breaks at a sentence boundary, else a word
  boundary, and marks the cut with an ellipsis. It never returns more
  characters than the raw slice did, so the ~4-chars-per-token budget
  arithmetic in :class:`~trellis.retrieve.pack_builder.PackBuilder` is
  unchanged.

* **Substance-free items.** Roughly 40% of graph nodes in the live corpus
  are name-only stubs whose excerpt falls back to the node name — three
  words of "content" occupying an item slot in an agent's context window,
  ranked alongside substantive memories. :func:`apply_content_floor`
  measures substance and, by default, *demotes* thin items rather than
  dropping them.

Why demote and not drop: the shortest genuinely useful memory this system
serves — a one-line gotcha, a two-clause procedure — is exactly the shape a
hard length filter would silently delete. A multiplicative penalty pushes a
name-only stub behind anything substantive while leaving it eligible when
the candidate pool is thin, and the decision is recorded on the item's
``score_breakdown`` so it is never invisible. Hard exclusion is available
via ``mode="exclude"`` for callers who want it, and emits a
:class:`~trellis.schemas.pack.RejectedItem` with reason ``content_floor``
so dropped items still show up in ``PACK_ASSEMBLED`` telemetry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from trellis.schemas.pack import PackItem, RejectedItem

#: Maximum characters an excerpt may occupy. Matches the raw
#: ``content[:500]`` slice the strategies used before boundary-aware
#: truncation, so per-item token estimates do not shift.
EXCERPT_MAX_CHARS = 500

#: Marker appended when text was cut. Counted against ``EXCERPT_MAX_CHARS``
#: — a truncated excerpt is never *longer* than the raw slice it replaces.
EXCERPT_ELLIPSIS = "…"

#: A boundary is only honoured if it retains at least this fraction of the
#: available characters. Without it, a lone "Note." in the first 20
#: characters would collapse a 500-character excerpt to one word.
_MIN_BOUNDARY_FRACTION = 0.5

#: Sentence terminator followed by whitespace or end-of-window. The
#: lookahead keeps "v1.2" and "trellis.retrieve" from reading as sentence
#: ends.
_SENTENCE_END_RE = re.compile(r"[.!?…](?=\s|$)")

#: Word-ish token for substance counting. Keeps intra-word apostrophes and
#: hyphens together so "trellis-ai" and "don't" each count once.
_WORD_RE = re.compile(r"[\w'-]+")

#: Distinct words below which an item is considered substance-free.
#: Calibrated against the two populations this has to separate: a graph
#: stub's excerpt is a bare entity name (1-4 tokens — "PackBuilder",
#: "Nathan Ronsse", "trellis-ai"), while the tersest memory worth serving
#: is a full clause ("restart the api container before re-running trellis
#: admin init" — 9 distinct words). Five sits in the gap, above every
#: name-shaped stub and below every real sentence.
DEFAULT_MIN_DISTINCT_WORDS = 5

#: Multiplier applied to a thin item's relevance score in ``penalize``
#: mode. Chosen against :class:`~trellis.retrieve.strategies.GraphSearch`'s
#: position decay (0.05 per rank): 0.35 demotes a top-ranked stub to
#: roughly the score of the 13th substantive node — past everything with
#: real content, but still ahead of the tail, so a thin pool can still
#: serve it.
DEFAULT_CONTENT_FLOOR_PENALTY = 0.35

#: Rejection reason recorded when ``mode="exclude"`` drops an item.
CONTENT_FLOOR_REJECTION_REASON = "content_floor"

ContentFloorMode = Literal["penalize", "exclude", "off"]


def truncate_excerpt(
    text: str,
    limit: int = EXCERPT_MAX_CHARS,
    *,
    marker: str = EXCERPT_ELLIPSIS,
) -> str:
    """Truncate ``text`` to at most ``limit`` characters on a clean break.

    Preference order:

    1. **Sentence boundary** — the last ``.``/``!``/``?`` followed by
       whitespace that still keeps :data:`_MIN_BOUNDARY_FRACTION` of the
       budget.
    2. **Word boundary** — the last whitespace run under the same
       retention guard.
    3. **Hard cut** — only when the tail is one unbroken token (a base64
       blob, a giant URL), where no clean break exists at all.

    The returned string is never longer than ``limit`` and never longer
    than the raw ``text[:limit]`` slice it replaces — the ``marker`` is
    charged against the budget, not appended on top of it. Text at or
    under ``limit`` is returned verbatim with no marker, so short items
    are byte-identical to before.
    """
    if len(text) <= limit:
        return text
    budget = limit - len(marker)
    if budget <= 0:
        # Degenerate limit (shorter than the marker) — fall back to the
        # raw slice rather than returning a string over ``limit``.
        return text[:limit]

    # One character of lookahead: if ``text[budget]`` is whitespace the cut
    # already lands on a word boundary and the whole budget is usable.
    window = text[: budget + 1]
    keep_at_least = int(budget * _MIN_BOUNDARY_FRACTION)

    cut = -1
    sentence_ends = [
        m.end() for m in _SENTENCE_END_RE.finditer(window) if m.end() <= budget
    ]
    if sentence_ends:
        cut = sentence_ends[-1]
    if cut < keep_at_least:
        word_end = _last_word_boundary(window)
        if word_end >= keep_at_least:
            cut = word_end
    if cut <= 0:
        cut = budget

    return text[:cut].rstrip() + marker


def _last_word_boundary(window: str) -> int:
    """Index of the last whitespace run in ``window``, or ``-1``.

    Returns the index *before* the whitespace, i.e. ``window[:result]``
    ends on a complete word.
    """
    stripped = window.rstrip()
    if len(stripped) < len(window):
        # ``window`` already ends on a whitespace run, so the cut lands on
        # a word boundary as-is and the full budget is usable.
        return len(stripped)
    for index in range(len(stripped) - 1, -1, -1):
        if stripped[index].isspace():
            return index
    return -1


def count_substance_words(text: str) -> int:
    """Count the distinct word tokens in ``text``.

    Distinct rather than raw length or total tokens because the two
    degenerate shapes this has to catch — a bare entity name and a
    repeated boilerplate line — both score low on variety while a long URL
    or a padded stub can score high on characters. Case-folded, so
    ``"Trellis trellis"`` counts once. Tokens with no alphanumeric
    character (a stray ``-``) do not count.
    """
    if not text:
        return 0
    return len(
        {
            token.lower()
            for token in _WORD_RE.findall(text)
            if any(char.isalnum() for char in token)
        }
    )


@dataclass(frozen=True)
class ContentFloorConfig:
    """Configuration for the pack content floor.

    ``mode`` semantics:

    * ``"penalize"`` (default) — multiply the item's ``relevance_score``
      by ``penalty`` and stamp the decision onto ``score_breakdown``. The
      item stays a candidate; a thin pool can still serve it. This is the
      safe default precisely because a terse-but-real memory is one of the
      highest-value things this system stores.
    * ``"exclude"`` — drop the item and record a ``content_floor``
      :class:`~trellis.schemas.pack.RejectedItem`. Simple, but it *will*
      hide short-and-good content; opt in deliberately.
    * ``"off"`` — no-op. Kept so the floor can be disabled without
      threading ``None`` through every call site.
    """

    min_distinct_words: int = DEFAULT_MIN_DISTINCT_WORDS
    mode: ContentFloorMode = "penalize"
    penalty: float = DEFAULT_CONTENT_FLOOR_PENALTY

    def __post_init__(self) -> None:
        if self.min_distinct_words < 0:
            msg = f"min_distinct_words must be >= 0; got {self.min_distinct_words!r}"
            raise ValueError(msg)
        if not 0.0 <= self.penalty <= 1.0:
            msg = f"penalty must be in [0.0, 1.0]; got {self.penalty!r}"
            raise ValueError(msg)
        if self.mode not in ("penalize", "exclude", "off"):
            msg = f"mode must be one of 'penalize', 'exclude', 'off'; got {self.mode!r}"
            raise ValueError(msg)


#: Shared default — demote, never drop. Applied by
#: :class:`~trellis.retrieve.pack_builder.PackBuilder` when no explicit
#: config is supplied.
DEFAULT_CONTENT_FLOOR = ContentFloorConfig()


@dataclass(frozen=True)
class ContentFloorResult:
    """Outcome of one :func:`apply_content_floor` pass."""

    config: ContentFloorConfig
    items: list[PackItem] = field(default_factory=list)
    rejected: list[RejectedItem] = field(default_factory=list)
    penalized_item_ids: list[str] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        """Payload fragment for the ``PACK_ASSEMBLED`` event.

        Emitted even when nothing tripped the floor so consumers can tell
        "floor ran, nothing thin" from "floor never ran".
        """
        return {
            "mode": self.config.mode,
            "min_distinct_words": self.config.min_distinct_words,
            "penalty": self.config.penalty,
            "penalized_count": len(self.penalized_item_ids),
            "penalized_item_ids": list(self.penalized_item_ids),
            "excluded_count": len(self.rejected),
            "excluded_item_ids": [r.item_id for r in self.rejected],
        }


def apply_content_floor(
    items: list[PackItem],
    config: ContentFloorConfig = DEFAULT_CONTENT_FLOOR,
) -> ContentFloorResult:
    """Demote (or drop) items whose excerpt carries too little substance.

    Item order is preserved; callers sort by ``relevance_score``
    afterwards so the penalty takes effect. Penalised items carry
    ``content_floor_penalty`` and ``content_floor_substance_words`` in
    ``score_breakdown``, which
    :meth:`~trellis.retrieve.pack_builder.PackBuilder._annotate_selected_items`
    preserves and ``PACK_ASSEMBLED`` telemetry already emits per item.
    """
    if config.mode == "off":
        return ContentFloorResult(config=config, items=list(items))

    kept: list[PackItem] = []
    rejected: list[RejectedItem] = []
    penalized_ids: list[str] = []
    for item in items:
        words = count_substance_words(item.excerpt or "")
        if words >= config.min_distinct_words:
            kept.append(item)
            continue
        if config.mode == "exclude":
            rejected.append(
                RejectedItem(
                    item_id=item.item_id,
                    item_type=item.item_type,
                    relevance_score=item.relevance_score,
                    reason=CONTENT_FLOOR_REJECTION_REASON,
                    strategy_source=item.strategy_source,
                )
            )
            continue
        penalized_ids.append(item.item_id)
        kept.append(_penalize(item, words, config.penalty))

    return ContentFloorResult(
        config=config,
        items=kept,
        rejected=rejected,
        penalized_item_ids=penalized_ids,
    )


def _penalize(item: PackItem, words: int, penalty: float) -> PackItem:
    """Return a copy of ``item`` with the floor penalty applied."""
    penalized_score = item.relevance_score * penalty
    breakdown = dict(item.score_breakdown)
    # Mirror the annotation step's convention so a penalised item's
    # breakdown is not *missing* the plain score once we make it non-empty.
    breakdown.setdefault("relevance_score", penalized_score)
    breakdown["content_floor_penalty"] = penalty
    breakdown["content_floor_substance_words"] = float(words)
    return item.model_copy(
        update={
            "relevance_score": penalized_score,
            "score_breakdown": breakdown,
        }
    )


__all__ = [
    "CONTENT_FLOOR_REJECTION_REASON",
    "DEFAULT_CONTENT_FLOOR",
    "DEFAULT_CONTENT_FLOOR_PENALTY",
    "DEFAULT_MIN_DISTINCT_WORDS",
    "EXCERPT_ELLIPSIS",
    "EXCERPT_MAX_CHARS",
    "ContentFloorConfig",
    "ContentFloorMode",
    "ContentFloorResult",
    "apply_content_floor",
    "count_substance_words",
    "truncate_excerpt",
]
