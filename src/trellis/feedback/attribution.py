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

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Payload key holding the flat list of item ids a pack served.
#: ``PackBuilder`` writes both this and the richer ``injected_items[]``;
#: the flat list is the one the join reads for membership.
_INJECTED_ITEM_IDS = "injected_item_ids"

#: Payload key holding the per-item rows a pack served. Richer than
#: :data:`_INJECTED_ITEM_IDS` — each row carries the item's id *and* which
#: strategy served it — and written by the same ``PackBuilder`` emit.
#: :func:`lookup_pack_items_by_strategy` reads it for the strategy; for
#: *membership* it is read only as a fallback by :func:`served_item_ids`,
#: because the two lists have never disagreed on any pack this deployment
#: assembled (219 of 219 flat packs carry both, with identical membership;
#: measured 2026-09-16). A sectioned pack carries neither.
_INJECTED_ITEMS = "injected_items"

#: Payload keys holding the pack's own learning-scope axes. ``PackBuilder``
#: derives ``intent_family`` through
#: :func:`~trellis.learning.scoring.normalize_intent_family` and carries
#: ``domain`` from the request, so the pack is where both acquire a value.
_INTENT_FAMILY = "intent_family"
_DOMAIN = "domain"


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


def _load_pack_payload(event_log: EventLog, pack_id: str) -> dict[str, object]:
    """The ``PACK_ASSEMBLED`` payload for ``pack_id``, or ``{}`` when unknown.

    The shared fail-soft read behind every lookup in this module. An unknown
    pack, a pack that predates the payload key a caller wants, or an
    event-log outage each yield ``{}``; no caller may read that as *"the pack
    served nothing"*.

    One read of one event behind every question this module answers, so a
    change to how a pack is located cannot answer "served", "bodied", "by
    strategy" and "scope" from different events.
    """
    if not pack_id or not pack_id.strip():
        return {}

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
        return {}

    if not events:
        return {}
    return events[0].payload or {}


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
    return _served_item_ids(_load_pack_payload(event_log, pack_id))


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
    payload = _load_pack_payload(event_log, pack_id)
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


def _served_item_ids(payload: Mapping[str, Any]) -> list[str]:
    """``injected_item_ids``, de-duplicated in served order."""
    return _clean_ids(payload.get(_INJECTED_ITEM_IDS))


def lookup_pack_items_by_strategy(
    event_log: EventLog, pack_id: str
) -> dict[str, list[str]]:
    """Return ``{strategy_source: [item_id, ...]}`` for ``pack_id``.

    Reads ``PACK_ASSEMBLED.payload['injected_items']`` — the per-item rows,
    not the flat id list — because only those carry ``strategy_source``, the
    short name the serving strategy stamped on each item it returned.

    **This is the denominator an agent cannot supply.** A ``PackFeedback``
    names what the caller *cited*; only the pack knows what it was *shown*,
    and only the pack knows which strategy showed it. Together they turn one
    pack-level grade into a per-strategy ``items_referenced / items_served``,
    which is the shape :class:`~trellis.learning.tuners.rule_tuner.TuningRule`
    matches on.

    Fails soft exactly as :func:`lookup_pack_item_ids` does, and for the same
    reason: an unknown pack, a sectioned pack (which emits no
    ``injected_items`` at all), a pack older than the payload key, or a store
    outage each yield ``{}``. A caller must read ``{}`` as *"no per-strategy
    breakdown is available"* and never as *"no strategy served anything"* —
    emitting a measured zero off that distinction is the
    :data:`~trellis.schemas.outcome.OutcomeEvent` failure #557 found.

    Rows with no usable ``strategy_source`` are **dropped**, not bucketed
    under a placeholder: an item whose serving strategy is unrecorded must
    not inflate any strategy's denominator. Returned as the raw source names
    the pack wrote — mapping those onto a ``component_id`` is the caller's
    decision and lives in
    :data:`~trellis.schemas.outcome.COMPONENT_ID_BY_SOURCE_STRATEGY`.

    Args:
        event_log: Operational event log holding ``PACK_ASSEMBLED``.
        pack_id: The pack to look up. Blank input short-circuits to ``{}``.

    Returns:
        Served item ids grouped by ``strategy_source``, in served order,
        de-duplicated within each group.
    """
    raw = _load_pack_payload(event_log, pack_id).get(_INJECTED_ITEMS)
    if not isinstance(raw, list):
        return {}

    by_strategy: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        item_id = row.get("item_id")
        strategy = row.get("strategy_source")
        if not isinstance(item_id, str) or not item_id:
            continue
        if not isinstance(strategy, str) or not strategy.strip():
            continue
        key = strategy.strip()
        bucket = by_strategy.setdefault(key, [])
        marks = seen.setdefault(key, set())
        if item_id in marks:
            continue
        marks.add(item_id)
        bucket.append(item_id)
    return by_strategy


