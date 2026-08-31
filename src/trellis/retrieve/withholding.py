"""Withholding as a stated fact — a pack must say what it did not serve.

``PackBuilder`` removes candidates for ten distinct reasons before a pack is
returned, and until now a caller could not tell **"this layer was empty"**
from **"this layer was redacted."** Both render as fewer items, or as *"No
context found for: …"*, which reads as greenfield.

**This module changes nothing about what is withheld.** It reports it.
Every gate keeps its own rule, its own default and its own escape hatch; the
only new behaviour is a line of text.

What already existed, and what did not
--------------------------------------

Most of the *data* was there. ``PACK_ASSEMBLED.payload["rejected_items"]``
carries a per-item row for eight of the ten gates, ``budget_trace[]`` prices
every candidate the walk saw including the rejected ones (#359 replays on
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

    @property
    def total(self) -> int:
        """Distinct items absent from the pack that a gate removed."""
        return sum(g.count for g in self.groups)

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
    for reason in first_reason.values():
        counts[reason] = counts.get(reason, 0) + 1

    groups = tuple(
        WithheldGroup(reason=reason, count=count)
        for reason, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return WithholdingSummary(
        groups=groups,
        withheld_item_ids=tuple(first_reason),
        non_absence_reasons=tuple(sorted(non_absence)),
    )


def _keys(payload: Any) -> list[str]:
    """Field names of an unreadable payload — never its values."""
    return sorted(str(k) for k in payload) if isinstance(payload, dict) else []


def _str_list(value: Any) -> list[str]:
    """Coerce a telemetry list field, tolerating absence."""
    return [str(v) for v in value] if isinstance(value, list) else []


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
    )


def format_withholding_note(summary: WithholdingSummary | None) -> str:
    """One markdown line naming the count and reasons, or ``""``.

    Empty string when nothing was withheld — a pack that served everything
    it found must not carry a line saying so, or the marker becomes noise
    the reader learns to skip and stops being a signal at all.

    Rendered by the pack formatters into the **header**, above the item
    blocks, never appended after them. The formatters' item loop ``break``\\ s
    when the token budget runs out, so anything appended after it is printed
    only for packs that fit — which is the "honest in JSON alone" failure
    this issue was filed about, one layer down.
    """
    if summary is None or not summary.groups:
        return ""
    detail = ", ".join(f"{g.reason} {g.count}" for g in summary.groups)
    noun = "item" if summary.total == 1 else "items"
    return (
        f"**Withheld:** {summary.total} {noun} matched this intent but were "
        f"not served ({detail}). Counts only — no ids or content."
    )


__all__ = [
    "NON_ABSENCE_REASONS",
    "WithheldGroup",
    "WithholdingSummary",
    "format_withholding_note",
    "summarize_withheld",
    "withholding_from_payload",
]
