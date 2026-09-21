"""What a feedback caller can legitimately cite for a given pack.

Attribution is the join key of the learning loop: ``learning.pack_observations``
matches ``FEEDBACK_RECORDED`` against ``PACK_ASSEMBLED`` on ``pack_id``, then
grades the pack's items by the ``helpful_item_ids`` / ``unhelpful_item_ids``
the caller supplied. Feedback that names no items contributes zero per-item
rows, so it is invisible to the promote half of the loop.

This module answers one narrow question — *which item ids did this pack
actually serve?* — from the authoritative record, the pack's own
``PACK_ASSEMBLED`` event. It exists so an agent-facing surface can hand a
caller the real ids instead of the caller reconstructing them from the
rendered markdown it may no longer hold in context.

**It never invents attribution.** The served list is what the pack contained,
not what the agent found useful; the two are different claims and only the
caller can make the second. Nothing here writes to the feedback payload — the
one guarantee is that a caller who wants to cite is not blocked by having lost
the ids. That distinction is the same one
:meth:`trellis.feedback.models.PackFeedback.from_agent_signal` draws when it
deliberately leaves ``items_served`` empty rather than unioning the cited ids.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
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

#: Payload key holding the richer per-item rows. Read only as a fallback:
#: ``PackBuilder`` writes it beside ``_INJECTED_ITEM_IDS`` and the two have
#: never disagreed on any pack this deployment assembled (219 of 219 flat
#: packs carry both, with identical membership; measured 2026-09-16). A
#: sectioned pack carries neither.
_INJECTED_ITEMS = "injected_items"

#: The payload keys through which a grader names a *pack item*.
#: ``followed_advisory_ids`` is deliberately absent: an advisory is an
#: element of the delivery but never a member of ``injected_item_ids``, so
#: including it would register a stray on every advisory-carrying pack —
#: counting a working surface as a defect.
CITED_ID_KEYS = ("helpful_item_ids", "unhelpful_item_ids")

#: Fallback key for an id that carries no namespace prefix — a bare ULID
#: written by ``save_memory`` / document ingest. Spelled the same as the
#: other axes' unknown bucket so a reader meets one convention.
NO_NAMESPACE = "(none)"

#: A leading ``<namespace>:`` on a pack item id. Anchored lowercase so an
#: uppercase Crockford ULID (``01KZDAAG...``) cannot match, and bounded so
#: a pathological id cannot mint a 200-character axis key. Only the FIRST
#: segment is taken, which is what makes ``artifact:https://example/x`` and
#: ``conversation:claude-ai:abc#chunk-0`` land in ``artifact`` and
#: ``conversation`` rather than in a bucket of one.
_NAMESPACE_RE = re.compile(r"^([a-z][a-z0-9_-]{0,31}):")

#: A stray whose id is a served id of the same pack with one extra
#: ``<namespace>:`` segment on the front — a grader stamping the item's
#: *type* onto its id, e.g. citing ``entity:trace:X`` for a served
#: ``trace:X``.
STRAY_PREFIX_ADDED = "prefix_added"

#: The mirror: a served id of the same pack with its leading segment(s)
#: removed, e.g. citing ``af366b3f2378948d`` for a served
#: ``capture:claude-code:af366b3f2378948d``.
STRAY_PREFIX_DROPPED = "prefix_dropped"

#: Neither — the cited id has no namespace-variant among the ids this
#: pack served. Only this shape is evidence the grader named something
#: the pack genuinely never carried.
STRAY_FOREIGN = "foreign"


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
    if not pack_id or not pack_id.strip():
        return []

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
        return []

    if not events:
        return []

    return _clean_ids((events[0].payload or {}).get(_INJECTED_ITEM_IDS))


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


def item_namespace(item_id: str) -> str:
    """The namespace prefix an item id carries, or :data:`NO_NAMESPACE`.

    **Why this axis exists.** ``by_item_type`` reads
    ``PackItem.item_type``, and every row the graph strategy produces
    carries the same one — ``"entity"``. So that axis cannot separate a
    name-only stub minted from a trace (``artifact:src/foo.py``, whose
    excerpt *is* the path) from a real curated entity, which is precisely
    the distinction issue #298 is about. Measured on the reference
    deployment those three populations differ by more than an order of
    magnitude in citation rate while sharing one ``item_type``, so the
    existing axis reported their average and nothing else.

    The namespace is read off the id rather than off any stored field
    because it is the one discriminator that is already present on every
    item, in the event log, retroactively — no backfill, no new write
    path, and it prices windows that closed before this function existed.
    That is also what lets it partition a **stray** citation, whose whole
    defining property is that no stored row exists to read a field from.

    It is a *description of the id*, not a classification of the content:
    an id with no prefix is reported as :data:`NO_NAMESPACE`, never
    guessed at.

    Lives here rather than in :mod:`trellis.retrieve.pack_value`, where it
    was introduced, because the learning join and the health surface need
    the same vocabulary and both sit below ``retrieve`` in the import
    graph. ``pack_value`` re-exports it.
    """
    match = _NAMESPACE_RE.match(item_id)
    return match.group(1) if match else NO_NAMESPACE


def _clean_ids(values: Any) -> list[str]:
    """Non-empty strings from a payload list, de-duplicated, in order."""
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for entry in values:
        if not isinstance(entry, str) or not entry or entry in seen:
            continue
        seen.add(entry)
        cleaned.append(entry)
    return cleaned


def served_item_ids(pack_payload: Mapping[str, Any]) -> set[str]:
    """Every item id a ``PACK_ASSEMBLED`` payload records as served.

    The payload-level twin of :func:`lookup_pack_item_ids`, for a caller
    that already holds the event and must not re-query per pack. Both read
    ``injected_item_ids`` first, so what an agent is told it may cite and
    what the analysis surfaces count as served cannot drift apart.

    An empty set means *"no record of what this pack served"*, never
    *"the pack served nothing"* — the same deliberate ambiguity
    :func:`lookup_pack_item_ids` documents, and the reason a caller must
    check for emptiness before subtracting anything from it.
    """
    served = set(_clean_ids(pack_payload.get(_INJECTED_ITEM_IDS)))
    if served:
        return served
    rows = pack_payload.get(_INJECTED_ITEMS)
    if not isinstance(rows, list):
        return set()
    return {
        row["item_id"]
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("item_id"), str)
        and row["item_id"]
    }


def cited_item_ids(feedback_payload: Mapping[str, Any]) -> list[str]:
    """Pack item ids a ``FEEDBACK_RECORDED`` payload names, first-seen order.

    Helpful and unhelpful united, because the question here is membership
    and not verdict: both are claims *about an item the pack served*. An id
    named in both lists is one citation, not two.
    """
    seen: set[str] = set()
    cited: list[str] = []
    for key in CITED_ID_KEYS:
        for item_id in _clean_ids(feedback_payload.get(key)):
            if item_id in seen:
                continue
            seen.add(item_id)
            cited.append(item_id)
    return cited


def stray_citations(
    feedback_payload: Mapping[str, Any], served: Collection[str]
) -> list[str]:
    """Cited ids the pack never served, in first-seen order.

    A stray is a **verdict that cannot be joined** — the grader spoke and
    the loop has nowhere to put it. That is the mirror image of #550's
    *served item that got no verdict*, and the two must not be conflated:
    one is missing signal, the other is discarded signal.

    Callers must skip a pack whose ``served`` set is empty. Emptiness means
    the pack's membership is unrecorded (a sectioned pack, or one older
    than the payload key), and subtracting from it would report every
    citation on it as a stray.
    """
    lookup = set(served)
    return [
        item_id for item_id in cited_item_ids(feedback_payload) if item_id not in lookup
    ]


def stray_shape(item_id: str, served: Collection[str]) -> str:
    """How a stray id relates to the ids its own pack did serve.

    Issue #574 asks for the strays to be partitioned by shape once they
    are counted, and this is that partition: :data:`STRAY_PREFIX_ADDED`,
    :data:`STRAY_PREFIX_DROPPED` or :data:`STRAY_FOREIGN`.

    **This describes the string; it never joins on it.** A shape is
    reported as a count and nothing reads it to decide that two ids are
    the same row — ``entity:trace:X`` and ``trace:X`` *looking* related
    is not the payload saying they are, and this module's rule is that it
    never invents attribution. The partition exists so an operator can
    see that a loss is a caller-side id-spelling defect rather than a
    retrieval one, and fix it where it happens.

    ``prefix_added`` is tested first because it is the stricter claim —
    an exact match on the remainder, against ``prefix_dropped``'s suffix
    match — so an id satisfying both is reported as the more specific.

    Args:
        item_id: A cited id already known to be absent from ``served``.
        served: The ids the same pack served.

    Returns:
        One of the three ``STRAY_*`` constants.
    """
    served_lookup = set(served)
    head, sep, tail = item_id.partition(":")
    if sep and head and tail in served_lookup:
        return STRAY_PREFIX_ADDED
    suffix = f":{item_id}"
    if item_id and any(entry.endswith(suffix) for entry in served_lookup):
        return STRAY_PREFIX_DROPPED
    return STRAY_FOREIGN


@dataclass
class StrayCitationTally:
    """Cited ids no pack served, counted once for every surface that reports them.

    ``analyze health``, ``analyze value`` and the learning join all need
    this number, and three independent subtractions is how one deployment
    ends up reporting three different stray counts for one window.

    The unit is one **(feedback event, cited id)** pair. Two graders naming
    the same stray id on one pack are two unjoinable verdicts, not one —
    the same per-serving convention ``parent_concentration`` and
    ``pack_replay`` use, and for the same reason: a per-distinct-id count
    lets one pack's loss mask another's.

    Two partitions are kept. ``by_namespace`` reads the id alone, which
    is the only field available: a stray by definition resolves to no
    stored row. ``by_shape`` reads it against what the same pack served,
    which is what separates a caller mis-spelling an id it *was* given
    from a caller naming something the pack never carried — see
    :func:`stray_shape`.
    """

    #: Cited ids on feedback that joined to a pack with a recorded
    #: membership. The denominator; not every cited id in the window.
    cited: int = 0
    #: Of those, the ones the pack never served.
    stray: int = 0
    #: Stray count per :func:`item_namespace` of the cited id.
    by_namespace: dict[str, int] = field(default_factory=dict)
    #: Stray count per :func:`stray_shape`, against the pack's own ids.
    by_shape: dict[str, int] = field(default_factory=dict)
    #: Packs contributing at least one citation, and at least one stray.
    packs_cited: set[str] = field(default_factory=set)
    packs_with_stray: set[str] = field(default_factory=set)

    def add(
        self,
        feedback_payload: Mapping[str, Any],
        served: Collection[str],
        *,
        pack_id: str,
    ) -> list[str]:
        """Fold in one joined feedback event; returns the strays it named.

        A pack with an empty ``served`` set is skipped whole — including
        its citations, which would otherwise inflate the denominator with
        a pack that could never contribute a stray.
        """
        served_lookup = set(served)
        if not served_lookup:
            return []
        cited = cited_item_ids(feedback_payload)
        if not cited:
            return []
        self.cited += len(cited)
        self.packs_cited.add(pack_id)
        strays = [item_id for item_id in cited if item_id not in served_lookup]
        for item_id in strays:
            self.stray += 1
            namespace = item_namespace(item_id)
            self.by_namespace[namespace] = self.by_namespace.get(namespace, 0) + 1
            shape = stray_shape(item_id, served_lookup)
            self.by_shape[shape] = self.by_shape.get(shape, 0) + 1
        if strays:
            self.packs_with_stray.add(pack_id)
        return strays

    @property
    def stray_rate(self) -> float:
        """Strays as a share of citations, or ``0.0`` when nothing cited.

        Zero on an empty window reads as *"no evidence"*, not as *"no
        strays"* — read it beside :attr:`cited`, never alone.
        """
        return round(self.stray / self.cited, 4) if self.cited else 0.0
