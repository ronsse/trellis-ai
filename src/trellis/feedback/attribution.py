"""What a feedback caller can legitimately cite for a given pack.

Attribution is the join key of the learning loop: ``learning.pack_observations``
matches ``FEEDBACK_RECORDED`` against ``PACK_ASSEMBLED`` on ``pack_id``, then
grades the pack's items by the ``helpful_item_ids`` / ``unhelpful_item_ids``
the caller supplied. Feedback that names no items contributes zero per-item
rows, so it is invisible to the promote half of the loop.

This module answers two narrow membership questions — *which item ids did
this pack actually serve?* and *which of those did it serve with an
excerpt rather than a one-line pointer?* — from the authoritative record,
the pack's own ``PACK_ASSEMBLED`` event. It exists so an agent-facing
surface can hand a caller the real ids instead of the caller
reconstructing them from the rendered markdown it may no longer hold in
context. Both answers come from one read of one event, so "served" and
"bodied" cannot be resolved against different packs.

**It never invents attribution.** The served list is what the pack contained,
not what the agent found useful; the two are different claims and only the
caller can make the second. Nothing here writes to the feedback payload — the
one guarantee is that a caller who wants to cite is not blocked by having lost
the ids. That distinction is the same one
:meth:`trellis.feedback.models.PackFeedback.from_agent_signal` draws when it
deliberately leaves ``items_served`` empty rather than unioning the cited ids.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog

from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Payload key holding the flat list of item ids a pack served.
#: ``PackBuilder`` writes both this and the richer ``injected_items[]``;
#: the flat list is the one the join reads for membership.
_INJECTED_ITEM_IDS = "injected_item_ids"

#: Payload key holding #359's graduated-disclosure summary, written by
#: :meth:`trellis.retrieve.disclosure.DisclosureResult.as_telemetry` and
#: emitted on every flat build — including when nothing was demoted, so
#: "graduation ran and demoted none" is distinguishable from "graduation
#: never ran".
_DISCLOSURE = "disclosure"

#: Key inside that summary naming the served ids replaced by a one-line
#: pointer. These items are in the pack, ranked and fetchable, but the
#: caller never saw their excerpt.
_POINTER_ITEM_IDS = "pointer_item_ids"

#: Payload key marking an index-mode pack (#305). Such a pack is *all*
#: pointers by construction and is exempt from graduation, so its
#: disclosure summary reads ``mode="off"`` with no pointer ids — the one
#: shape where subtracting pointers from served would be exactly wrong.
_INDEX_MODE = "index_mode"


def lookup_pack_item_ids(event_log: EventLog, pack_id: str) -> list[str]:
    """Return the item ids ``pack_id`` served, or ``[]`` when unknown.

    Reads ``PACK_ASSEMBLED.payload['injected_item_ids']`` — the same field
    :func:`trellis.learning.pack_observations.join_pack_feedback` treats as
    the pack's membership list, so "citable" and "joinable" cannot drift.

    Fails soft in every direction. An unknown ``pack_id``, a pack that
    predates the ``injected_item_ids`` payload, a sectioned pack (which
    emits no per-item rows at all), or an event-log outage each yield an
    empty list. Callers must read ``[]`` as *"nothing to offer the caller"*
    and never as *"the pack served nothing"* — the two are indistinguishable
    here by design, because acting on the difference would mean guessing.

    Args:
        event_log: Operational event log holding ``PACK_ASSEMBLED``.
        pack_id: The pack to look up. Blank input short-circuits to ``[]``.

    Returns:
        Item ids in served order, de-duplicated, with falsy entries dropped.
    """
    payload = _pack_payload(event_log, pack_id)
    if payload is None:
        return []
    return _served_item_ids(payload)


def lookup_pack_bodied_item_ids(event_log: EventLog, pack_id: str) -> list[str]:
    """Return the ids ``pack_id`` served *with an excerpt*, or ``[]``.

    The served list minus #359's pointers. Graduated disclosure serves
    the first ``body_items`` items as excerpts and demotes the rest to a
    one-line pointer carrying a label and the withheld size — so a served
    id names two very different deliveries, and only one of them put text
    in front of the caller.

    That distinction is the whole reason this function exists. Measured
    on the reference deployment over 365 days (n=59 attributed packs), a
    bodied item draws *any* per-item verdict at **0.502** against a
    pointer's **0.256**, and a *helpful* verdict at **0.143** against
    **0.023** — bodied winning in 30 of 40 packs that served both. Asking
    a caller to account for every served id therefore spends most of the
    ask on items they were never shown; asking for the bodied half is the
    same question against a denominator the caller can actually answer.
    The confound is stated rather than hidden: pointers are the rank tail
    by construction, so some of that gap is rank, not disclosure.

    Deliberately a sibling of :func:`lookup_pack_item_ids` in this module
    rather than a reader of ``retrieve.disclosure`` from the feedback
    surface. Both membership questions are answered from one read of one
    event, by one module, so "served" and "bodied" cannot drift apart the
    way a reader keyed on a field its writer later moved has drifted here
    repeatedly (#325/#326). What makes that more than a wish is
    ``tests/unit/feedback/test_attribution.py``'s round trip through a
    real :class:`~trellis.retrieve.pack_builder.PackBuilder` emit: a
    hand-written payload fixture would keep passing after the writer
    moved the key, and that is the failure being guarded against.

    Fails soft in the same directions as :func:`lookup_pack_item_ids`,
    and in one more. ``[]`` comes back for an unknown pack, a sectioned
    pack, an outage — and for an **index-mode** pack, whose items are all
    pointers, and for a disclosure record whose shape is not the one this
    reader knows. The bodied set is only ever narrowed by uncertainty,
    never widened: a caller refused for not judging an item they were
    never shown is the expensive mistake here.

    A pack carrying **no** disclosure record is not uncertainty — it
    predates #359 (or was built with graduation off), and every item it
    served carried a body. Those get the full served list.

    Args:
        event_log: Operational event log holding ``PACK_ASSEMBLED``.
        pack_id: The pack to look up. Blank input short-circuits to ``[]``.

    Returns:
        Bodied item ids in served order, de-duplicated.
    """
    payload = _pack_payload(event_log, pack_id)
    if payload is None:
        return []

    served = _served_item_ids(payload)
    if not served or payload.get(_INDEX_MODE) is True:
        # Nothing served, or an index pack — every line of which is a
        # pointer, so there is no bodied half to ask about.
        return []

    pointers = _pointer_item_ids(payload, pack_id=pack_id)
    if pointers is None:
        return []
    return [item_id for item_id in served if item_id not in pointers]


def _pointer_item_ids(payload: Mapping[str, Any], *, pack_id: str) -> set[str] | None:
    """Ids this pack served as one-line pointers, or ``None`` when unknown.

    ``None`` is the fail-soft signal and is not the same claim as the
    empty set: an **empty set** says graduation demoted nothing and every
    served item carried a body, while ``None`` says this reader does not
    recognise the record's shape and must not guess. Callers narrow the
    bodied set to nothing on ``None`` — see
    :func:`lookup_pack_bodied_item_ids`.
    """
    disclosure = payload.get(_DISCLOSURE)
    if not disclosure:
        # Absent or ``{}`` — graduation never ran, so nothing was demoted.
        return set()
    if not isinstance(disclosure, Mapping):
        logger.warning(
            "pack_disclosure_shape_unrecognised",
            pack_id=pack_id,
            field=_DISCLOSURE,
            got=type(disclosure).__name__,
        )
        return None

    raw = disclosure.get(_POINTER_ITEM_IDS)
    if not isinstance(raw, list):
        # ``as_telemetry`` always writes this key, as a list. Anything
        # else means the writer moved and this reader did not — say so
        # at a level a shipped deployment actually prints (the CLI pins
        # ``WARNING``), rather than going quiet on a constant.
        logger.warning(
            "pack_disclosure_shape_unrecognised",
            pack_id=pack_id,
            field=_POINTER_ITEM_IDS,
            got=type(raw).__name__,
        )
        return None

    return {entry for entry in raw if isinstance(entry, str) and entry}


def _pack_payload(event_log: EventLog, pack_id: str) -> dict[str, Any] | None:
    """Newest ``PACK_ASSEMBLED`` payload for ``pack_id``, or ``None``.

    One read behind both membership questions, so a change to how a pack
    is located cannot answer "served" and "bodied" from two different
    events.
    """
    if not pack_id or not pack_id.strip():
        return None

    try:
        events = event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED,
            entity_id=pack_id.strip(),
            limit=1,
            order="desc",
        )
    except Exception:
        # GRACEFUL-DEGRADATION: this is a convenience lookup on a write
        # path. A store outage must not turn a recordable feedback signal
        # into a failed tool call.
        logger.exception("pack_item_lookup_failed", pack_id=pack_id)
        return None

    if not events:
        return None
    return events[0].payload or {}


def _served_item_ids(payload: Mapping[str, Any]) -> list[str]:
    """``injected_item_ids``, de-duplicated in served order."""
    raw = payload.get(_INJECTED_ITEM_IDS)
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    item_ids: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            continue
        if entry in seen:
            continue
        seen.add(entry)
        item_ids.append(entry)
    return item_ids


def payload_is_attributed(payload: dict[str, object]) -> bool:
    """Whether a ``FEEDBACK_RECORDED`` payload carries element attribution.

    One spelling of the predicate, shared by the health analyzer and the
    MCP boundary so "attributed" cannot mean two things in one deployment.
    A followed advisory counts: it is not a pack item, but it is an element
    of the delivery the agent cited, and ``analyze advisory-effectiveness``
    consumes it.
    """
    for key in ("helpful_item_ids", "unhelpful_item_ids", "followed_advisory_ids"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def payload_pack_id(payload: dict[str, object]) -> str:
    """The pack a ``FEEDBACK_RECORDED`` payload targets, or ``""``.

    Read strictly from the top-level ``pack_id`` key —
    :func:`trellis.learning.pack_observations.join_pack_feedback` reads the
    same key and skips the event when it is absent, so this is exactly the
    predicate "could this event ever join to a pack?".
    """
    value = payload.get("pack_id")
    return value.strip() if isinstance(value, str) else ""
