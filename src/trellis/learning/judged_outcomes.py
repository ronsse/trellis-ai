"""What happened downstream of a judged memory operation (corpus B1).

``memory_op.judged`` records the system's own decision — a classification
label, a distillation ``keep`` — with a ``subject_ref`` naming what was
decided about. :mod:`trellis.schemas.memory_op` defines the whole record as
*(input context, decision, downstream outcome)* and says the outcome join
lands separately (#263). It never landed, and nothing in ``src/`` read the
stream, so its density — 1,880 judged rows against 79 feedback events in the
30 days to 2026-09-13 — was a count of decisions with no outcome attached.

This module is that join, and it is read-only. Each judged row follows
``subject_ref.ref_id`` into ``PACK_ASSEMBLED.injected_items[]`` (servings)
and from there into ``FEEDBACK_RECORDED`` (citations), and lands on exactly
one rung:

===================  ==========================================================
``unservable``       matched no serving, and its ``ref_type`` names something
                     no pack can serve (a distillation ``discard`` refs the
                     *session* it refused; there is no document)
``never_served``     no pack served the subject at or after the judgment
``served_ungraded``  served, but no pack that served it got a per-item verdict
``graded_uncited``   served in a pack whose grader cited other items only
``cited_unhelpful``  named in a serving pack's ``unhelpful_item_ids``
``cited_helpful``    named in a serving pack's ``helpful_item_ids``
===================  ==========================================================

A row keeps the best rung any of its servings reached, and helpful wins over
unhelpful — the rule :mod:`trellis.retrieve.pack_value` applies — so the rungs
partition the rows and sum to ``judged``.

**Matching is containment.** A long document is one parent row plus
``<parent>#chunk-N`` rows, each independently servable. A judgment about the
parent covers every chunk of it, so a parent subject matches its chunks'
servings; a judgment about one chunk says nothing about its siblings or its
parent, so a chunk subject matches only itself. ``ref_type`` is reported and
never matched on, because writers spell a document both ``doc`` and
``document``.

**Order matters, and both orders are reported.** The rungs count only
servings at or after the judgment, since an outcome cannot precede the
decision it grades. The ``*_any_order`` counters drop that constraint, and
``served_before_only`` counts rows whose every serving came first — a
document that was already being served when it was judged.

**The Phase 1 gate** asks for more than :data:`PHASE_1_GATE` usable graded
rows per 30 days, where graded means joined to an outcome rather than merely
emitted. It is decided on the strict reading — rows cited downstream of their
judgment — and every looser reading is reported beside it, so the verdict can
be audited rather than taken on trust. ``never_served`` and ``unservable``
rows are counted and excluded from every reading: a judgment nothing served is
evidence about the judge, not a label on a memory, and mixing the two is how
the gate gets re-litigated.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.ingest_corpus.models import CHUNK_ID_SEPARATOR
from trellis.learning.pack_observations import join_pack_events_with_coverage
from trellis.retrieve.pack_value import collect_pack_verdicts
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventType,
    ScanCoverage,
    merge_coverage,
    scan_events,
)

if TYPE_CHECKING:
    from trellis.stores.base.event_log import Event, EventLog

logger = structlog.get_logger(__name__)

#: Usable graded rows per 30 days the Phase 1 exporter needs, strictly above.
PHASE_1_GATE = 500

#: ``subject_ref.ref_type`` values naming something no pack can serve. A row
#: of one of these types that matched nothing is ``unservable`` rather than
#: ``never_served``, so a refused session is not read as a memory retrieval
#: missed. A deny-list: an unknown type is presumed servable, which keeps its
#: misses visible instead of explaining them away.
UNSERVABLE_REF_TYPES: frozenset[str] = frozenset({"session"})

RUNG_UNSERVABLE = "unservable"
RUNG_NEVER_SERVED = "never_served"
RUNG_SERVED_UNGRADED = "served_ungraded"
RUNG_GRADED_UNCITED = "graded_uncited"
RUNG_CITED_UNHELPFUL = "cited_unhelpful"
RUNG_CITED_HELPFUL = "cited_helpful"

#: Evidence rank of a serving. ``0`` is "no serving"; the thresholds below are
#: what the ``served`` / ``graded`` / ``cited`` rollups mean.
_RUNG_BY_RANK = {
    1: RUNG_SERVED_UNGRADED,
    2: RUNG_GRADED_UNCITED,
    3: RUNG_CITED_UNHELPFUL,
    4: RUNG_CITED_HELPFUL,
}
_SERVED, _GRADED, _CITED = 1, 2, 3

GATE_PASSES = "passes"
GATE_FAILS_STRICT_READING = "fails_strict_reading"
GATE_FAILS_EVERY_READING = "fails_every_reading"

#: The reading the gate is decided on; every other reading is looser.
STRICT_READING = "cited_rows"

_ALL = "(all)"
_DAY_SECONDS = 86_400


class JudgedOutcomeCell(TrellisModel):
    """One ``(op_type, decision)`` cell; ``(all)`` marks a rollup.

    The six rung counts partition ``judged``. ``served`` / ``graded`` /
    ``cited`` are cumulative rollups over them (a cited row is also graded
    and served), and the ``*_any_order`` variants count the same rollups with
    the judged-before-served constraint dropped.
    """

    op_type: str
    decision: str
    judged: int = 0
    unservable: int = 0
    never_served: int = 0
    served_ungraded: int = 0
    graded_uncited: int = 0
    cited_unhelpful: int = 0
    cited_helpful: int = 0
    served: int = 0
    graded: int = 0
    cited: int = 0
    cited_contradictory: int = 0
    served_before_only: int = 0
    servings: int = 0
    served_any_order: int = 0
    graded_any_order: int = 0
    cited_any_order: int = 0
    distinct_subjects: int = 0
    distinct_served_subjects: int = 0
    distinct_graded_subjects: int = 0
    distinct_cited_subjects: int = 0


class GateReading(TrellisModel):
    """One way of counting the Phase 1 gate, stated with its rate."""

    reading: str
    count: int
    per_30d: float | None = None
    passes: bool = False


class JudgedOutcomesReport(TrellisModel):
    """Judged decisions joined to servings and citations over a window."""

    window_days: int
    effective_window_days: float
    judged_events: int = 0
    malformed_judged_events: int = 0
    judged_rows_before_coverage: int = 0
    ref_types: dict[str, int] = Field(default_factory=dict)
    packs: int = 0
    flat_packs: int = 0
    sectioned_packs_excluded: int = 0
    feedback_events: int = 0
    pack_targeted_feedback: int = 0
    unjoined_feedback: int = 0
    attributed_packs: int = 0
    stray_citations: int = 0
    stray_citations_matching_judged: int = 0
    cells: list[JudgedOutcomeCell] = Field(default_factory=list)
    by_op_type: list[JudgedOutcomeCell] = Field(default_factory=list)
    totals: JudgedOutcomeCell = Field(
        default_factory=lambda: JudgedOutcomeCell(op_type=_ALL, decision=_ALL)
    )
    gate_threshold: int = PHASE_1_GATE
    gate_reading: str = STRICT_READING
    gate_number: float | None = None
    gate_verdict: str = GATE_FAILS_EVERY_READING
    gate_readings: list[GateReading] = Field(default_factory=list)
    single_decision_op_types: dict[str, str] = Field(default_factory=dict)
    scan: ScanCoverage = Field(default_factory=ScanCoverage)
    notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _Serving:
    pack_id: str
    item_id: str
    at: datetime


@dataclass(frozen=True)
class _Row:
    op_type: str
    decision: str
    ref_type: str
    ref_id: str
    at: datetime


@dataclass(frozen=True)
class _Outcome:
    rung: str
    rank: int
    any_order_rank: int
    servings: int
    contradictory: bool
    served_before_only: bool


class _ServingIndex:
    """Every flat-pack serving, reachable by item id and by chunk parent."""

    def __init__(self, flat_packs: Mapping[str, Event]) -> None:
        self.by_item: dict[str, list[_Serving]] = defaultdict(list)
        self.by_parent: dict[str, list[_Serving]] = defaultdict(list)
        self.served_ids: dict[str, set[str]] = {}
        for pack_id, event in flat_packs.items():
            at = _as_utc(event.occurred_at)
            served = self.served_ids.setdefault(pack_id, set())
            for raw in (event.payload or {}).get("injected_items") or []:
                if not isinstance(raw, Mapping):
                    continue
                item_id = raw.get("item_id")
                if not isinstance(item_id, str) or not item_id or item_id in served:
                    continue
                served.add(item_id)
                serving = _Serving(pack_id=pack_id, item_id=item_id, at=at)
                self.by_item[item_id].append(serving)
                if CHUNK_ID_SEPARATOR in item_id:
                    self.by_parent[_parent_id(item_id)].append(serving)

    def matching(self, subject: str) -> Iterator[_Serving]:
        """Servings a judgment about ``subject`` covers (containment).

        No guard is needed for a chunk subject: ``by_parent`` is keyed by the
        text before the first separator, which cannot contain one, so a chunk
        judgment matches its own servings and nothing else.
        """
        yield from self.by_item.get(subject, ())
        yield from self.by_parent.get(subject, ())


class _Tally:
    """Counts and distinct-subject sets for one cell, built into a model last."""

    __slots__ = ("counts", "subjects")

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.subjects: dict[str, set[str]] = defaultdict(set)

    def add(self, row: _Row, outcome: _Outcome) -> None:
        counts = self.counts
        counts["judged"] += 1
        counts[outcome.rung] += 1
        counts["servings"] += outcome.servings
        counts["cited_contradictory"] += int(outcome.contradictory)
        counts["served_before_only"] += int(outcome.served_before_only)
        self.subjects["judged"].add(row.ref_id)
        for name, floor in (
            ("served", _SERVED),
            ("graded", _GRADED),
            ("cited", _CITED),
        ):
            if outcome.rank >= floor:
                counts[name] += 1
                self.subjects[name].add(row.ref_id)
            if outcome.any_order_rank >= floor:
                counts[f"{name}_any_order"] += 1

    def build(self, op_type: str, decision: str) -> JudgedOutcomeCell:
        c = self.counts
        return JudgedOutcomeCell(
            op_type=op_type,
            decision=decision,
            judged=c["judged"],
            unservable=c[RUNG_UNSERVABLE],
            never_served=c[RUNG_NEVER_SERVED],
            served_ungraded=c[RUNG_SERVED_UNGRADED],
            graded_uncited=c[RUNG_GRADED_UNCITED],
            cited_unhelpful=c[RUNG_CITED_UNHELPFUL],
            cited_helpful=c[RUNG_CITED_HELPFUL],
            served=c["served"],
            graded=c["graded"],
            cited=c["cited"],
            cited_contradictory=c["cited_contradictory"],
            served_before_only=c["served_before_only"],
            servings=c["servings"],
            served_any_order=c["served_any_order"],
            graded_any_order=c["graded_any_order"],
            cited_any_order=c["cited_any_order"],
            distinct_subjects=len(self.subjects["judged"]),
            distinct_served_subjects=len(self.subjects["served"]),
            distinct_graded_subjects=len(self.subjects["graded"]),
            distinct_cited_subjects=len(self.subjects["cited"]),
        )


def summarize_judged_outcomes(
    event_log: EventLog,
    *,
    days: int = 30,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> JudgedOutcomesReport:
    """Join every judged row in the window to what followed it.

    Args:
        event_log: Operational event log holding ``MEMORY_OP_JUDGED``,
            ``PACK_ASSEMBLED`` and ``FEEDBACK_RECORDED``.
        days: Look-back window for all three event types.
        limit: Per-event-type scan limit.

    Returns:
        A :class:`JudgedOutcomesReport`. When a scan hits its cap, judged rows
        older than the latest scan's evidence start are dropped and counted,
        and every rate is taken over the window the evidence actually covers.
    """
    now = datetime.now(tz=UTC)
    since = now - timedelta(days=days)
    judged_scan = scan_events(
        event_log, event_type=EventType.MEMORY_OP_JUDGED, since=since, limit=limit
    )
    feedback_events, pack_events, pack_event_count, join_coverage = (
        join_pack_events_with_coverage(event_log, since=since, limit=limit)
    )
    scan = merge_coverage(judged_scan.coverage, join_coverage)

    evidence_start = since
    if scan.truncated and scan.covered_since:
        evidence_start = max(since, _as_utc(datetime.fromisoformat(scan.covered_since)))
    effective_days = max((now - evidence_start).total_seconds(), 0.0) / _DAY_SECONDS

    # Sectioned packs carry no per-item rows, so their servings are invisible
    # to this join exactly as they are to analyze value.
    flat_packs = {
        pack_id: event
        for pack_id, event in pack_events.items()
        if (event.payload or {}).get("injected_items")
    }
    verdicts = collect_pack_verdicts(
        feedback_events,
        {pack_id: event.payload or {} for pack_id, event in flat_packs.items()},
    )
    helpful_by_pack: Mapping[str, set[str]] = verdicts["helpful"]
    unhelpful_by_pack: Mapping[str, set[str]] = verdicts["unhelpful"]
    index = _ServingIndex(flat_packs)

    rows, malformed, before_coverage = _partition_rows(
        judged_scan.events, evidence_start
    )

    cells: dict[tuple[str, str], _Tally] = defaultdict(_Tally)
    op_rollups: dict[str, _Tally] = defaultdict(_Tally)
    totals = _Tally()
    ref_types: Counter[str] = Counter()
    decisions_by_op: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        outcome = _grade(row, index, helpful_by_pack, unhelpful_by_pack)
        cells[(row.op_type, row.decision)].add(row, outcome)
        op_rollups[row.op_type].add(row, outcome)
        totals.add(row, outcome)
        ref_types[row.ref_type] += 1
        if outcome.rung != RUNG_UNSERVABLE:
            decisions_by_op[row.op_type].add(row.decision)

    total_cell = totals.build(_ALL, _ALL)
    readings = _gate_readings(total_cell, effective_days)
    verdict = _gate_verdict(readings)

    single_decision = {
        op_type: next(iter(decisions))
        for op_type, decisions in sorted(decisions_by_op.items())
        if len(decisions) == 1
    }
    strays, strays_matching = _stray_citations(
        index,
        helpful_by_pack,
        unhelpful_by_pack,
        subjects={row.ref_id for row in rows},
    )
    sectioned = len(pack_events) - len(flat_packs)
    attributed_packs = len(set(helpful_by_pack) | set(unhelpful_by_pack))

    report = JudgedOutcomesReport(
        window_days=days,
        effective_window_days=round(effective_days, 2),
        judged_events=len(judged_scan.events),
        malformed_judged_events=malformed,
        judged_rows_before_coverage=before_coverage,
        ref_types=dict(sorted(ref_types.items())),
        packs=pack_event_count,
        flat_packs=len(flat_packs),
        sectioned_packs_excluded=sectioned,
        feedback_events=len(feedback_events),
        pack_targeted_feedback=verdicts["targeted"],
        unjoined_feedback=verdicts["targeted_unjoined"],
        attributed_packs=attributed_packs,
        stray_citations=strays,
        stray_citations_matching_judged=strays_matching,
        cells=sorted(
            (tally.build(op, decision) for (op, decision), tally in cells.items()),
            key=lambda cell: (cell.op_type, -cell.judged, cell.decision),
        ),
        by_op_type=[
            op_rollups[op_type].build(op_type, _ALL) for op_type in sorted(op_rollups)
        ],
        totals=total_cell,
        gate_number=readings[0].per_30d,
        gate_verdict=verdict,
        gate_readings=readings,
        single_decision_op_types=single_decision,
        scan=scan,
        notes=_build_notes(
            readings=readings,
            single_decision=single_decision,
            op_rollups=op_rollups,
            sectioned=sectioned,
            malformed=malformed,
            before_coverage=before_coverage,
            scan=scan,
        ),
    )
    logger.debug(
        "judged_outcomes_summarized",
        window_days=days,
        judged=total_cell.judged,
        cited=total_cell.cited,
        gate_number=report.gate_number,
        gate_verdict=verdict,
    )
    return report


def _parse_row(event: Event) -> _Row | None:
    """Read the join key off a judged payload, or ``None`` if it has none.

    Read leniently rather than through ``MemoryOpJudgedPayload``: the log is
    written by several builds, and a row a newer build widened must still be
    counted rather than rejected by ``extra="forbid"``.
    """
    payload = event.payload or {}
    op_type = payload.get("op_type")
    decision = payload.get("decision")
    subject = payload.get("subject_ref")
    if not isinstance(subject, Mapping):
        return None
    ref_id = subject.get("ref_id")
    ref_type = subject.get("ref_type")
    if not isinstance(op_type, str) or not op_type:
        return None
    if not isinstance(decision, str) or not decision:
        return None
    if not isinstance(ref_id, str) or not ref_id:
        return None
    return _Row(
        op_type=op_type,
        decision=decision,
        ref_type=ref_type if isinstance(ref_type, str) else "",
        ref_id=ref_id,
        at=_as_utc(event.occurred_at),
    )


def _partition_rows(
    events: list[Event], evidence_start: datetime
) -> tuple[list[_Row], int, int]:
    """Split judged events into joinable rows, malformed, and pre-coverage."""
    rows: list[_Row] = []
    malformed = before_coverage = 0
    for event in events:
        row = _parse_row(event)
        if row is None:
            malformed += 1
        elif row.at < evidence_start:
            before_coverage += 1
        else:
            rows.append(row)
    return rows, malformed, before_coverage


def _grade(
    row: _Row,
    index: _ServingIndex,
    helpful_by_pack: Mapping[str, set[str]],
    unhelpful_by_pack: Mapping[str, set[str]],
) -> _Outcome:
    """Place one judged row on its best rung."""
    rank = any_order_rank = servings = 0
    cited_helpful = cited_unhelpful = False
    for serving in index.matching(row.ref_id):
        helpful = helpful_by_pack.get(serving.pack_id)
        unhelpful = unhelpful_by_pack.get(serving.pack_id)
        in_helpful = helpful is not None and serving.item_id in helpful
        in_unhelpful = unhelpful is not None and serving.item_id in unhelpful
        if in_helpful:
            serving_rank = 4
        elif in_unhelpful:
            serving_rank = 3
        elif helpful is not None or unhelpful is not None:
            serving_rank = 2
        else:
            serving_rank = 1
        any_order_rank = max(any_order_rank, serving_rank)
        if serving.at < row.at:
            continue
        servings += 1
        rank = max(rank, serving_rank)
        cited_helpful = cited_helpful or in_helpful
        cited_unhelpful = cited_unhelpful or in_unhelpful

    if rank:
        rung = _RUNG_BY_RANK[rank]
    elif not any_order_rank and row.ref_type in UNSERVABLE_REF_TYPES:
        rung = RUNG_UNSERVABLE
    else:
        rung = RUNG_NEVER_SERVED
    return _Outcome(
        rung=rung,
        rank=rank,
        any_order_rank=any_order_rank,
        servings=servings,
        contradictory=cited_helpful and cited_unhelpful,
        served_before_only=not rank and any_order_rank > 0,
    )


def _gate_readings(totals: JudgedOutcomeCell, window_days: float) -> list[GateReading]:
    """Every reading of the gate, strictest first.

    Rows are never fewer than distinct subjects, any-order never fewer than
    downstream, and served never fewer than graded never fewer than cited, so
    ``served_rows_any_order`` is the loosest: if it fails, every reading does.
    """
    counts = (
        (STRICT_READING, totals.cited),
        ("graded_rows", totals.graded),
        ("served_rows", totals.served),
        ("cited_subjects", totals.distinct_cited_subjects),
        ("graded_subjects", totals.distinct_graded_subjects),
        ("served_subjects", totals.distinct_served_subjects),
        ("cited_rows_any_order", totals.cited_any_order),
        ("graded_rows_any_order", totals.graded_any_order),
        ("served_rows_any_order", totals.served_any_order),
    )
    readings = []
    for name, count in counts:
        rate = _per_30d(count, window_days)
        readings.append(
            GateReading(
                reading=name,
                count=count,
                per_30d=rate,
                passes=rate is not None and rate > PHASE_1_GATE,
            )
        )
    return readings


def _gate_verdict(readings: list[GateReading]) -> str:
    """Decide on the strict reading; say whether a looser one would have passed."""
    if readings[0].passes:
        return GATE_PASSES
    if any(reading.passes for reading in readings):
        return GATE_FAILS_STRICT_READING
    return GATE_FAILS_EVERY_READING


def _stray_citations(
    index: _ServingIndex,
    helpful_by_pack: Mapping[str, set[str]],
    unhelpful_by_pack: Mapping[str, set[str]],
    *,
    subjects: set[str],
) -> tuple[int, int]:
    """Citations naming an id the pack never served, and how many were joinable.

    A grader who cites a parent id when a chunk was served, or the other way
    round, loses that citation to this join. The second count says how many
    such strays would have matched a judged subject, so a low cited count can
    be told apart from a spelling mismatch between graders and servings.
    """
    strays = matching = 0
    for pack_id in set(helpful_by_pack) | set(unhelpful_by_pack):
        cited = helpful_by_pack.get(pack_id, set()) | unhelpful_by_pack.get(
            pack_id, set()
        )
        for item_id in cited - index.served_ids.get(pack_id, set()):
            strays += 1
            if item_id in subjects or (
                CHUNK_ID_SEPARATOR in item_id and _parent_id(item_id) in subjects
            ):
                matching += 1
    return strays, matching


def _build_notes(
    *,
    readings: list[GateReading],
    single_decision: Mapping[str, str],
    op_rollups: Mapping[str, _Tally],
    sectioned: int,
    malformed: int,
    before_coverage: int,
    scan: ScanCoverage,
) -> list[str]:
    notes = [
        (
            "Each judged row takes the best rung any serving at or after the "
            "judgment reached: cited_helpful > cited_unhelpful > graded_uncited "
            "(the pack was graded, this item was not cited) > served_ungraded. A "
            "parent subject also matches its chunks' servings; a chunk subject "
            "matches only itself. never_served and unservable rows are counted but "
            "excluded from every gate reading, because a judgment nothing served "
            "grades the judge rather than the memory. The gate is decided on "
            f"{STRICT_READING}; every other reading is looser."
        )
    ]
    if scan.truncated and scan.note:
        notes.append(scan.note)
    if before_coverage:
        notes.append(
            f"{before_coverage} judged row(s) older than the truncated scans' "
            "evidence start were dropped, and rates use the covered window, "
            "because their servings and citations could not be read."
        )
    by_name = {reading.reading: reading for reading in readings}
    rows, subjects = by_name[STRICT_READING], by_name["cited_subjects"]
    if rows.passes != subjects.passes:
        notes.append(
            f"Rows and distinct subjects fall on opposite sides of the gate: "
            f"{rows.per_30d} cited rows but {subjects.per_30d} distinct cited "
            "subjects per 30 days. A subject judged more than once contributes "
            "one row per judgment, so rows overstate independent evidence."
        )
    if single_decision:
        listed = ", ".join(
            f"{op_type}={decision!r} (n={_servable(op_rollups[op_type])})"
            for op_type, decision in single_decision.items()
        )
        notes.append(
            f"Every servable row of these op types carries one decision: "
            f"{listed}. Their rows record what followed that decision and "
            "cannot contrast it with another, so as training pairs they are "
            "an upper bound."
        )
    if sectioned:
        notes.append(
            f"{sectioned} sectioned pack(s) excluded — build_sectioned emits no "
            "injected_items[], so a subject served only in sectioned packs reads "
            "as never_served."
        )
    if malformed:
        notes.append(
            f"{malformed} judged event(s) had no string op_type, decision or "
            "subject_ref.ref_id and were placed on no rung."
        )
    return notes


def _servable(tally: _Tally) -> int:
    return tally.counts["judged"] - tally.counts[RUNG_UNSERVABLE]


def _per_30d(count: int, window_days: float) -> float | None:
    if window_days <= 0:
        return None
    return round(count * 30 / window_days, 1)


def _parent_id(item_id: str) -> str:
    return item_id.split(CHUNK_ID_SEPARATOR, 1)[0]


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


__all__ = [
    "PHASE_1_GATE",
    "UNSERVABLE_REF_TYPES",
    "GateReading",
    "JudgedOutcomeCell",
    "JudgedOutcomesReport",
    "summarize_judged_outcomes",
]
