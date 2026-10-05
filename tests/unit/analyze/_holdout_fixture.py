"""A synthetic event log for ``trellis analyze holdout``.

Writes the three event types the analysis reads, at times the test
controls: ``PACK_ASSEMBLED`` rows shaped like the pack builder's
(``holdout`` / ``holdout_rate`` and, on a withheld row, ``holdout_items``
or ``holdout_sections``), one ``CAPTURE_SESSION_PACKS`` join per task
shaped like the capture worker's, and ``CAPTURE_SWEEP_COMPLETED`` rows.

Every id is synthetic, and every intent is :data:`INTENT_MARKER`, which no
report may print.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from trellis.stores.base.event_log import Event, EventLog, EventType

INTENT_MARKER = "intent-marker-not-for-output"
SOURCE_SYSTEM = "claude-code"
#: A Monday (ISO week 32 of 2026), so whole-week offsets stay in one week.
MONDAY = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
#: An ``until`` after every fixture row; a 60-day window from it starts
#: on 2026-07-30, before :data:`MONDAY`.
UNTIL = datetime(2026, 9, 28, tzinfo=UTC)
DAYS = 60
#: Where the :data:`DAYS`-day window ending at :data:`UNTIL` opens.
WINDOW_START = UNTIL - timedelta(days=DAYS)
#: 20 days into that window: :func:`seed_late_build`'s first row at rate 0.5.
KEYED_FROM = WINDOW_START + timedelta(days=20)


def outcome_payload(
    *,
    turns: int,
    ended_on_error: bool = False,
    prs_created: int = 0,
    commits: int = 0,
    tokens: int | None = 10_000,
    tool_errors: int = 0,
) -> dict[str, Any]:
    """A ``SessionOutcome.to_payload()`` dict, every field present."""
    return {
        "tool_calls": turns,
        "tool_errors": tool_errors,
        "assistant_turns": turns,
        "assistant_turns_with_usage": turns,
        "user_turns": 1,
        "input_tokens": tokens,
        "output_tokens": None if tokens is None else tokens // 10,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "wall_clock_seconds": 30.0 * turns,
        "commits": commits,
        "prs_created": prs_created,
        "prs_merged": 0,
        "pr_urls": prs_created,
        "ended_on_error": ended_on_error,
        "ended_interrupted": False,
    }


class HoldoutLog:
    """Appends synthetic experiment rows to one event log."""

    def __init__(self, event_log: EventLog) -> None:
        self.event_log = event_log
        self._packs = 0
        self._tasks = 0
        self.latest_join: datetime | None = None

    def pack(
        self,
        *,
        at: datetime,
        withheld: bool | None,
        items: int,
        rate: float = 0.5,
        sectioned: bool = False,
    ) -> str:
        """Append one ``PACK_ASSEMBLED`` row and return its pack id.

        ``withheld=None`` writes a row from a build before the holdout: no
        ``holdout`` key and no ``holdout_rate``. ``items`` is the would-be
        pack's size, recorded under ``holdout_*`` when the row is withheld.
        """
        self._packs += 1
        pack_id = f"pack-{self._packs:04d}"
        rows = [
            {
                "item_id": f"item-{self._packs:04d}-{rank}",
                "item_type": "document",
                "estimated_tokens": 50,
                "rank": rank,
            }
            for rank in range(items)
        ]
        ids = [row["item_id"] for row in rows]
        payload: dict[str, Any] = {
            "intent": INTENT_MARKER,
            "domain": None,
            "agent_id": None,
            "session_id": None,
            "advisory_ids": [],
        }
        if sectioned:
            entity_type = "sectioned_pack"
            full = {
                "name": "context",
                "items_count": items,
                "item_ids": ids,
                "injected_advisory_ids": [],
            }
            if withheld:
                empty = dict(full, items_count=0, item_ids=[])
                payload.update(
                    section_count=1,
                    total_items=0,
                    sections=[empty],
                    holdout_sections=[full],
                    holdout_advisory_ids=[],
                )
            else:
                payload.update(section_count=1, total_items=items, sections=[full])
        else:
            entity_type = "pack"
            if withheld:
                payload.update(
                    items_count=0,
                    injected_item_ids=[],
                    injected_items=[],
                    holdout_items=rows,
                    holdout_advisory_ids=[],
                )
            else:
                payload.update(
                    items_count=items, injected_item_ids=ids, injected_items=rows
                )
        if withheld is not None:
            payload["holdout"] = withheld
            payload["holdout_rate"] = rate
        self.event_log.append(
            Event(
                event_type=EventType.PACK_ASSEMBLED,
                source="pack_builder",
                entity_id=pack_id,
                entity_type=entity_type,
                occurred_at=at,
                recorded_at=at,
                payload=payload,
            )
        )
        return pack_id

    def deployed_before_window(self, *, rate: float = 0.5) -> str:
        """A served row at ``rate`` a day before the window opens.

        The holdout build was already writing that rate when the window
        opened, so the task figures divide by the whole window. The scan
        starts at the window, so the row is in no count.
        """
        return self.pack(
            at=WINDOW_START - timedelta(days=1), withheld=False, items=1, rate=rate
        )

    def join(
        self,
        task_id: str,
        *,
        at: datetime,
        pack_ids: Sequence[str],
        parent: str | None,
        outcome: dict[str, Any] | None,
        retrieval_results: int | None = None,
        pack_ids_unparsed: int = 0,
        source_system: str = SOURCE_SYSTEM,
    ) -> None:
        """Append one ``CAPTURE_SESSION_PACKS`` join for ``task_id``.

        ``retrieval_results`` defaults to one result per pack id.
        """
        payload: dict[str, Any] = {
            "pack_ids": list(pack_ids),
            "retrieval_results": (
                len(pack_ids) if retrieval_results is None else retrieval_results
            ),
            "retrieval_errors": 0,
            "pack_ids_unparsed": pack_ids_unparsed,
            "parent_session_id": parent,
            "source_system": source_system,
        }
        if outcome is not None:
            payload["outcome"] = outcome
        self.event_log.append(
            Event(
                event_type=EventType.CAPTURE_SESSION_PACKS,
                source="worker:session-capture",
                entity_id=task_id,
                entity_type="capture_session",
                occurred_at=at,
                recorded_at=at,
                payload=payload,
            )
        )
        if self.latest_join is None or at > self.latest_join:
            self.latest_join = at

    def task(
        self,
        *,
        start: datetime,
        parent: str | None = "parent-a",
        arms: Sequence[bool | None] = (False,),
        items: int | Sequence[int] = 3,
        turns: int = 20,
        rate: float = 0.5,
        ended_on_error: bool = False,
        prs_created: int = 0,
        commits: int = 0,
        tokens: int | None = 10_000,
        tool_errors: int = 0,
        sectioned: bool = False,
        with_outcome: bool = True,
    ) -> str:
        """One task: a pack per entry of ``arms``, a minute apart, then its join.

        ``arms`` holds each call's ``withheld`` value in call order
        (``None`` for a row from a build before the holdout). The join is
        written an hour after the last pack.
        """
        self._tasks += 1
        task_id = f"task-{self._tasks:04d}"
        sizes = [items] * len(arms) if isinstance(items, int) else list(items)
        pack_ids = [
            self.pack(
                at=start + timedelta(minutes=index),
                withheld=withheld,
                items=size,
                rate=rate,
                sectioned=sectioned,
            )
            for index, (withheld, size) in enumerate(zip(arms, sizes, strict=True))
        ]
        outcome = (
            outcome_payload(
                turns=turns,
                ended_on_error=ended_on_error,
                prs_created=prs_created,
                commits=commits,
                tokens=tokens,
                tool_errors=tool_errors,
            )
            if with_outcome
            else None
        )
        self.join(
            task_id,
            at=start + timedelta(minutes=len(arms), hours=1),
            pack_ids=pack_ids,
            parent=parent,
            outcome=outcome,
        )
        return task_id

    def sweep(
        self,
        *,
        at: datetime | None = None,
        dry_run: bool = False,
        source_system: str = SOURCE_SYSTEM,
    ) -> None:
        """A completed capture sweep, by default four hours after the latest join."""
        if at is None:
            assert self.latest_join is not None, "write a task before the sweep"
            at = self.latest_join + timedelta(hours=4)
        self.event_log.append(
            Event(
                event_type=EventType.CAPTURE_SWEEP_COMPLETED,
                source="worker:session-capture",
                entity_id=f"capture:{source_system}",
                entity_type="capture_sweep",
                occurred_at=at,
                recorded_at=at,
                payload={"dry_run": dry_run, "source_system": source_system},
            )
        )


def seed_experiment(
    log: HoldoutLog,
    *,
    effect: float,
    seed: int,
    parents: int = 6,
    weeks: int = 2,
    per_stratum: int = 10,
    rate: float = 0.5,
) -> int:
    """Tasks across ``parents`` x ``weeks`` strata, half of each stratum withheld.

    Parents differ in their baseline turn counts, so a comparison that
    ignored the strata would mix baselines. Turns scatter around the
    baseline with a log-scale SD of 0.3. A withheld task takes ``effect``
    times the turns its served draw would have taken. Returns the number
    of tasks written; the caller writes the sweep.
    """
    rng = random.Random(seed)  # noqa: S311 - test RNG, not crypto
    written = 0
    for p in range(parents):
        parent = f"parent-{p:02d}"
        baseline = 8 + 6 * p
        for w in range(weeks):
            week_start = MONDAY + timedelta(weeks=w)
            for t in range(per_stratum):
                withheld = t % 2 == 1
                served_turns = max(1, round(baseline * math.exp(rng.gauss(0.0, 0.3))))
                turns = round(served_turns * effect) if withheld else served_turns
                log.task(
                    start=week_start + timedelta(hours=3 * t, minutes=7 * p),
                    parent=parent,
                    arms=[withheld],
                    turns=turns,
                    rate=rate,
                )
                written += 1
    return written


#: :func:`seed_guardrails`' tasks in arm order: PRs created, commits, tool
#: errors, each pack call's ``withheld`` value, and whether it was cut off.
GUARDRAIL_TASKS: tuple[tuple[int, int, int, tuple[bool, ...], bool], ...] = (
    (1, 0, 2, (False,), False),
    (0, 3, 0, (False, False), False),
    (2, 1, 4, (False,), False),
    (0, 0, 1, (False, True, False), False),
    (5, 9, 9, (False, False), True),
    (0, 0, 3, (True, True), False),
    (1, 2, 5, (True, False), False),
    (0, 0, 0, (True, True, True), False),
    (0, 0, 1, (True,), False),
    (0, 0, 0, (True,), True),
    (0, 0, 0, (True,), True),
)


def seed_guardrails(log: HoldoutLog) -> None:
    """:data:`GUARDRAIL_TASKS` at rate 0.5 in one parent session and one week.

    Five served tasks, one of them cut off, and six withheld, two of them
    cut off; each task's arm is its first call's. Turns differ by task.
    """
    for index, (prs, commits, errors, arms, cut_off) in enumerate(GUARDRAIL_TASKS):
        log.task(
            start=MONDAY + timedelta(hours=index),
            arms=arms,
            turns=10 + index,
            prs_created=prs,
            commits=commits,
            tool_errors=errors,
            ended_on_error=cut_off,
        )
    log.sweep()


def seed_missing_tool_errors(log: HoldoutLog) -> None:
    """Six tasks at rate 0.5, of which only two withheld ones record tool errors.

    Served: three tasks whose outcomes lack ``tool_errors``. Withheld: tool
    errors 1 and 3, and a third task lacking the field.
    """
    layout = [(False, None), (False, None), (False, None)]
    layout += [(True, 1), (True, 3), (True, None)]
    for index, (withheld, errors) in enumerate(layout):
        at = MONDAY + timedelta(hours=index)
        pack = log.pack(at=at, withheld=withheld, items=2)
        outcome = outcome_payload(turns=5 + 2 * index, tool_errors=errors or 0)
        if errors is None:
            del outcome["tool_errors"]
        log.join(
            f"task-te-{index}",
            at=at + timedelta(hours=1),
            pack_ids=[pack],
            parent="parent-a",
            outcome=outcome,
        )
    log.sweep()


def seed_late_build(
    log: HoldoutLog, *, early_arm: bool | None = None, early_rate: float = 0.5
) -> None:
    """A rate-0.5 build whose first row in the window is :data:`KEYED_FROM`.

    Main session A, 5 days into the window, got one pack written with
    ``early_arm`` at ``early_rate`` (by default by a build that records no
    holdout). Main session B's own pack at :data:`KEYED_FROM` is then the
    window's first row at rate 0.5, and its ten sub-agent tasks follow an
    hour apart: odd ones withheld, the last two cut off, every one with
    different turns. That is 10 eligible and 8 analysed tasks in one parent
    session and one ISO week, and 2 main sessions, all finished by one sweep.
    """
    log.task(
        start=WINDOW_START + timedelta(days=5),
        parent=None,
        arms=[early_arm],
        items=2,
        rate=early_rate,
        turns=6,
    )
    main = log.task(start=KEYED_FROM, parent=None, arms=[False], items=2, turns=4)
    for index in range(1, 11):
        log.task(
            start=KEYED_FROM + timedelta(hours=index),
            parent=main,
            arms=[index % 2 == 1],
            turns=10 + 3 * index,
            ended_on_error=index > 8,
        )
    log.sweep()
