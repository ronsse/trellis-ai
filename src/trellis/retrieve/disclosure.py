"""Graduated disclosure: full bodies at the head of a pack, pointers in the tail.

Measured on the reference deployment (30-day window, 15 attributed packs,
251 injected items. The two shape facts below come from a per-rank
breakdown of that window; every before/after figure comes from
``trellis analyze replay``:

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
cheaper still — ``--max-items 12`` replays at -37.9% tokens against
graduation's -26.4%, and lifts the fraction further — but it *deletes* the
tail. Ninety-six of 251 items become unreachable, and **four of the
twenty-eight cited-helpful servings in the window sat past rank 12**: a
caller who found those useful would simply not have been given them.
Graduation withholds one, and withholds it as a fetchable pointer rather
than a deletion. The tail is low-yield, not worthless, and the same
commitment that makes the content floor a penalty rather than an exclusion
applies here — a legitimately terse memory, or a genuinely good one the
ranker put eighteenth, must not be silently dropped.

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
excerpts first makes each item cheaper and the greedy simply admits more
of them — the saving is spent on more tail. That is not a hypothesis: it
is what the width lever does. ``trellis analyze replay
--excerpt-max-chars 300`` prices a narrower excerpt against the same
window and returns -5.5% tokens with the useful-token fraction falling
from 0.102 to 0.076, because the walk backfilled 92 items nobody had ever
graded. Suppress the refill (``--no-refill``, diagnostic only, the shipped
walk does refill) and the same cap saves 30.9% with the fraction flat at
0.104 — a ratio is close to invariant under a uniform width scaling,
since the numerator shrinks with the denominator.

Applied after the walk instead, the pack keeps exactly the items it
already chose and simply costs less: ``--body-items 12`` replays at
**-26.4% tokens and a useful-token fraction of 0.131 against 0.102, a
+28.6% relative lift, with one of twenty-eight cited-helpful servings
demoted to a pointer and none dropped.** The pack comes in *under* its
ceiling, which is what a ceiling is for.

Reproduce any of this with ``trellis analyze replay`` — see
:mod:`trellis.retrieve.pack_replay` for what the counterfactual can and
cannot honestly claim.
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
#: ``15``: same cost (one withheld cited-helpful serving of 28), more
#: saving (-26.4% against -18.8%), better fraction (+28.6% against
#: +16.5%). Going lower keeps paying — ``8`` reaches -36.9% — but starts
#: withholding a second body, and the risk is asymmetric: a token spent on
#: a tail nobody reads is cheap, a memory the caller needed and did not
#: fetch is not. Twelve is the deepest cut the window supports at a cost
#: of one. Re-run ``trellis analyze replay --body-items N`` as the
#: attributed sample grows; the number should move with the evidence.
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

    The size quoted is what this *pack* withheld — the excerpt the item was
    carrying, which may itself already be a 500-character cut of a far
    longer document. That is why the note says ``get_items`` fetches *the
    source* rather than "the rest": the fetch resolves the id against the
    document, graph and trace stores and returns the whole record, which
    for a long document is more than the number quoted here. An agent
    budgeting a fetch should read the size as a floor, not an amount.
    """
    label = item_label(
        {"metadata": item.metadata, "excerpt": item.excerpt},
        POINTER_LABEL_MAX_CHARS,
    )
    withheld = format_char_count(len(item.excerpt or ""))
    note = f"[+{withheld} chars withheld — get_items fetches the source]"
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