class PackScope(NamedTuple):
    """The learning-scope axes a pack recorded about itself.

    Two of the four axes of a
    :class:`~trellis.schemas.outcome.ParameterScope`, read back off the
    pack that the feedback is grading.
    """

    domain: str | None
    """The pack's request domain, or ``None`` when it carried none."""

    intent_family: str | None
    """The family ``PackBuilder`` normalized the request intent into, or
    ``None`` when the pack recorded none."""


#: A pack that recorded neither axis — also what an unknown pack resolves to.
EMPTY_PACK_SCOPE = PackScope(domain=None, intent_family=None)


def lookup_pack_scope(event_log: EventLog, pack_id: str) -> PackScope:
    """Return the ``(domain, intent_family)`` the pack recorded for itself.

    **This is the other half of what an agent cannot supply.**
    :meth:`~trellis.feedback.models.PackFeedback.from_agent_signal` takes
    neither axis and ``PackFeedback`` has no ``domain`` field at all, so on
    every agent-facing surface both arrive empty — measured across 30 days on
    the reference deployment, ``intent_family`` was empty on **72 of 72**
    feedback events while the packs they graded carried **7** distinct
    families, and ``domain`` was empty on all 72 against **8** distinct pack
    values (#560). An ``OutcomeEvent`` built from the feedback alone therefore
    keys every row into one global cell per component, which is narrower
    information than the pack already wrote down.

    The pack is the authoritative source: ``PackBuilder`` derives
    ``intent_family`` via
    :func:`~trellis.learning.scoring.normalize_intent_family` and stamps both
    onto ``PACK_ASSEMBLED``. Three other consumers —
    ``learning.pack_observations``, ``retrieve.pack_value`` and
    ``retrieve.metrics_timeseries`` — each independently built this same
    fallback at their own join; this is that read, once, for the outcome
    bridge.

    Values are returned **verbatim**, normalized only by stripping and by
    mapping blank to ``None``. A pack that recorded no domain is not the same
    as one whose domain is the empty string, and inventing a placeholder
    family here would create a cell that no ``PackBuilder`` ever emits.

    Fails soft exactly as the other lookups here do: an unknown pack, an
    event-log outage, or a pack payload predating either key resolves to
    :data:`EMPTY_PACK_SCOPE`, which leaves the axes at the ``None`` they
    would have had anyway.
    """
    payload = _load_pack_payload(event_log, pack_id)
    if not payload:
        return EMPTY_PACK_SCOPE
    return PackScope(
        domain=_clean_axis(payload.get(_DOMAIN)),
        intent_family=_clean_axis(payload.get(_INTENT_FAMILY)),
    )


def _clean_axis(raw: object) -> str | None:
    """A scope axis as a non-blank string, or ``None``.

    Blank and non-string both resolve to ``None`` — an axis is a cell key,
    and a key of ``""`` is a *distinct* cell from "unscoped" that nothing
    else in the loop produces.
    """
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


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
