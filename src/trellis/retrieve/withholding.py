"""Withholding as a stated fact — a pack must say what it did not serve.

``PackBuilder`` removes candidates for eleven distinct reasons before a pack
is returned, and until now a caller could not tell **"this layer was empty"**
from **"this layer was redacted."** Both render as fewer items, or as *"No
context found for: …"*, which reads as greenfield.

**This module changes nothing about what is withheld.** It reports it.
Every gate keeps its own rule, its own default and its own escape hatch; the
only new behaviour is a line of text.

What already existed, and what did not
--------------------------------------

Most of the *data* was there. ``PACK_ASSEMBLED.payload["rejected_items"]``
carries a per-item row for eight of the first ten gates, ``budget_trace[]``
prices every candidate the walk saw including the rejected ones (#359 replays on
exactly that), and ``payload["content_floor"]`` records the floor's
decisions. Two things were missing, and they are the whole of this change:

1. **None of it reached the pack the caller reads.**
   :func:`~trellis.retrieve.formatters.format_pack_as_markdown` is handed
   ``list[dict]`` of *served* items and never sees
   :class:`~trellis.schemas.pack.RetrievalReport`. The one "omitted" line it
   could print counts items the *renderer* could not fit, not items the
   *builder* withheld, and it prints after a ``break`` that a full pack
   never reaches.
2. **Two gates recorded nothing anywhere.**
   :func:`~trellis.retrieve.noise.exclude_noise` and
   :func:`~trellis.retrieve.lifecycle.exclude_archived` run at the collect
   seam and their only observable was ``logger.debug``, which is a no-op
   under the CLI's ``WARNING`` default and under the MCP server's own
   configuration. A noise-demoted item vanished from the pack, from the
   event payload, and from the log at once. That is the silent-fallback
   shape this repo keeps finding, at the serving boundary.

Withheld means *absent*, and is computed that way
--------------------------------------------------

A rejection is not the same as an absence, and conflating them would make
this report lie in the direction that matters most.
:meth:`~trellis.retrieve.pack_builder.PackBuilder._deduplicate_tracked`
records a ``RejectedItem`` for the *losing copy* of an item retrieved by two
strategies — but the winning copy carries the same ``item_id`` and is served.
Reporting that as "1 item withheld" would tell a caller a memory was kept
from it while the memory is on screen.

So the summary is a set difference, not a count of rejections:

    withheld = {rejected item_ids} - {served item_ids}

which drops ``dedup`` on its own, without a special case, and keeps
``semantic_dedup`` (a *different* id, genuinely absent). It also means
graduated disclosure (#359) contributes nothing here and should not: a
pointer is a served item — ranked, cited, and fetchable by
:func:`get_items` — and calling it withheld would blur the distinction that
issue was careful to make.

An id withheld under more than one gate is attributed to the **first** gate
that rejected it, because ``rejected`` is appended in pipeline order and the
first removal is the one that actually happened.

What this cannot see, and why that boundary moved
--------------------------------------------------

The summary is computed from ``RejectedItem`` rows, and a ``RejectedItem``
requires a ``PackItem``. **A row a strategy discards before constructing a
PackItem is invisible here** — there is no candidate to withhold and no
channel back to the builder. That covers all three of
:class:`~trellis.retrieve.strategies.GraphSearch`'s client-side filters:
structural, unconfirmed (#301) and domain scope. The boundary is as old as
the initial commit.

What changed is the *population* on the far side of it. #375/#436 mints the
meta-recorder's per-invocation ``Activity`` ``node_role="structural"``, so
the graph axis now drops it **before** the candidate slice, rather than
``PackBuilder._is_meta_activity`` dropping it after. Measured end to end on
one store with one graph strategy, 6 meta-Activities and 4 memories: under
the pre-#436 ``semantic`` role the summary reports
``{meta_activity_filter: 6}`` and renders a note; under the ``structural``
role it reports nothing at all. The same six rows are removed either way.

**That is the right answer for the caller and the wrong one for the
operator**, which is why it is documented here rather than fixed:

* For the caller, the old note was the lie. "6 items matched this intent but
  were not served" was false twice over — the graph axis is a recency feed,
  not a matcher, and the rows were Trellis's own per-cron plumbing, never
  memory. Silence is honest. Restoring the count would put a permanent,
  content-free line on every pack.
* For the operator, ``PACK_ASSEMBLED``'s ``meta_filtered_count`` now reads
  ``0`` on a post-#436 corpus while suppression happens on every pack, and
  the structural filter has **no** observable at all — not even the
  ``logger.debug`` its ``include_unconfirmed`` sibling three lines below it
  emits.

Note the tension this leaves, because :mod:`trellis.retrieve.lifecycle` and
:mod:`trellis.retrieve.noise` state the opposite rule — "enforced where
``PackBuilder`` collects, not per strategy", on the grounds that a rule
applied inside a built-in strategy would not hold for a fourth strategy
added later. The *rule* still holds: ``_is_structural`` and
``_is_meta_activity`` remain as collect-seam backstops covering every axis.
Only its **earliness**, and therefore its observability, is strategy-local,
and #436 bought that earliness deliberately — the point was to stop the rows
spending candidate slots. The asymmetry with ``exclude_noise`` /
``exclude_archived`` is real, and it is *not* explained by "a strategy-level
filter never produces a candidate": ``candidates_found`` is incremented
**after** the collect gates too, so an archived item is not a candidate
either, and it is still recorded. What separates them is where the code
happens to live.

Counts and reasons, never content
----------------------------------

The rendered note carries a total, a reason and a count per reason. It does
not carry ids and it does not carry excerpts. Naming the ids would invite the
caller to re-fetch precisely what a gate decided not to serve — and for the
noise and archived gates, that is the item the feedback loop or the retention
pass judged should stop being served. The ids remain in the event payload for
the operator, which is a different access path and a different audience.

Reason vocabulary
-----------------

Derived from the gates that exist, not from a wish list. The values are the
``RejectedItem.reason`` strings the pipeline already writes, reused verbatim
rather than re-labelled: a second vocabulary for the same facts is how
``content_type`` and ``document_form`` drifted apart in #325/#326.

There is deliberately **no** ``sensitivity`` reason. Nothing in
``src/trellis/retrieve/`` reads ``DataClassification``, and ``Pack``'s
``policies_applied`` field is populated by no code path in the repository —
so a ``sensitivity`` count could only ever render ``0``. Emitting a reason
code for a gate that does not exist is the defect this module was filed to
remove, not to reproduce. Sensitivity *enforcement* is #194; when it lands it
adds a gate here, and this report will count it because it counts whatever
the pipeline rejects.

Section routing is the eleventh gate, and it is reported differently
--------------------------------------------------------------------

:meth:`~trellis.retrieve.pack_builder.PackBuilder.build_sectioned` routes
the shared candidate pool through
:meth:`~trellis.retrieve.tier_mapping.TierMapper.matches_section` and keeps
only what a requested section matched. The losing side was discarded
without a ``RejectedItem``, so a sectioned pack could serve **zero** items
and report ``total: 0`` — an affirmative *"nothing was withheld"*, which is
a stronger and more misleading signal than the silence #404 replaced
(#440).

It is recorded now, but it is **not** a group in ``by_reason`` and it does
not enter ``total``, because it does not make the claim the other ten gates
make. Measured on the reference deployment: replaying the two shipped
section presets over the 47 flat packs assembled since 2026-07-07, the
routing removes at least one *served* item on **46/47** packs under
``get_task_context``'s spec and **47/47** under ``get_objective_context``'s
— median 10 and 16 items respectively. Joining ``by_reason`` would
therefore put a ``section_filter 10`` line on essentially every sectioned
pack, which is exactly the failure :func:`format_withholding_note` names:
a marker that always fires is one the reader learns to skip.

That replay has **two biases running in opposite directions**, and only
one of the two quantities it is asked for survives them. It reads
``injected_items[]``, so it sees only the subset a flat budget already
cut, which *under*-counts routing against the whole deduped pool — but
that payload carries neither ``retrieval_affinity`` nor ``scope``, while
``matches_section`` consults the first of those **before** any heuristic
and 968 of this deployment's 1533 documents carry one, which *over*-counts
it. For the **per-pack count** the net direction is safe: re-derived with
the documents' real tags and with the candidates the flat budget cut added
back to the pool, the medians rise to 28 and 53 and every one of the 47
packs loses at least one item under both presets. The ``by_reason``
conclusion holds a fortiori.

So the count is split from the claim:

* ``section_filtered`` rides the ``PACK_ASSEMBLED`` payload on **every**
  build, because the operator is a different audience from the caller (the
  same split this module already makes for ``withheld_item_ids``), and
  because the interesting quantity — how often routing empties a pack —
  should be re-askable at larger *n* from the served record rather than
  re-derived by simulation, as it had to be here.
* The **note** renders only when the pack served nothing, which is the one
  case where the count is both small and load-bearing. **How often that
  happens is the quantity the replay cannot answer**, and it is the second
  of the two above: "no candidate matched any section" needs the whole
  pool and the real tags, and the two biases stop cancelling — 10 of 47
  packs from ``injected_items`` alone, 7 once the served items' real tags
  are read, 0 once the flat budget's own cuts are added back. Treat the
  simulated rate as an upper bound of unknown tightness. What is *not* a
  simulation is that **one of the four** sectioned packs this deployment
  has ever assembled served zero items — which is why
  ``section_filtered`` is emitted on every build: so this becomes a
  property of the served record instead of a replay. The note gets its own
  sentence rather than a ``by_reason`` entry, because "matched no section
  you asked for" is a narrower statement than "a gate removed this."

An item routed away from section A and served in section B is never
reported, and it is worth being exact about *which* mechanism does that.
The builder defines the routed set as **matched no section at all**, so
such an item is never rejected in the first place; the ``{rejected} -
{served}`` subtraction is a backstop here, not the mechanism. Defining the
set per-section instead would lean entirely on the subtraction — and the
subtraction is not enough. An item that section B matched and then cut on
``max_items`` would collect a ``section_filter`` row from section A too,
and first-reason-wins would hand it to whichever row happened to land
first, moving a genuinely budget-withheld item out of ``by_reason`` (which
renders) into a sentence that renders on empty packs only. Both halves are
pinned by test: the per-section variant fails the attribution test, and
the cross-section survivor is checked by execution rather than assumed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from trellis.schemas.pack import RejectedItem

from trellis_wire.withholding import (
    WithheldGroup,
    WithholdingSummary,
    format_withholding_note,
    withholding_from_payload,
)

#: Rejection reasons that never denote an absence, so they are excluded from
#: the summary before the served-set subtraction rather than relying on it.
#:
#: ``dedup`` is the only member: it rejects the losing *copy* of an id whose
#: winner is served. The subtraction alone already handles it in a normal
#: build, but not when the winner is later dropped by a downstream gate — in
#: which case the honest reason is that downstream gate, not "duplicate".
NON_ABSENCE_REASONS: frozenset[str] = frozenset({"dedup"})

#: Reason recorded for a candidate that matched the intent but none of the
#: sections the caller requested (#440). A real absence — it is subtracted
#: against the served set like every other rejection — but reported as its
#: own field rather than as a ``by_reason`` group, for the measured reason
#: in the module docstring.
SECTION_FILTER_REASON: str = "section_filter"


def summarize_withheld(
    rejected: Iterable[RejectedItem],
    served_item_ids: Collection[str],
) -> WithholdingSummary:
    """Group rejections into the items actually absent from the pack.

    An id counts once, under the first reason that rejected it, and only
    when it is missing from ``served_item_ids``. See the module docstring
    for why the set difference is the definition rather than a filter
    applied to it.

    :data:`SECTION_FILTER_REASON` takes the same trip — subtracted against
    the served set, attributed first-reason-wins — and is then peeled off
    into ``section_filtered`` instead of a ``groups`` entry, so it stays out
    of ``total``, out of ``by_reason`` and out of ``withheld_item_ids``
    (which would otherwise carry a median of ten ids on every sectioned
    pack, for a count the caller is not shown).
    """
    served = set(served_item_ids)
    first_reason: dict[str, str] = {}
    non_absence: list[str] = []
    for item in rejected:
        if item.reason in NON_ABSENCE_REASONS or item.item_id in served:
            if item.reason not in non_absence:
                non_absence.append(item.reason)
            continue
        first_reason.setdefault(item.item_id, item.reason)

    counts: dict[str, int] = {}
    section_filtered = 0
    for reason in first_reason.values():
        if reason == SECTION_FILTER_REASON:
            section_filtered += 1
            continue
        counts[reason] = counts.get(reason, 0) + 1

    groups = tuple(
        WithheldGroup(reason=reason, count=count)
        for reason, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return WithholdingSummary(
        groups=groups,
        withheld_item_ids=tuple(
            item_id
            for item_id, reason in first_reason.items()
            if reason != SECTION_FILTER_REASON
        ),
        non_absence_reasons=tuple(sorted(non_absence)),
        section_filtered=section_filtered,
        served_count=len(served),
    )


__all__ = [
    "NON_ABSENCE_REASONS",
    "SECTION_FILTER_REASON",
    "WithheldGroup",
    "WithholdingSummary",
    "format_withholding_note",
    "summarize_withheld",
    "withholding_from_payload",
]
