"""Pack holdout: which assembled packs are withheld, and how readers tell.

Measuring what a pack is worth needs packs that were *not* served, chosen
by a coin the agent cannot see.  With ``TRELLIS_PACK_HOLDOUT_RATE`` above
zero (:data:`trellis.core.write_config.PACK_HOLDOUT_RATE_ENV`),
:class:`~trellis.retrieve.pack_builder.PackBuilder` assembles every pack
as usual and then draws: a withheld pack reaches the caller as an ordinary
empty pack, items and advisories alike, and its ``PACK_ASSEMBLED`` row
records the would-be pack under the ``holdout_*`` keys instead of as
served.

**The assignment is per pack, from the pack id.**  It is drawn after the
pack is built, so the would-be pack is known, and it is a pure function of
``(pack_id, rate)`` — SHA-256 under a fixed domain prefix, never
:func:`hash`, which ``PYTHONHASHSEED`` salts per process — so any process
on any Python version recomputes the same arm for a recorded pack.

**Readers analyse the served arm.**  Every ``PACK_ASSEMBLED`` row carries
``holdout`` (a bool) and ``holdout_rate`` (the rate in force), so a row
from a build that predates the flag reads as served by absence.  Readers
that aggregate over packs drop holdout packs, and the feedback that names
them, with :func:`drop_holdout`.  Surfaces that return a row to a caller
without the ``admin`` scope pass it through :func:`blind_holdout` first.

Standard library only: aggregate readers in ``trellis.learning`` and
``trellis.ops`` import this, and ``trellis.retrieve`` imports them back.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event

#: Domain prefix for the assignment hash.  Changing it re-randomises every
#: pack already recorded, so it is versioned rather than edited.
HOLDOUT_HASH_DOMAIN = b"trellis/pack-holdout/v1:"

#: ``PACK_ASSEMBLED`` payload keys.  ``holdout`` and ``holdout_rate`` are on
#: every row the builder emits; the rest only on a withheld pack's row.
HOLDOUT_KEY = "holdout"
HOLDOUT_RATE_KEY = "holdout_rate"
HOLDOUT_ITEMS_KEY = "holdout_items"
HOLDOUT_SECTIONS_KEY = "holdout_sections"
HOLDOUT_ADVISORY_IDS_KEY = "holdout_advisory_ids"

_DRAW_BITS = 53  # a double's mantissa: every draw is exactly representable


def holdout_draw(pack_id: str) -> float:
    """The pack's assignment draw, uniform on ``[0, 1)``.

    The top 53 bits of ``sha256(HOLDOUT_HASH_DOMAIN + pack_id)``, scaled by
    ``2**-53``: deterministic across processes, hosts and Python versions.
    """
    digest = hashlib.sha256(HOLDOUT_HASH_DOMAIN + pack_id.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") >> (64 - _DRAW_BITS)) / float(
        1 << _DRAW_BITS
    )


def is_held_out(pack_id: str, rate: float) -> bool:
    """Whether the pack is withheld at ``rate``: its draw falls below it.

    Strictly below, so rate ``0`` withholds nothing, not even a pack whose
    draw is exactly ``0.0``, and rate ``1`` withholds everything.
    """
    return holdout_draw(pack_id) < rate


def is_holdout(payload: Mapping[str, Any] | None) -> bool:
    """Whether a ``PACK_ASSEMBLED`` payload records a withheld pack.

    Only a literal ``True`` counts: a row without the key comes from a build
    that predates the flag and was served.
    """
    return payload is not None and payload.get(HOLDOUT_KEY) is True


def _feedback_pack_key(event: Event) -> str | None:
    """The pack a feedback event grades, by either spelling readers use."""
    value = (event.payload or {}).get("pack_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return event.entity_id


def drop_holdout(
    pack_events: Iterable[Event], feedback_events: Iterable[Event]
) -> tuple[list[Event], list[Event]]:
    """Drop withheld packs, and the feedback that names them, before a join.

    An empty pack an agent received because of the draw is not evidence
    about retrieval: counted, it would dilute every pack-level rate and
    land in the "without" arm of every advisory.  Feedback is matched on
    its payload ``pack_id`` or, failing that, its ``entity_id`` — the two
    keys the readers join on.  With no withheld pack in the window both
    inputs come back unchanged.
    """
    served: list[Event] = []
    held: set[str] = set()
    for event in pack_events:
        if is_holdout(event.payload):
            if event.entity_id:
                held.add(event.entity_id)
        else:
            served.append(event)
    if not held:
        return served, list(feedback_events)
    return served, [
        event for event in feedback_events if _feedback_pack_key(event) not in held
    ]


def unscanned_pack_ids(
    pack_events: Iterable[Event], feedback_events: Iterable[Event]
) -> set[str]:
    """The packs the feedback names that ``pack_events`` holds no row for.

    :func:`drop_holdout` drops feedback on a withheld pack only when that
    pack's row is among the pack events it is given.  A windowed or capped
    scan lacks the row of a pack assembled before the window opened, or
    pushed past the cap, while the feedback naming it is still in the
    feedback scan.  A reader looks these ids up and passes any withheld
    row on.  Feedback is keyed as :func:`drop_holdout` keys it.
    """
    scanned = {event.entity_id for event in pack_events if event.entity_id}
    return {
        key
        for event in feedback_events
        if (key := _feedback_pack_key(event)) and key not in scanned
    }


def blind_holdout(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A ``PACK_ASSEMBLED`` payload as a caller without ``admin`` reads it.

    A withheld row's would-be pack is what the draw kept from the agent,
    and a caller who can read it gets the pack back.  So every
    ``holdout_*`` key goes except ``holdout_rate``, and a would-be key
    added later is hidden by default.  ``holdout`` stays, so the arm is
    still readable.  Returns a copy.  A payload with no would-be key comes
    back equal.
    """
    return {
        key: value
        for key, value in payload.items()
        if not key.startswith("holdout_") or key == HOLDOUT_RATE_KEY
    }


__all__ = [
    "HOLDOUT_ADVISORY_IDS_KEY",
    "HOLDOUT_HASH_DOMAIN",
    "HOLDOUT_ITEMS_KEY",
    "HOLDOUT_KEY",
    "HOLDOUT_RATE_KEY",
    "HOLDOUT_SECTIONS_KEY",
    "blind_holdout",
    "drop_holdout",
    "holdout_draw",
    "is_held_out",
    "is_holdout",
    "unscanned_pack_ids",
]
