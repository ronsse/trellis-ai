"""Replay historical feedback events into the :class:`OutcomeStore`.

The outcome stack (#558 → #559 → #561 → #563) bridges every *new*
``FEEDBACK_RECORDED`` into per-component ``OutcomeEvent`` rows, and
:class:`~trellis.learning.tuners.rule_tuner.RuleTuner` reads those rows
over a trailing 30-day window.  On a deployment that already has months
of feedback, the store is empty on the day that stack ships, so the
tuner's first useful pass is a month away — the history it needs exists,
it just never passed through a bridge that did not exist yet.

This module replays it, through **the same bridge the live path uses**
(:func:`trellis.feedback.recording.bridge_feedback_to_outcomes`) rather
than a second implementation of it.  Three properties follow, and each
is load-bearing:

* **Dry run is apply with the final append withheld.** Both modes run
  the real bridge; only the sink differs — a
  :class:`_CollectingOutcomeStore` buffers the rows instead of a real
  store persisting them.  There is no model of what apply *would* do,
  so the two cannot drift.
* **De-duplication lives here, not in the store.**  ``OutcomeStore`` is
  an append-only contract and the live bridge deliberately re-emits on a
  replayed ``feedback_id``; teaching the store to collapse duplicates
  would change a contract its other callers depend on.
* **Only rows this bridge could have written are replayed.**  See
  :meth:`~trellis.feedback.models.PackFeedback.from_event_payload`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from trellis.feedback.attribution import payload_pack_id
from trellis.feedback.models import PackFeedback
from trellis.feedback.recording import bridge_feedback_to_outcomes
from trellis.learning.tuners.rule_tuner import (
    DEFAULT_WINDOW_DAYS,
    aggregate_outcomes,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.base.outcome import OutcomeStore

if TYPE_CHECKING:
    from trellis.schemas.outcome import OutcomeEvent
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Ceiling on the ``FEEDBACK_RECORDED`` rows one pass will read.  Matches
#: :class:`RuleTuner`'s own ``batch_limit`` so a backfill sized for the
#: tuner's window cannot silently read a different population than the
#: tuner will.
DEFAULT_EVENT_LIMIT = 5000

#: Rows per ``append_many`` call.  The append is chunked rather than
#: single-shot so a large first backfill does not hand one transaction a
#: list bounded only by the event limit.
_APPEND_CHUNK = 500

#: Identity of one replayed row: the feedback it came from, and the
#: component it was attributed to.  Unique because *both* emitters stamp
#: ``metadata["feedback_id"]`` and ``COMPONENT_ID_BY_SOURCE_STRATEGY`` is
#: injective, so one feedback yields at most one row per component.  It
#: is deliberately finer than ``feedback_id`` alone: a crash midway
#: through a fan-out leaves the remaining rows — and only those — to be
#: written by the next run.
_RowKey = tuple[str, str]


@dataclass(frozen=True)
class BackfillCell:
    """One learning-axis cell the tuner would see after this backfill.

    The same grouping :func:`aggregate_outcomes` applies, reported
    rather than evaluated: the backfill states what population exists
    and leaves "does a rule fire on it?" to ``trellis worker tune``,
    which already answers that and must stay the only thing that does.
    """

    component_id: str
    domain: str | None
    intent_family: str | None
    tool_name: str | None
    count: int
    items_served: int
    items_referenced: int
    success_rate: float
    reference_rate: float | None


@dataclass(frozen=True)
class BackfillReport:
    """What a backfill pass found, and what it wrote.

    ``rows_written`` is ``0`` on a dry run *and* on an apply that had
    nothing left to do; ``applied`` is what distinguishes them.
    """

    window_start: datetime
    window_end: datetime
    applied: bool
    events_scanned: int = 0
    events_truncated: bool = False
    events_replayable: int = 0
    events_not_replayable: int = 0
    events_pack_targeted: int = 0
    events_failed: int = 0
    rows_planned: int = 0
    rows_already_present: int = 0
    rows_written: int = 0
    rows_by_component: dict[str, int] = field(default_factory=dict)
    existing_rows_in_window: int = 0
    cells: list[BackfillCell] = field(default_factory=list)

    @property
    def rows_pending(self) -> int:
        """Rows this pass would write, or did."""
        return self.rows_planned - self.rows_already_present


class _CollectingOutcomeStore(OutcomeStore):
    """In-memory sink that buffers what the bridge emits.

    Lets the dry run execute the real bridge — the whole point of the
    seam — and lets an apply de-duplicate *before* anything is
    persisted.

    A faithful ``OutcomeStore``, not a stub: ``query`` and ``count``
    filter the buffer on the axes the ABC names.  Today the bridge only
    appends, so those two are never called from here — but "dry run is
    apply with the append withheld" holds only while this sink answers
    a read the way a real store would, and a bridge that grows one
    would silently make the two modes disagree rather than fail.  The
    buffer holds the bridge's own objects, which is also why the plan
    is not round-tripped through a scratch database.
    """

    def __init__(self) -> None:
        self.collected: list[OutcomeEvent] = []

    def append(self, outcome: OutcomeEvent) -> None:
        self.collected.append(outcome)

    def append_many(self, outcomes: list[OutcomeEvent]) -> int:
        self.collected.extend(outcomes)
        return len(outcomes)

    def query(
        self,
        *,
        component_id: str | None = None,
        domain: str | None = None,
        intent_family: str | None = None,
        tool_name: str | None = None,
        phase: str | None = None,
        agent_role: str | None = None,
        params_version: str | None = None,
        run_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[OutcomeEvent]:
        matched = [
            event
            for event in self.collected
            if _matches(
                event,
                component_id=component_id,
                domain=domain,
                intent_family=intent_family,
                tool_name=tool_name,
                phase=phase,
                agent_role=agent_role,
                params_version=params_version,
                run_id=run_id,
                since=since,
                until=until,
            )
        ]
        matched.sort(key=lambda event: event.occurred_at)
        return matched[:limit]

    def count(
        self,
        *,
        component_id: str | None = None,
        domain: str | None = None,
        intent_family: str | None = None,
        tool_name: str | None = None,
        phase: str | None = None,
        params_version: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        return sum(
            1
            for event in self.collected
            if _matches(
                event,
                component_id=component_id,
                domain=domain,
                intent_family=intent_family,
                tool_name=tool_name,
                phase=phase,
                params_version=params_version,
                since=since,
                until=until,
            )
        )

    def close(self) -> None:
        """No resources to release."""


def _matches(
    event: OutcomeEvent,
    *,
    component_id: str | None = None,
    domain: str | None = None,
    intent_family: str | None = None,
    tool_name: str | None = None,
    phase: str | None = None,
    agent_role: str | None = None,
    params_version: str | None = None,
    run_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> bool:
    """Whether one row satisfies a set of ``OutcomeStore`` filters."""
    axes = (
        (component_id, event.component_id),
        (domain, event.domain),
        (intent_family, event.intent_family),
        (tool_name, event.tool_name),
        (phase, event.phase),
        (agent_role, event.agent_role),
        (params_version, event.params_version),
        (run_id, event.run_id),
    )
    if any(wanted is not None and wanted != actual for wanted, actual in axes):
        return False
    if since is not None and event.occurred_at < since:
        return False
    return not (until is not None and event.occurred_at > until)


def _row_key(event: OutcomeEvent) -> _RowKey | None:
    """The ``(feedback_id, component_id)`` identity of a bridged row."""
    feedback_id = event.metadata.get("feedback_id")
    if not isinstance(feedback_id, str) or not feedback_id:
        return None
    return (feedback_id, event.component_id)


def backfill_outcomes(
    *,
    event_log: EventLog,
    outcome_store: OutcomeStore,
    since: datetime | None = None,
    until: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    apply: bool = False,
    event_limit: int = DEFAULT_EVENT_LIMIT,
) -> BackfillReport:
    """Replay ``FEEDBACK_RECORDED`` events into the outcome store.

    Dry by default.  Idempotent in both modes: a row whose
    ``(feedback_id, component_id)`` is already in the store over the
    replayed rows' own time span is counted and skipped, so re-running
    after a partial write completes exactly what is missing.

    The window defaults to the tuner's own trailing 30 days, because
    that is the population whose absence the backfill exists to fix.
    Replayed rows are stamped with the *feedback's* timestamp, not the
    backfill's, so they land in the historical window the tuner reads —
    which is also why the de-duplication scan covers that span rather
    than the present.

    Args:
        event_log: Source of the ``FEEDBACK_RECORDED`` history.
        outcome_store: Destination, and the store scanned for rows a
            previous pass already wrote.
        since: Window start.  Defaults to ``until - window_days``.
        until: Window end.  Defaults to now.
        window_days: Width of the default window.
        apply: Persist the planned rows.  When ``False`` (default)
            everything runs except the final append.
        event_limit: Ceiling on events read.  A pass that hits it
            reports ``events_truncated`` — the window is not silently
            narrowed.

    Returns:
        :class:`BackfillReport` describing the population, the plan, and
        the cells the tuner would then see.
    """
    window_end = until or datetime.now(UTC)
    window_start = since or (window_end - timedelta(days=window_days))

    events = event_log.get_events(
        event_type=EventType.FEEDBACK_RECORDED,
        since=window_start,
        until=window_end,
        limit=event_limit,
        order="asc",
    )
    truncated = len(events) >= event_limit

    collector = _CollectingOutcomeStore()
    replayable = 0
    not_replayable = 0
    pack_targeted = 0
    failed = 0

    for event in events:
        feedback = PackFeedback.from_event_payload(
            event.payload,
            default_timestamp_utc=event.occurred_at.isoformat(),
        )
        if feedback is None:
            # Written by the governed ``FeedbackRecordHandler`` family,
            # which never called this bridge and carries no item
            # attribution.  Counted, never replayed.
            not_replayable += 1
            continue
        replayable += 1
        pack_id = payload_pack_id(event.payload) or None
        if pack_id is not None:
            pack_targeted += 1
        bridged = bridge_feedback_to_outcomes(
            feedback,
            outcome_store=collector,
            pack_id=pack_id,
            event_log=event_log,
        )
        if bridged.error is not None:
            failed += 1

    planned = collector.collected
    existing_keys, existing_rows = _existing_rows(
        outcome_store,
        planned=planned,
        window_start=window_start,
        window_end=window_end,
    )

    pending: list[OutcomeEvent] = []
    already_present = 0
    for row in planned:
        key = _row_key(row)
        if key is not None and key in existing_keys:
            already_present += 1
            continue
        pending.append(row)

    rows_written = 0
    if apply and pending:
        for start in range(0, len(pending), _APPEND_CHUNK):
            rows_written += outcome_store.append_many(
                pending[start : start + _APPEND_CHUNK]
            )

    rows_by_component: dict[str, int] = {}
    for row in pending:
        rows_by_component[row.component_id] = (
            rows_by_component.get(row.component_id, 0) + 1
        )

    report = BackfillReport(
        window_start=window_start,
        window_end=window_end,
        applied=apply,
        events_scanned=len(events),
        events_truncated=truncated,
        events_replayable=replayable,
        events_not_replayable=not_replayable,
        events_pack_targeted=pack_targeted,
        events_failed=failed,
        rows_planned=len(planned),
        rows_already_present=already_present,
        rows_written=rows_written,
        rows_by_component=dict(sorted(rows_by_component.items())),
        existing_rows_in_window=len(existing_rows),
        cells=_summarize_cells(existing_rows + pending),
    )
    logger.info(
        "outcome_backfill_complete",
        applied=apply,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        events_scanned=report.events_scanned,
        events_not_replayable=report.events_not_replayable,
        rows_planned=report.rows_planned,
        rows_already_present=report.rows_already_present,
        rows_written=report.rows_written,
        cells=len(report.cells),
    )
    return report


def _existing_rows(
    outcome_store: OutcomeStore,
    *,
    planned: list[OutcomeEvent],
    window_start: datetime,
    window_end: datetime,
) -> tuple[set[_RowKey], list[OutcomeEvent]]:
    """Rows already in the store over the span the plan touches.

    ``OutcomeStore.query`` offers no metadata filter, so the skip set is
    built by reading the span and keying in Python.  The span is the
    union of the backfill window and the planned rows' own
    ``occurred_at`` range — a payload timestamp can sit outside the
    window its event landed in, and a row missed by the scan would be
    written twice.

    The read is sized by ``count`` first because ``query`` is
    ``ORDER BY occurred_at ASC LIMIT ?`` on the SQLite backend, so a
    bare limit truncates the *newest* rows — exactly the ones a repeat
    run needs to see.
    """
    span_start = window_start
    span_end = window_end
    for row in planned:
        span_start = min(span_start, row.occurred_at)
        span_end = max(span_end, row.occurred_at)

    expected = outcome_store.count(since=span_start, until=span_end)
    rows = outcome_store.query(
        since=span_start,
        until=span_end,
        limit=expected + 1,
    )
    if len(rows) > expected:
        # The window grew between the two reads: a concurrent writer
        # landed a row. Harmless — the extra row is in the skip set —
        # but worth saying out loud rather than absorbing.
        logger.info(
            "outcome_backfill_window_grew_during_scan",
            expected=expected,
            observed=len(rows),
        )

    keys = {key for key in (_row_key(row) for row in rows) if key is not None}
    return keys, rows


def _summarize_cells(outcomes: list[OutcomeEvent]) -> list[BackfillCell]:
    """Group rows the way the tuner will, largest cell first."""
    cells = [
        BackfillCell(
            component_id=agg.scope.component_id,
            domain=agg.scope.domain,
            intent_family=agg.scope.intent_family,
            tool_name=agg.scope.tool_name,
            count=agg.count,
            items_served=agg.items_served_total,
            items_referenced=agg.items_referenced_total,
            success_rate=agg.success_rate,
            reference_rate=agg.reference_rate,
        )
        for agg in aggregate_outcomes(outcomes)
    ]
    cells.sort(key=lambda cell: (-cell.count, cell.component_id))
    return cells
