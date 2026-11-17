"""Pack to scenario: a pack that was served and graded, as a repeatable eval case.

A pack-quality scenario (:class:`~trellis.retrieve.evaluate.EvaluationScenario`)
used to be hand-written. This module turns a real ``PACK_ASSEMBLED`` row and
the ``FEEDBACK_RECORDED`` rows naming it into one, so a request that went badly
(or well) can be replayed through ``trellis analyze pack-quality``. It is
read-only and decides nothing; the caller writes the result where the operator
asked.

**Join.** The pack is the first ``PACK_ASSEMBLED`` event whose ``entity_id`` is
the requested id. Feedback is read with the same rules as the unanswered-intents
report (:func:`~trellis.analyze.unanswered_intents.feedback_pack_id`,
:func:`~trellis.analyze.unanswered_intents.row_verdict`,
:func:`~trellis.analyze.unanswered_intents.latest_verdict`): both families, the
payload ``pack_id`` or a governed row's ``target_id``, and the latest row with a
verdict decides. The feedback scan opens at the earliest exported pack's
assembly time, because a grade cannot precede its pack; its coverage is
reported.

**What is derived, and from what.** Expectations are read only from items the
grader named in ``helpful_item_ids`` *and* the pack served (an id the pack did
not serve is a stray citation, counted and ignored); an item also named
unhelpful is left out and counted. An ungraded pack, and a pack graded only by
the governed family (which names no items), gets no expectations.

* ``expected_shapes``: per ``item_type`` of those items, ``min_count`` = how
  many there were, ``max_count`` unset. :class:`ShapeCompositionScorer` compares
  ``PackItem.item_type``, which is what the row records.
* ``expected_categories``: the distinct ``category`` those rows carry. The row's
  ``category`` is the ``content_tags.content_type`` facet, which
  :class:`BreadthScorer` reads first, so a re-served item scores as it did. An
  item with no category adds none (counted): the scorer's fallbacks (a flat
  ``content_type``, ``document_form``) are not on the row, and guessing them
  would invent a category.
* ``required_coverage``: always empty. The completeness scorer matches
  keywords against excerpts, and ``PACK_ASSEMBLED`` records no excerpt text; a
  keyword drawn from the intent or a title would be an invented term.

**Refused, never exported:** an unknown id, a held-out pack
(:func:`~trellis.core.pack_holdout.is_holdout`: it served nothing by design and
its outcome is sealed), a pack with no intent text, and a sectioned pack
(:func:`~trellis.feedback.attribution.is_sectioned_payload`): its rows are
budgeted per section, a scenario is scored against one flat pack, and reading
them as flat would state a composition the request never asked for.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.analyze.unanswered_intents import (
    Verdict,
    feedback_pack_id,
    latest_verdict,
    row_verdict,
)
from trellis.core.base import TrellisModel
from trellis.core.pack_holdout import is_holdout
from trellis.feedback.attribution import is_sectioned_payload
from trellis.feedback.pack_rows import ServedRow, iter_feedback_rows, iter_served_rows
from trellis.retrieve.evaluate import EvaluationScenario, ShapeConstraint
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventType,
    ScanCoverage,
    scan_events,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event, EventLog

logger = structlog.get_logger(__name__)

VERDICT_ANSWERED = "answered"
VERDICT_UNANSWERED = "unanswered"
VERDICT_UNGRADED = "ungraded"

REFUSED_UNKNOWN = "unknown_pack"
REFUSED_HELD_OUT = "held_out"
REFUSED_NO_INTENT = "no_intent"
REFUSED_SECTIONED = "sectioned"

#: Prefix of every exported scenario name.
NAME_PREFIX = "pack-"

#: Hex digits of the digest appended when two pack ids slug alike.
NAME_DIGEST_CHARS = 8

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")

_MESSAGES: dict[str, str] = {
    REFUSED_UNKNOWN: "no PACK_ASSEMBLED event has this pack id",
    REFUSED_HELD_OUT: (
        "the pack was held out: it served nothing by design and its outcome is sealed"
    ),
    REFUSED_NO_INTENT: "the pack records no intent text, and a scenario needs one",
    REFUSED_SECTIONED: (
        "the pack is sectioned: its rows are budgeted per section and a "
        "scenario is scored against one flat pack"
    ),
}

_LIMIT_STATEMENTS: tuple[str, ...] = (
    (
        "required_coverage is always empty: PACK_ASSEMBLED records no excerpt "
        "text, and a coverage keyword not read from served text would be "
        "invented."
    ),
    (
        "expected_shapes and expected_categories are read from served items a "
        "grader named helpful; an item also named unhelpful, or not served by "
        "the pack, is left out."
    ),
    (
        "A helpful item with no category (no content_tags.content_type facet) "
        "adds no expected category, though the breadth scorer may read a "
        "fallback the event did not record."
    ),
    (
        "Feedback from the CLI and POST /feedback family names no items, so a "
        "pack graded only by it exports a verdict and no expectations."
    ),
    ("An ungraded pack exports with empty expectations; ungraded is not unanswered."),
    (
        "The scenario intent is the request's own text, verbatim; the output "
        "file carries it."
    ),
)


class PackScenarioRefusal(TrellisModel):
    """One requested pack that was not exported, and why."""

    pack_id: str
    reason: str
    message: str


class PackScenarioLimits(TrellisModel):
    """What the export could not see or would not derive, beside what it did."""

    requested: int = 0
    exported: int = 0
    duplicate_pack_ids: int = 0
    unknown_refused: int = 0
    held_out_refused: int = 0
    no_intent_refused: int = 0
    sectioned_refused: int = 0
    ungraded_exported: int = 0
    feedback_events_scanned: int = 0
    feedback_rows_matched: int = 0
    feedback_without_verdict: int = 0
    packs_with_conflicting_verdicts: int = 0
    stray_citations: int = 0
    items_named_both_ways: int = 0
    helpful_items_without_category: int = 0
    helpful_items_without_item_type: int = 0
    statements: list[str] = Field(default_factory=lambda: list(_LIMIT_STATEMENTS))


class PackScenarioExport(TrellisModel):
    """Scenarios for the exportable packs, refusals for the rest."""

    scenarios: list[EvaluationScenario] = Field(default_factory=list)
    refusals: list[PackScenarioRefusal] = Field(default_factory=list)
    limits: PackScenarioLimits = Field(default_factory=PackScenarioLimits)
    scan: ScanCoverage = Field(default_factory=ScanCoverage)


@dataclass
class _Pack:
    pack_id: str
    at: datetime
    intent: str
    domain: str | None
    served: list[ServedRow]
    verdicts: list[Verdict] = field(default_factory=list)
    helpful: set[str] = field(default_factory=set)
    unhelpful: set[str] = field(default_factory=set)


def scenario_name(pack_id: str, taken: set[str]) -> str:
    """A scenario name derived from ``pack_id``, not already in ``taken``.

    ``pack-`` plus the id with every run of characters outside
    ``[A-Za-z0-9._-]`` replaced by ``-``. When that is taken (two ids that slug
    alike) a short digest of the id itself is appended, so the result depends
    only on the id and the ids before it.
    """
    slug = _SLUG.sub("-", pack_id).strip("-") or "pack"
    name = f"{NAME_PREFIX}{slug}"
    if name in taken:
        digest = hashlib.sha256(pack_id.encode("utf-8")).hexdigest()
        name = f"{name}-{digest[:NAME_DIGEST_CHARS]}"
    return name


def export_pack_scenarios(
    event_log: EventLog,
    pack_ids: Sequence[str],
    *,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> PackScenarioExport:
    """Turn served, graded packs into :class:`EvaluationScenario` objects.

    Args:
        event_log: Holds the pack and feedback events.
        pack_ids: The packs to export, in output order; a repeat is exported
            once and counted.
        limit: Cap on the feedback scan; truncation is reported in ``scan``.

    Returns:
        A :class:`PackScenarioExport`. Reads only.
    """
    limits = PackScenarioLimits(requested=len(pack_ids))
    refusals: list[PackScenarioRefusal] = []
    packs: dict[str, _Pack] = {}
    seen: set[str] = set()
    for raw_id in pack_ids:
        pack_id = raw_id.strip()
        if pack_id in seen:
            limits.duplicate_pack_ids += 1
            continue
        seen.add(pack_id)
        reason, pack = _read_pack(event_log, pack_id)
        if reason is not None:
            _count_refusal(limits, reason)
            refusals.append(
                PackScenarioRefusal(
                    pack_id=pack_id, reason=reason, message=_MESSAGES[reason]
                )
            )
            continue
        if pack is not None:
            packs[pack_id] = pack

    scan = ScanCoverage()
    if packs:
        since = min(pack.at for pack in packs.values())
        feedback_scan = scan_events(
            event_log, event_type=EventType.FEEDBACK_RECORDED, since=since, limit=limit
        )
        scan = feedback_scan.coverage
        limits.feedback_events_scanned = len(feedback_scan.events)
        _attach_feedback(packs, feedback_scan.events, limits)

    scenarios: list[EvaluationScenario] = []
    taken: set[str] = set()
    for pack in packs.values():
        scenario = _scenario(pack, taken, limits)
        taken.add(scenario.name)
        scenarios.append(scenario)
    limits.exported = len(scenarios)
    logger.debug(
        "pack_scenarios_exported",
        requested=limits.requested,
        exported=limits.exported,
        refused=len(refusals),
    )
    return PackScenarioExport(
        scenarios=scenarios, refusals=refusals, limits=limits, scan=scan
    )


def _read_pack(event_log: EventLog, pack_id: str) -> tuple[str | None, _Pack | None]:
    if not pack_id:
        return REFUSED_UNKNOWN, None
    events = event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED, entity_id=pack_id, limit=1, order="asc"
    )
    if not events:
        return REFUSED_UNKNOWN, None
    event = events[0]
    payload = event.payload or {}
    if is_holdout(payload):
        return REFUSED_HELD_OUT, None
    intent = payload.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return REFUSED_NO_INTENT, None
    if is_sectioned_payload(payload):
        return REFUSED_SECTIONED, None
    served = list(
        iter_served_rows(event, include_sectioned=False, include_holdout=False)
    )
    return None, _Pack(
        pack_id=pack_id,
        at=_as_utc(event.occurred_at),
        intent=intent,
        domain=_clean(payload.get("domain")),
        served=served,
    )


def _count_refusal(limits: PackScenarioLimits, reason: str) -> None:
    if reason == REFUSED_UNKNOWN:
        limits.unknown_refused += 1
    elif reason == REFUSED_HELD_OUT:
        limits.held_out_refused += 1
    elif reason == REFUSED_NO_INTENT:
        limits.no_intent_refused += 1
    elif reason == REFUSED_SECTIONED:
        limits.sectioned_refused += 1


def _attach_feedback(
    packs: dict[str, _Pack], events: list[Event], limits: PackScenarioLimits
) -> None:
    for index, (event, row) in enumerate(
        zip(events, iter_feedback_rows(events), strict=True)
    ):
        pack = packs.get(feedback_pack_id(row, packs) or "")
        if pack is None:
            continue
        limits.feedback_rows_matched += 1
        pack.helpful.update(row.helpful_item_ids)
        pack.unhelpful.update(row.unhelpful_item_ids)
        verdict = row_verdict(row)
        if verdict is None:
            limits.feedback_without_verdict += 1
            continue
        pack.verdicts.append((_as_utc(event.occurred_at), index, verdict, row.rating))


def _scenario(
    pack: _Pack, taken: set[str], limits: PackScenarioLimits
) -> EvaluationScenario:
    decided = latest_verdict(pack.verdicts)
    if len({answered for _, _, answered, _ in pack.verdicts}) > 1:
        limits.packs_with_conflicting_verdicts += 1
    rating: float | None = None
    categories: list[str] = []
    shapes: dict[str, ShapeConstraint] | None = None
    if decided is None:
        verdict = VERDICT_UNGRADED
        limits.ungraded_exported += 1
    else:
        _, _, answered, rating = decided
        verdict = VERDICT_ANSWERED if answered else VERDICT_UNANSWERED
        categories, shapes = _expectations(pack, limits)
    return EvaluationScenario(
        name=scenario_name(pack.pack_id, taken),
        intent=pack.intent,
        domain=pack.domain,
        expected_categories=categories,
        expected_shapes=shapes,
        metadata={
            "source_pack_id": pack.pack_id,
            "assembled_at": pack.at.isoformat(),
            "verdict": verdict,
            "rating": rating,
            "helpful_item_ids": sorted(pack.helpful),
            "unhelpful_item_ids": sorted(pack.unhelpful),
            "served_items": [
                {"item_id": row.item_id, "item_type": row.item_type}
                for row in pack.served
            ],
        },
    )


def _expectations(
    pack: _Pack, limits: PackScenarioLimits
) -> tuple[list[str], dict[str, ShapeConstraint] | None]:
    served_ids = {row.item_id for row in pack.served if row.item_id}
    limits.stray_citations += len(pack.helpful - served_ids)
    both = pack.helpful & pack.unhelpful & served_ids
    limits.items_named_both_ways += len(both)
    chosen = (pack.helpful & served_ids) - both
    categories: set[str] = set()
    shape_counts: Counter[str] = Counter()
    for row in pack.served:
        if row.item_id not in chosen:
            continue
        category = _clean(row.row.get("category"))
        if category is None:
            limits.helpful_items_without_category += 1
        else:
            categories.add(category)
        if row.item_type is None:
            limits.helpful_items_without_item_type += 1
        else:
            shape_counts[row.item_type] += 1
    shapes = (
        {
            item_type: ShapeConstraint(min_count=count)
            for item_type, count in sorted(shape_counts.items())
        }
        if shape_counts
        else None
    )
    return sorted(categories), shapes


def _clean(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


__all__ = [
    "NAME_PREFIX",
    "REFUSED_HELD_OUT",
    "REFUSED_NO_INTENT",
    "REFUSED_SECTIONED",
    "REFUSED_UNKNOWN",
    "VERDICT_ANSWERED",
    "VERDICT_UNANSWERED",
    "VERDICT_UNGRADED",
    "PackScenarioExport",
    "PackScenarioLimits",
    "PackScenarioRefusal",
    "export_pack_scenarios",
    "scenario_name",
]
