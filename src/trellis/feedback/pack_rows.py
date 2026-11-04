"""One typed reader for ``PACK_ASSEMBLED`` and ``FEEDBACK_RECORDED`` rows.

Every analysis that asks "what did this pack serve" or "what did this grader
say" used to re-derive it from the raw payload, and each copy decided for
itself whether to skip a sectioned pack, whether to drop a held-out pack and
what a malformed row means. A reader that forgot one of the two policies read
as correct until the day a payload of the forgotten shape arrived. This module
is the single place the shapes are parsed, and the two policies are required
keyword arguments with no default, so a caller states them rather than
inheriting someone else's.

**Served rows.** :func:`iter_served_rows` (an event) and
:func:`served_rows_from_payload` (a payload already in hand, as the
``join_pack_feedback`` family returns it) walk ``injected_items[]`` and yield
one :class:`ServedRow` per mapping row, in payload order.

* ``include_sectioned`` — a sectioned pack (``build_sectioned``, recognised by
  its ``sections`` list, :func:`~trellis.feedback.attribution.is_sectioned_payload`)
  emits ``injected_items[]`` since #958, each row adding ``section`` and a
  ``rank`` that is 1-based *within that section*. ``False`` yields nothing for
  such a pack, for a reader built over one flat budget (a replay that re-ranks
  it, a yield rate over one ``rejected_items`` list). ``True`` reads the rows
  as a flat pack's, which is right for a per-item join keyed on
  ``(pack_id, item_id)``.
* ``include_holdout`` — a withheld pack (``holdout: true``,
  :func:`~trellis.core.pack_holdout.is_holdout`) is blind by design. ``False``
  yields nothing for it, whatever its payload holds. ``True`` reads
  ``injected_items[]`` as written, which the builder leaves empty on a
  withheld pack (the would-be rows ride ``holdout_items[]``, which this module
  never reads), so ``True`` is for a caller whose pack source has already
  dropped withheld packs with
  :func:`~trellis.core.pack_holdout.drop_holdout`, or one that wants the
  record exactly as written.

A row is yielded even when it names no usable ``item_id`` (``item_id`` is then
``None``): whether such a row counts is the caller's question, and several
callers count it. Non-mapping rows are skipped.

**Feedback rows.** :func:`feedback_row` / :func:`iter_feedback_rows` read both
``FEEDBACK_RECORDED`` families (CLAUDE.md, "Feedback path"): the full
``PackFeedback.to_event_payload()`` and the governed
``{target_id, rating, comment, success}`` the executor writes. The attribution
fields of the governed family are empty (or ``None``), never invented.

Only the fields a reader uses are typed. Everything a single reader wants
beyond them (``graph_selection``, ``document_form``, ``title``, ...) stays on
:attr:`ServedRow.row`, the verbatim payload row, so a field is promoted here
when a second reader needs it and not before.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from trellis.core.pack_holdout import is_holdout
from trellis.feedback.attribution import (
    _clean_ids,
    is_sectioned_payload,
    payload_pack_id,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event

#: The ``PACK_ASSEMBLED`` payload key this module is the one reader of.
INJECTED_ITEMS_KEY = "injected_items"


@dataclass(frozen=True, slots=True)
class ServedRow:
    """One item a pack served, as ``PACK_ASSEMBLED.injected_items[]`` recorded it.

    Every typed field is ``None`` when the row lacks it or holds another type
    there; the writers (``PackBuilder._injected_item_rows``) always write the
    types below, so ``None`` means a row from outside that writer.
    """

    pack_id: str
    item_id: str | None
    item_type: str | None
    #: 1-based position in the pack, or within its section when ``section`` is set.
    rank: int | None
    #: The serving section's name; ``None`` on a flat pack's row.
    section: str | None
    strategy_source: str | None
    estimated_tokens: int | float | None
    #: The verbatim payload row, for the fields only one reader uses.
    row: Mapping[str, Any]


def _row_from(pack_id: str, raw: Mapping[str, Any]) -> ServedRow:
    item_id = raw.get("item_id")
    item_type = raw.get("item_type")
    rank = raw.get("rank")
    section = raw.get("section")
    strategy = raw.get("strategy_source")
    tokens = raw.get("estimated_tokens")
    return ServedRow(
        pack_id=pack_id,
        item_id=item_id if isinstance(item_id, str) else None,
        item_type=item_type if isinstance(item_type, str) else None,
        rank=rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
        section=section if isinstance(section, str) else None,
        strategy_source=strategy if isinstance(strategy, str) else None,
        estimated_tokens=tokens if isinstance(tokens, int | float) else None,
        row=raw,
    )


def has_served_rows(payload: Mapping[str, Any]) -> bool:
    """Whether a ``PACK_ASSEMBLED`` payload carries any per-item rows.

    The "attributable" test: a verdict joins to a pack only through these
    rows. Truthiness of the raw list, exactly as the callers it replaced
    wrote it, so an empty list and an absent key both read ``False``.
    """
    return bool(payload.get(INJECTED_ITEMS_KEY))


def served_rows_from_payload(
    pack_id: str,
    payload: Mapping[str, Any],
    *,
    include_sectioned: bool,
    include_holdout: bool,
) -> Iterator[ServedRow]:
    """The rows ``payload`` served, under the caller's stated policy.

    See the module docstring for what ``include_sectioned`` and
    ``include_holdout`` mean. ``pack_id`` is stamped on every row as given,
    because a payload does not carry its own pack id (it rides the event's
    ``entity_id``).
    """
    if not include_holdout and is_holdout(payload):
        return
    if not include_sectioned and is_sectioned_payload(payload):
        return
    rows = payload.get(INJECTED_ITEMS_KEY)
    if not isinstance(rows, Sequence) or isinstance(rows, str | bytes):
        return
    for raw in rows:
        if isinstance(raw, Mapping):
            yield _row_from(pack_id, raw)


def iter_served_rows(
    event: Event, *, include_sectioned: bool, include_holdout: bool
) -> Iterator[ServedRow]:
    """The rows a ``PACK_ASSEMBLED`` event served; ``pack_id`` is its ``entity_id``.

    An event without an ``entity_id`` stamps ``""``: a pack that cannot be
    named cannot be joined, but its rows are still its rows.
    """
    return served_rows_from_payload(
        event.entity_id or "",
        event.payload or {},
        include_sectioned=include_sectioned,
        include_holdout=include_holdout,
    )


class FeedbackFamily(StrEnum):
    """Which writer produced a ``FEEDBACK_RECORDED`` payload.

    A fact about the writer, read off the payload's discriminators and never
    inferred from content: ``feedback_id`` is emitted by
    ``PackFeedback.to_event_payload()`` and never by the governed surface
    (``PackFeedback.from_event_payload`` relies on the same split).
    """

    #: ``PackFeedback.to_event_payload()`` — MCP ``record_feedback``, REST
    #: ``POST /packs/{pack_id}/feedback``, ``TrellisClient.record_feedback``.
    PACK_FEEDBACK = "pack_feedback"
    #: ``Command(FEEDBACK_RECORD)`` through ``MutationExecutor`` — CLI
    #: ``trellis curate feedback``, REST ``POST /feedback``.
    GOVERNED = "governed"
    #: Neither discriminator is present.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FeedbackRow:
    """One ``FEEDBACK_RECORDED`` payload, normalised across both families."""

    family: FeedbackFamily
    #: The payload's own ``pack_id`` (stripped), or ``None`` when it names none.
    #: Never falls back to the event's ``entity_id``: on the governed family
    #: that is a ``target_id``, which may be a trace.
    pack_id: str | None
    #: Full family only.
    feedback_id: str | None
    rating: float | None
    success: bool | None
    intent_family: str | None
    #: Governed family only: the ``target_id`` the grade names.
    target_id: str | None
    #: Attribution. Empty for the governed family, which carries none.
    helpful_item_ids: tuple[str, ...]
    unhelpful_item_ids: tuple[str, ...]
    followed_advisory_ids: tuple[str, ...]
    #: ``None`` is "no ignored claim made", which is not an empty claim.
    ignored_item_ids: tuple[str, ...] | None


def _family_of(payload: Mapping[str, Any]) -> FeedbackFamily:
    feedback_id = payload.get("feedback_id")
    if isinstance(feedback_id, str) and feedback_id:
        return FeedbackFamily.PACK_FEEDBACK
    if "target_id" in payload:
        return FeedbackFamily.GOVERNED
    return FeedbackFamily.UNKNOWN


def feedback_row(event: Event) -> FeedbackRow:
    """Normalise one ``FEEDBACK_RECORDED`` event; never raises on a payload shape."""
    payload = event.payload or {}
    family = _family_of(payload)
    rating = payload.get("rating")
    success = payload.get("success")
    intent_family = payload.get("intent_family")
    target = payload.get("target_id")
    feedback_id = payload.get("feedback_id")
    ignored = payload.get("ignored_item_ids")
    return FeedbackRow(
        family=family,
        pack_id=payload_pack_id(payload) or None,
        feedback_id=(feedback_id if family is FeedbackFamily.PACK_FEEDBACK else None),
        rating=(
            float(rating)
            if isinstance(rating, int | float) and not isinstance(rating, bool)
            else None
        ),
        success=success if isinstance(success, bool) else None,
        intent_family=intent_family if isinstance(intent_family, str) else None,
        target_id=target if isinstance(target, str) else None,
        helpful_item_ids=tuple(_clean_ids(payload.get("helpful_item_ids"))),
        unhelpful_item_ids=tuple(_clean_ids(payload.get("unhelpful_item_ids"))),
        followed_advisory_ids=tuple(_clean_ids(payload.get("followed_advisory_ids"))),
        ignored_item_ids=(
            tuple(_clean_ids(ignored)) if isinstance(ignored, list) else None
        ),
    )


def iter_feedback_rows(events: Iterable[Event]) -> Iterator[FeedbackRow]:
    """:func:`feedback_row` over every event, in order, one row per event.

    Does not filter on event type or on holdout: the caller scanned the type,
    and the held-out join is :func:`~trellis.core.pack_holdout.drop_holdout`'s
    (it needs the pack events too, which one feedback row cannot see).
    """
    for event in events:
        yield feedback_row(event)


__all__ = [
    "INJECTED_ITEMS_KEY",
    "FeedbackFamily",
    "FeedbackRow",
    "ServedRow",
    "feedback_row",
    "has_served_rows",
    "iter_feedback_rows",
    "iter_served_rows",
    "served_rows_from_payload",
]
