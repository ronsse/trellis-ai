"""Graduated disclosure: full bodies at the head of a pack, pointers in the tail.

Measured on the reference deployment (30-day window, 15 attributed packs,
251 injected items, ``trellis analyze replay``):

* The bottom fifth of a pack by rank carries **23% of its tokens and 2.4%
  of its cited-helpful tokens.** The top fifth carries 20.4%.
* Nine of the fifteen attributed packs ran to the *token* budget, not the
  item budget — 17 to 35 items each. Nobody chose 35 items; the number
  fell out of ``max_tokens ÷ ~90 tokens per excerpt``.

That second fact is the defect. ``PackBudget.max_tokens`` is documented as
a ceiling and implemented as a **quota**: the greedy walk keeps admitting
items while any budget remains, so a caller who asks for "up to 2000
tokens" always gets 2000 tokens, the last several hundred of which buy
almost nothing. Graduated disclosure spends the head of that budget on
bodies and refuses to spend the tail on more of them.

**Why not just serve fewer items.** A hard item cap at the same rank is
cheaper still (−44% tokens against −28%) but it *deletes* the tail: 114 of
251 items become unreachable to the agent, and four of the twenty-eight
cited-helpful items in the window sat past it. The tail is low-yield, not
worthless, and the same commitment that makes the content floor a penalty
rather than an exclusion applies here — a legitimately terse memory, or a
genuinely good one the ranker put fourteenth, must not be silently
dropped. A pointer keeps every id addressable: the agent sees the label
and the withheld size and fetches the body with ``get_items`` if it wants
it.

**Why not make index mode the default instead.** Measured, an index line
costs 33 tokens against a 90-token mean excerpt — 2.7:1, not the order of
magnitude the name suggests, because ``item_id`` alone averages 45
characters. Surveying all 251 items as lines costs 37% of the full pack,
so index mode only pays if the agent then opens fewer than ~63% of what it
surveyed. Nothing measures that: index mode (#305) fired on **zero** of
the 37 packs assembled in the window. Graduated disclosure applies the
index line exactly where the fetch rate is demonstrably near zero — the
tail nobody cites — and leaves the head alone.

**Why rank and not score.** A relative-score stop would be the more
principled rule, and it is not available: after RRF fusion every item in a
pack scores within 0.87-1.00 of the pack's top item (helpful items median
0.976, unjudged 0.910), so no threshold separates them. Rank is the only
signal that does, which is why the cut is an integer and not a ratio.

**Where it runs.** *After* the token-budget walk, never before. Rewriting
excerpts first would make each item cheaper and the greedy would simply
admit more of them — same tokens, more tail. Replayed over the same
window, pre-walk disclosure drives the useful-token fraction from 0.102 to
0.076 while saving 5% of tokens; post-walk it goes to 0.124 while saving
24%. The pack comes in *under* its ceiling, which is what a ceiling is
for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from trellis.core.elision import format_char_count
from trellis.retrieve.formatters import item_label
from trellis.schemas.pack import PackItem

#: Full excerpts served before the tail becomes pointers.
#:
#: Chosen on the replay above, which is a 15-pack sample — small, and the
#: value is fitted to it, so treat it as the best current estimate rather
#: than a constant with a proof behind it. On that window ``12`` dominates
#: its neighbours: it keeps 24 of 28 cited-helpful bodies (the same count
#: 15 and 20 keep) while saving 24% of tokens against their 17% and 8%.
#: Re-run ``trellis analyze replay`` as the attributed sample grows.
DEFAULT_BODY_ITEMS = 12

#: Characters of label a pointer carries. Matches the index line's label
#: budget — a pointer and an index line describe the same item.
POINTER_LABEL_MAX_CHARS = 80

#: Reason recorded on a demoted item's ``score_breakdown`` and in
#: ``PACK_ASSEMBLED.payload["disclosure"]``.
POINTER_SELECTION_REASON = "disclosure_pointer"

DisclosureMode = Literal["graduated", "off"]


@dataclass(frozen=True)
class DisclosureConfig:
    """How much of a flat pack is served as bodies rather than pointers.

    ``mode`` semantics:

    * ``"graduated"`` (default) — the first ``body_items`` items keep their
      excerpts; everything past that rank is replaced by a one-line
      pointer carrying its label and the size of the withheld body. No
      item is dropped and no id becomes unreachable.
    * ``"off"`` — every selected item keeps its excerpt. The behaviour
      before this module existed, and what index-mode packs use: an index
      pack is already all pointers, so graduating it would be a second cut
      of an already-cut rendering.

    ``body_items`` of ``0`` is rejected rather than treated as "all
    pointers": that is index mode, which has its own path, its own
    renderer and its own telemetry flag, and quietly reproducing it here
    would give two spellings of one behaviour.
    """

    body_items: int = DEFAULT_BODY_ITEMS
    mode: DisclosureMode = "graduated"

    def __post_init__(self) -> None:
        if self.body_items < 1:
            msg = (
                "body_items must be >= 1 (use mode='off' for no graduation, "
                f"or index_mode for an all-pointer pack); got {self.body_items!r}"
            )
            raise ValueError(msg)
        modes = get_args(DisclosureMode)
        if self.mode not in modes:
            allowed = ", ".join(repr(m) for m in modes)
            msg = f"mode must be one of {allowed}; got {self.mode!r}"
            raise ValueError(msg)


#: Shared default — graduate at :data:`DEFAULT_BODY_ITEMS`.
DEFAULT_DISCLOSURE = DisclosureConfig()

#: Disable graduation entirely.
DISCLOSURE_OFF = DisclosureConfig(mode="off")


@dataclass(frozen=True)
class DisclosureResult:
    """Outcome of one :func:`apply_disclosure` pass."""

    config: DisclosureConfig
    items: list[PackItem] = field(default_factory=list)
    pointer_item_ids: list[str] = field(default_factory=list)
    tokens_before: int = 0
    tokens_after: int = 0

    def as_telemetry(self) -> dict[str, Any]:
        """Payload fragment for the ``PACK_ASSEMBLED`` event.

        Emitted even when nothing was demoted, so a consumer can tell
        "graduation ran, the pack was short" from "graduation never ran".
        ``tokens_before`` / ``tokens_after`` are the pack's excerpt cost on
        either side of the pass — the saving is observable per pack rather
        than only in aggregate.
        """
        return {
            "mode": self.config.mode,
            "body_items": self.config.body_items,
            "pointer_count": len(self.pointer_item_ids),
            "pointer_item_ids": list(self.pointer_item_ids),
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
        }


def pointer_excerpt(item: PackItem) -> str:
    """The one-line stand-in served in place of ``item``'s excerpt.

    Carries the label, the size of the withheld body, and how to get it.
    The withheld size uses the same ``[+2.3k chars]`` rendering
    :func:`~trellis.retrieve.excerpts.truncate_excerpt` marks its own cuts
    with, so an agent reads one convention for "there is more text than
    this" wherever it meets one.

    The size quoted is what this *pack* withheld — the excerpt the item
    was carrying, which may itself already be a 500-character cut of a far
    longer document. Quoting the document's full length would be a number
    this function cannot know and ``get_items`` would not return.
    """
    label = item_label(
        {"metadata": item.metadata, "excerpt": item.excerpt},
        POINTER_LABEL_MAX_CHARS,
    )
    withheld = len(item.excerpt or "")
    note = f"[+{format_char_count(withheld)} chars — fetch id for full text]"
    return f"{label} … {note}" if label else note


def apply_disclosure(
    items: list[PackItem],
    config: DisclosureConfig = DEFAULT_DISCLOSURE,
) -> DisclosureResult:
    """Replace the excerpts of items past ``config.body_items`` with pointers.

    Call this **after** the token-budget walk has chosen the item set (see
    the module docstring: running it earlier turns a saving into a refill)
    and before per-item annotation, so ``estimated_tokens`` records the
    cost actually served rather than the body that was not.

    Item order and item count are preserved exactly — this pass never
    admits, drops or reorders anything. A demoted item keeps its id, type,
    score, strategy source and metadata; only its ``excerpt`` changes, and
    the change is recorded on ``score_breakdown["disclosure_withheld_chars"]``
    as well as in :meth:`DisclosureResult.as_telemetry`.

    Args:
        items: The selected items, already in rank order.
        config: Policy. ``mode="off"`` returns the items untouched.

    Returns:
        A :class:`DisclosureResult` whose ``items`` replace the input.
    """
    before = sum(len(item.excerpt or "") for item in items)
    if config.mode == "off":
        return DisclosureResult(
            config=config,
            items=list(items),
            tokens_before=before,
            tokens_after=before,
        )

    kept: list[PackItem] = []
    pointer_ids: list[str] = []
    for index, item in enumerate(items):
        if index < config.body_items:
            kept.append(item)
            continue
        replacement = pointer_excerpt(item)
        if len(replacement) >= len(item.excerpt or ""):
            # A pointer that is not smaller than the body it stands for
            # buys nothing and costs the agent the text. Short items —
            # a one-line gotcha, a graph stub — are already cheaper than
            # any pointer for them would be.
            kept.append(item)
            continue
        pointer_ids.append(item.item_id)
        breakdown = dict(item.score_breakdown)
        breakdown["disclosure_withheld_chars"] = float(len(item.excerpt or ""))
        kept.append(
            item.model_copy(
                update={
                    "excerpt": replacement,
                    "selection_reason": POINTER_SELECTION_REASON,
                    "score_breakdown": breakdown,
                    # Recomputed by the builder's annotation step from the
                    # replacement text; clearing it here stops a stale body
                    # estimate from surviving as the item's served cost.
                    "estimated_tokens": None,
                }
            )
        )

    after = sum(len(item.excerpt or "") for item in kept)
    return DisclosureResult(
        config=config,
        items=kept,
        pointer_item_ids=pointer_ids,
        tokens_before=before,
        tokens_after=after,
    )


__all__ = [
    "DEFAULT_BODY_ITEMS",
    "DEFAULT_DISCLOSURE",
    "DISCLOSURE_OFF",
    "POINTER_LABEL_MAX_CHARS",
    "POINTER_SELECTION_REASON",
    "DisclosureConfig",
    "DisclosureMode",
    "DisclosureResult",
    "apply_disclosure",
    "pointer_excerpt",
]
