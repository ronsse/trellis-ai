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
— median 10 and 16 items respectively, and both are lower bounds, since the
sectioned path routes the whole deduped pool rather than the subset a flat
budget had already cut. Joining ``by_reason`` would therefore put a
``section_filter 10`` line on essentially every sectioned pack, which is
exactly the failure :func:`format_withholding_note` names: a marker that
always fires is one the reader learns to skip.

So the count is split from the claim:

* ``section_filtered`` rides the ``PACK_ASSEMBLED`` payload on **every**
  build, because the operator is a different audience from the caller (the
  same split this module already makes for ``withheld_item_ids``), and
  because the interesting quantity — how often routing empties a pack —
  should be re-askable at larger *n* from the served record rather than
  re-derived by simulation, as it had to be here.
* The **note** renders only when the pack served nothing, which is the one
  case where the count is both small and load-bearing: 10 of those same 47
  packs would have served zero items under the objective preset, and one of
  the four sectioned packs this deployment has ever assembled did. It gets
  its own sentence rather than a ``by_reason`` entry, because "matched no
  section you asked for" is a narrower statement than "a gate removed this."

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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from trellis.schemas.pack import RejectedItem

logger = structlog.get_logger(__name__)

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


@dataclass(frozen=True)
class WithheldGroup:
    """One reason and the number of distinct items withheld under it."""

    reason: str
    count: int


@dataclass(frozen=True)
class WithholdingSummary:
    """What one pack build withheld, grouped by reason.

    ``groups`` is ordered by descending count then reason name, so the
    rendering is stable across builds and diffable across packs.
    """

    groups: tuple[WithheldGroup, ...] = ()
    #: Distinct item ids withheld, in first-rejection order. Telemetry and
    #: operator surfaces only — never rendered into a pack (see module
    #: docstring).
    withheld_item_ids: tuple[str, ...] = ()
    #: Reasons seen on rejections that were *not* absences (their item was
    #: served anyway). Recorded so a zero total is legibly "nothing was
    #: withheld" rather than "nothing was rejected".
    non_absence_reasons: tuple[str, ...] = ()
    #: Distinct items that matched the intent but no requested section
    #: (#440). Counted on every build for the operator; rendered to the
    #: caller only when the pack served nothing. Deliberately outside
    #: ``groups`` — see the module docstring for the measurement.
    section_filtered: int = 0
    #: Distinct ids the pack served. Carried so the rendering rule for
    #: ``section_filtered`` ("only on an empty pack") survives the trip
    #: through :meth:`as_telemetry` — the formatters read a *serialized*
    #: summary and have no other view of the pack.
    served_count: int = 0

    @property
    def total(self) -> int:
        """Distinct items absent from the pack that a gate removed.

        Excludes ``section_filtered``: those items are absent, but the
        headline is the ten-gate claim and inflating it on every sectioned
        pack is the noise this design was measured to avoid.
        """
        return sum(g.count for g in self.groups)

    @property
    def section_note_applies(self) -> bool:
        """Whether the caller-facing section-routing sentence should render.

        The empty pack is the case #404 was filed about and the only case
        where this count is both small and load-bearing.
        """
        return self.section_filtered > 0 and self.served_count == 0

    def as_telemetry(self) -> dict[str, Any]:
        """Payload fragment for the ``PACK_ASSEMBLED`` event.

        Emitted even when nothing was withheld, so a consumer can tell
        "the summary ran and found nothing" from "the summary never ran" —
        the same posture ``ContentFloorResult.as_telemetry`` takes, and for
        the same reason.
        """
        return {
            "total": self.total,
            "by_reason": {g.reason: g.count for g in self.groups},
            "withheld_item_ids": list(self.withheld_item_ids),
            "non_absence_reasons": list(self.non_absence_reasons),
            "section_filtered": self.section_filtered,
            "served_count": self.served_count,
        }


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


def _keys(payload: Any) -> list[str]:
    """Field names of an unreadable payload — never its values."""
    return sorted(str(k) for k in payload) if isinstance(payload, dict) else []


def _str_list(value: Any) -> list[str]:
    """Coerce a telemetry list field, tolerating absence."""
    return [str(v) for v in value] if isinstance(value, list) else []


def _count(payload: dict[str, Any], key: str) -> int:
    """Read a telemetry counter, distinguishing absence from junk.

    An **absent** key reads ``0`` silently: every payload written before
    the field existed is in that state, and ``0`` is the honest answer for
    it — no sectioned build of that vintage recorded a routed-away item.

    A key that is **present and unusable** warns, for the same reason
    :func:`withholding_from_payload` warns on an unreadable ``by_reason``:
    a reporter whose job is to stop a pack under-reporting must not itself
    under-report quietly. ``bool`` counts as unusable because ``True`` is
    an ``int`` in Python — a boolean here means the writer changed shape,
    not that one item was filtered, and coercing it would render a note
    claiming exactly that.
    """
    value = payload.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning(
            "withholding_payload_unreadable", payload_keys=_keys(payload), field=key
        )
        return 0
    return value


def withholding_from_payload(
    payload: dict[str, Any] | None,
) -> WithholdingSummary | None:
    """Rebuild a summary from its :meth:`WithholdingSummary.as_telemetry` dict.

    The builder computes the summary once and stamps it on
    ``Pack.metadata["withholding"]`` (and on ``SectionedPack.metadata``,
    which has no top-level ``RetrievalReport`` to hold it). A renderer
    therefore reads a *serialized* summary, and reads it the same way for
    both pack kinds — rather than re-deriving one from whichever rejection
    list happens to be reachable on that path, which is how the same fact
    ends up computed two ways and drifting.

    ``None`` for a missing or unusable payload. An unusable payload is
    logged at **warning**: the whole point of this module is that a pack
    must not quietly under-report what it withheld, so a reporter that
    fails to render must not itself fail silently.
    """
    if payload is None:
        return None
    by_reason = payload.get("by_reason") if isinstance(payload, dict) else None
    if not isinstance(by_reason, dict):
        logger.warning("withholding_payload_unreadable", payload_keys=_keys(payload))
        return None
    try:
        groups = tuple(
            WithheldGroup(reason=str(reason), count=int(count))
            for reason, count in by_reason.items()
        )
    except (TypeError, ValueError):
        logger.warning("withholding_payload_unreadable", payload_keys=_keys(payload))
        return None
    return WithholdingSummary(
        groups=groups,
        withheld_item_ids=tuple(_str_list(payload.get("withheld_item_ids"))),
        non_absence_reasons=tuple(_str_list(payload.get("non_absence_reasons"))),
        section_filtered=_count(payload, "section_filtered"),
        served_count=_count(payload, "served_count"),
    )


def format_withholding_note(summary: WithholdingSummary | None) -> str:
    """Markdown lines naming the counts and reasons, or ``""``.

    Empty string when nothing was withheld — a pack that served everything
    it found must not carry a line saying so, or the marker becomes noise
    the reader learns to skip and stops being a signal at all.

    A second line is added for section routing (#440), and only on an empty
    pack: that gate fires on all but one of the reference deployment's packs
    when its two shipped presets are replayed, so an unconditional line
    would *be* the noise this docstring warns about. It is a separate
    sentence rather than a ``by_reason`` entry because it makes a narrower
    claim than the other ten gates — see the module docstring.

    Rendered by the pack formatters into the **header**, above the item
    blocks, never appended after them. The formatters' item loop ``break``\\ s
    when the token budget runs out, so anything appended after it is printed
    only for packs that fit — which is the "honest in JSON alone" failure
    this issue was filed about, one layer down.
    """
    if summary is None:
        return ""
    sentences: list[str] = []
    if summary.groups:
        detail = ", ".join(f"{g.reason} {g.count}" for g in summary.groups)
        singular = summary.total == 1
        noun = "item" if singular else "items"
        verb = "was" if singular else "were"
        sentences.append(
            f"**Withheld:** {summary.total} {noun} matched this intent but {verb} "
            f"not served ({detail}). Counts only — no ids or content."
        )
    if summary.section_note_applies:
        n = summary.section_filtered
        section_noun = "item" if n == 1 else "items"
        sentences.append(
            f"**Section routing:** this pack is empty because {n} {section_noun} "
            "matched this intent but none of the sections you requested — "
            "not because nothing was found. Counts only — no ids or content."
        )
    return "\n".join(sentences)


__all__ = [
    "NON_ABSENCE_REASONS",
    "SECTION_FILTER_REASON",
    "WithheldGroup",
    "WithholdingSummary",
    "format_withholding_note",
    "summarize_withheld",
    "withholding_from_payload",
]
