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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from trellis.analyze import holdout
from trellis.stores.base.event_log import Event, EventLog, EventType

INTENT_MARKER = "intent-marker-not-for-output"
SOURCE_SYSTEM = "claude-code"
#: A Monday (ISO week 32 of 2026), so whole-week offsets stay in one week.
MONDAY = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
#: An ``until`` after every fixture row; a 60-day window from it starts
#: on 2026-07-30, before :data:`MONDAY`.
UNTIL = datetime(2026, 9, 28, tzinfo=UTC)
DAYS = 60
#: Hours after :data:`UNTIL` of the two sweeps that settle its window: the first
#: sweep at or after the end, then one past the three-hour settle allowance.
SETTLING_SWEEP_HOURS_AFTER_UNTIL = (1, 25)
#: Where the :data:`DAYS`-day window ending at :data:`UNTIL` opens.
WINDOW_START = UNTIL - timedelta(days=DAYS)
#: 20 days into that window: :func:`seed_late_build`'s first row at rate 0.5.
KEYED_FROM = WINDOW_START + timedelta(days=20)


@contextmanager
def unchecked_window() -> Iterator[None]:
    """Read a window that has not settled, with the settled-window check skipped.

    A checked read of an unsettled window is refused and computes nothing, so a
    test of what the full report counts there (the funnel's ``unfinished``
    stage, say) reads the same window as an unchecked one. ``checked=False`` is
    what a read with no ``until`` passes; the window itself is unchanged.
    """
    original = holdout._settled_window

    def unchecked(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **{**kwargs, "checked": False})

    with mock.patch.object(holdout, "_settled_window", unchecked):
        yield


#: The outcome fields a readout turns into a measured value.
_MEASURED_FIELDS = (
    "assistant_turns",
    "prs_created",
    "commits",
    "input_tokens",
    "output_tokens",
)


def outcome_payload(
    *,
    turns: int,
    ended_on_error: bool = False,
    prs_created: int = 0,
    commits: int = 0,
    tokens: int | None = 10_000,
    tool_errors: int | None = 0,
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


def timing_payload(
    *,
    hook_before: bool | None,
    hook_messages: int | None = None,
    commit_before: bool | None = False,
    ordinal: int = 3,
    calls: int = 10,
) -> dict[str, Any]:
    """A ``SessionDigest.retrieval_timing_payload()`` dict.

    ``hook_before`` is ``hook_feedback_before_first_retrieval`` and
    ``commit_before`` is ``commit_before_first_retrieval``. The hook count
    defaults to one message when feedback came first and none otherwise. The
    first retrieval is call ``ordinal`` of ``calls``, a first commit (when one
    came before it) call 2.
    """
    if hook_messages is None:
        hook_messages = 1 if hook_before else 0
    return {
        "first_retrieval_ordinal": ordinal,
        "tool_call_count": calls,
        "hook_feedback_messages": hook_messages,
        "hook_feedback_before_first_retrieval": hook_before,
        "first_commit_ordinal": 2 if commit_before else None,
        "commit_before_first_retrieval": commit_before,
    }


def covariates_payload(
    *, brief_chars: Any = 400, agent_type: Any = "aaaaaaaaaaaa"
) -> dict[str, Any]:
    """A ``SessionDigest.covariates_payload()`` dict; the defaults are fake."""
    return {"brief_chars": brief_chars, "agent_type_digest": agent_type}


def legacy_timing_payload(*, hook_before: bool | None) -> dict[str, Any]:
    """A ``retrieval_timing`` as capture wrote it before it kept commits.

    The same keys less the two about the first commit, so a join carrying it
    can decide the hook comparison and nothing about commits.
    """
    timing = timing_payload(hook_before=hook_before)
    del timing["first_commit_ordinal"]
    del timing["commit_before_first_retrieval"]
    return timing


def provenance(commit: str | None) -> dict[str, Any]:
    """An event's ``metadata`` stamped by the build that wrote ``commit``."""
    return {"write_provenance": {"version": "0.0.0", "commit": commit}}


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
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Append one ``PACK_ASSEMBLED`` row and return its pack id.

        ``withheld=None`` writes a row from a build before the holdout: no
        ``holdout`` key and no ``holdout_rate``. ``items`` is the would-be
        pack's size, recorded under ``holdout_*`` when the row is withheld.
        ``metadata`` is the row's event metadata (see :func:`provenance`);
        left ``None`` the row carries no stamp.
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
                metadata=metadata or {},
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
        retrieval_errors: int = 0,
        pack_ids_unparsed: int = 0,
        source_system: str = SOURCE_SYSTEM,
        timing: dict[str, Any] | None = None,
        covariates: dict[str, Any] | None = None,
    ) -> None:
        """Append one ``CAPTURE_SESSION_PACKS`` join for ``task_id``.

        ``retrieval_results`` defaults to one result per pack id. ``timing``
        is the join's ``retrieval_timing`` and ``covariates`` its
        ``covariates``; left ``None`` the join is one from before capture
        recorded it.
        """
        payload: dict[str, Any] = {
            "pack_ids": list(pack_ids),
            "retrieval_results": (
                len(pack_ids) if retrieval_results is None else retrieval_results
            ),
            "retrieval_errors": retrieval_errors,
            "pack_ids_unparsed": pack_ids_unparsed,
            "parent_session_id": parent,
            "source_system": source_system,
        }
        if outcome is not None:
            payload["outcome"] = outcome
        if timing is not None:
            payload["retrieval_timing"] = timing
        if covariates is not None:
            payload["covariates"] = covariates
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
        tool_errors: int | None = 0,
        sectioned: bool = False,
        with_outcome: bool = True,
        measures_missing: bool = False,
        timing: dict[str, Any] | None = None,
        covariates: dict[str, Any] | None = None,
        pack_metadata: Sequence[dict[str, Any] | None] | None = None,
        join_delay: timedelta = timedelta(0),
    ) -> str:
        """One task: a pack per entry of ``arms``, a minute apart, then its join.

        ``arms`` holds each call's ``withheld`` value in call order
        (``None`` for a row from a build before the holdout). The join is
        written an hour after the last pack, and ``join_delay`` later still.
        ``pack_metadata`` holds each
        pack row's event metadata in the same order. ``measures_missing``
        leaves every outcome count the readout reads as ``None``, so the task
        has an outcome block and no measured value.
        """
        self._tasks += 1
        task_id = f"task-{self._tasks:04d}"
        sizes = [items] * len(arms) if isinstance(items, int) else list(items)
        stamps = pack_metadata or [None] * len(arms)
        pack_ids = [
            self.pack(
                at=start + timedelta(minutes=index),
                withheld=withheld,
                items=size,
                rate=rate,
                sectioned=sectioned,
                metadata=stamp,
            )
            for index, (withheld, size, stamp) in enumerate(
                zip(arms, sizes, stamps, strict=True)
            )
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
        if outcome is not None and measures_missing:
            outcome.update(dict.fromkeys(_MEASURED_FIELDS))
        self.join(
            task_id,
            at=start + timedelta(minutes=len(arms), hours=1) + join_delay,
            pack_ids=pack_ids,
            parent=parent,
            outcome=outcome,
            timing=timing,
            covariates=covariates,
        )
        return task_id

    def sweep(
        self,
        *,
        at: datetime | None = None,
        dry_run: bool = False,
        source_system: str = SOURCE_SYSTEM,
        sessions_parsed: int | None = None,
        sessions_errored: int | None = None,
    ) -> None:
        """A completed capture sweep, by default four hours after the latest join.

        ``sessions_parsed`` and ``sessions_errored`` are the sweep's own
        counters; left ``None`` the payload carries neither, as a sweep from
        before capture wrote them.

        A sweep with no ``at`` is the last one the test needs, so it also
        settles the window ending at :data:`UNTIL`: the first sweep after it
        and one past the settle allowance (a read is incomplete until both
        exist). Pass ``at`` to place a single sweep exactly.
        """
        counters = {
            "sessions_parsed": sessions_parsed,
            "sessions_errored": sessions_errored,
        }
        if at is not None:
            self._sweep_at(at, dry_run, source_system, counters)
            return
        assert self.latest_join is not None, "write a task before the sweep"
        self._sweep_at(
            self.latest_join + timedelta(hours=4), dry_run, source_system, counters
        )
        for hours in SETTLING_SWEEP_HOURS_AFTER_UNTIL:
            self._sweep_at(
                UNTIL + timedelta(hours=hours), dry_run, source_system, counters
            )

    def _sweep_at(
        self,
        at: datetime,
        dry_run: bool,
        source_system: str,
        counters: dict[str, int | None],
    ) -> None:
        payload: dict[str, Any] = {"dry_run": dry_run, "source_system": source_system}
        payload.update({key: n for key, n in counters.items() if n is not None})
        self.event_log.append(
            Event(
                event_type=EventType.CAPTURE_SWEEP_COMPLETED,
                source="worker:session-capture",
                entity_id=f"capture:{source_system}",
                entity_type="capture_sweep",
                occurred_at=at,
                recorded_at=at,
                payload=payload,
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


#: How many tasks :func:`seed_arm_shift` changes: three in each of the first
#: three parents, the fourth parent's only task and one of the fifth's two.
CHANGED_TASKS = 11
#: How a second store differs from the first, by the one thing it changes in
#: the withheld arm: the outcome values, which tasks ended on a usage limit, or
#: which tasks have no measured outcome. Keyword arguments of the seed.
ARM_VARIANTS: dict[str, dict[str, Any]] = {
    "values": {"shift": 6},
    "cut_offs": {"cut_offs": True},
    "missing": {"missing": True},
}


#: The behaviour every pair of the unsettled-read guard starts from, so that a
#: variant differs from it in one thing and the timing is present in both.
BEHAVIOUR_FLAT: dict[str, Any] = {"tool_calls": 300, "hook_messages": 0}
#: What a store pair may differ in, beyond the withheld arm's outcomes: how
#: many pack calls its tasks made, when their joins were written, how many tool
#: calls and hook messages the session timing counts, and whether the join
#: carries an outcome block at all. Keyword arguments of :func:`seed_arm_shift`.
BEHAVIOUR_VARIANTS: dict[str, dict[str, Any]] = {
    **{name: {**BEHAVIOUR_FLAT, **kw} for name, kw in ARM_VARIANTS.items()},
    "pack_calls": {**BEHAVIOUR_FLAT, "withheld_calls": 3},
    "join_time": {**BEHAVIOUR_FLAT, "withheld_join_delay": timedelta(hours=3)},
    "tool_call_count": {**BEHAVIOUR_FLAT, "tool_calls": 10},
    "hook_feedback_messages": {**BEHAVIOUR_FLAT, "hook_messages": 7},
    "no_outcome_block": {**BEHAVIOUR_FLAT, "outcome_block": False},
}
#: Every key a refused read publishes: the allow-list the guard holds it to.
#: A key added to the refusal fails the guard until it is named here.
REFUSAL_KEYS = frozenset(
    {
        "status",
        "window_days",
        "since",
        "until",
        "rate",
        "outcome",
        "itt",
        "settle_hours",
        "planned_mde",
        "planned_n",
        "settled_window",
        "notes",
    }
)


def seed_arm_shift(
    log: HoldoutLog,
    *,
    shift: int = 0,
    cut_offs: bool = False,
    missing: bool = False,
    rate: float = 0.2,
    withheld_calls: int | None = None,
    withheld_join_delay: timedelta = timedelta(0),
    tool_calls: int | None = None,
    hook_messages: int = 0,
    outcome_block: bool = True,
) -> None:
    """Ninety-three tasks over five parents and three builds; the withheld arm moved.

    Every figure is arithmetic on the task's index, so two calls differ in
    nothing but the arguments. ``shift`` is added to each of the withheld
    arm's four outcome counts (turns, PRs, commits, tool errors). ``cut_offs``
    ends, and ``missing`` leaves with no measured outcome, every second
    withheld task, the only task of the fourth parent and the withheld task of
    the fifth, whose other task is served: the fourth parent's stratum
    disappears and the fifth's drops from two arms to one. Those tasks carry no
    covariates, record no tool errors and made two calls in both stores, so
    each count that reads the analysed set (the covariate coverage, the
    re-call rate, a guardrail's missing count, the parents and strata it spans,
    the main sessions) has something to differ on. The
    served arm, the builds and the timing are identical across calls.

    The rest move behaviour, not an outcome. ``withheld_calls`` is how many
    pack calls every withheld task made (left ``None``, the task's own one or
    two) and ``withheld_join_delay`` how much later its join was written.
    ``tool_calls`` and ``hook_messages`` fill every task's retrieval timing,
    whose first retrieval is call 10 (left ``None``, no task carries one), and
    ``outcome_block=False`` drops the outcome block from the withheld tasks'
    joins.
    """
    commits = ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]
    hour = 0

    def one(parent: str, parent_index: int, k: int, *, changed: bool) -> None:
        nonlocal hour
        withheld = k % 5 == 0
        move = shift if withheld else 0
        build = commits[0] if k < 15 else commits[1 if k < 28 else 2]
        calls = withheld_calls if withheld and withheld_calls else 1 + changed
        log.task(
            start=MONDAY + timedelta(hours=hour),
            parent=parent,
            arms=[withheld] * calls,
            turns=10 + (k * 7 + parent_index * 3) % 25 + move,
            prs_created=(k + parent_index) % 2 + move,
            commits=(k * 5 + parent_index) % 4 + move,
            tool_errors=None if changed else (k * 3 + parent_index) % 3 + move,
            ended_on_error=cut_offs and changed,
            measures_missing=missing and changed,
            rate=rate,
            pack_metadata=[provenance(build)] * calls,
            with_outcome=outcome_block or not withheld,
            join_delay=withheld_join_delay if withheld else timedelta(0),
            timing=None
            if tool_calls is None
            else timing_payload(
                hook_before=False,
                hook_messages=hook_messages,
                calls=tool_calls,
                ordinal=10,
            ),
            covariates=None
            if changed
            else covariates_payload(
                brief_chars=100 + (hour * 37) % 800, agent_type=f"kind-{k % 3}"
            ),
        )
        hour += 1

    parents = ("parent-a", "parent-b", "parent-c", "parent-d", "parent-e")
    for parent_index, parent in enumerate(parents[:3]):
        for k in range(30):
            one(parent, parent_index, k, changed=k % 5 == 0 and k % 2 == 0)
    one(parents[3], 3, 0, changed=True)
    one(parents[4], 4, 0, changed=True)
    one(parents[4], 4, 1, changed=False)
    for parent in parents:
        log.join(
            parent,
            at=MONDAY + timedelta(hours=hour),
            pack_ids=[],
            parent=None,
            outcome=outcome_payload(turns=5),
        )
        hour += 1


def seed_random_outcomes(
    log: HoldoutLog, *, structure: int, outcomes: int, rate: float = 0.3
) -> None:
    """Eighty tasks over seven parents; ``outcomes`` alone draws what a task did.

    ``structure`` draws everything a window and a rate fix: the parent, the arm,
    the number of packs, the build and whether a covariate was recorded, and the
    main session of each parent. ``outcomes`` draws the figures: the counts, a
    missing tool-error count, a usage-limit cut-off, a missing measured outcome
    and the main sessions' own. Two calls with the same ``structure`` differ in
    outcomes only, and the draws are independent, so cut-offs and missing values
    land in both arms and on any stratum, which a hand-built pair cannot promise.
    """
    layout = random.Random(structure)  # noqa: S311 - test RNG, not crypto
    results = random.Random(outcomes)  # noqa: S311 - test RNG, not crypto
    commits = ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]
    hour = 0
    for _ in range(80):
        parent = f"parent-{layout.randrange(7)}"
        withheld = layout.random() < 0.3
        packs = layout.choice((1, 1, 2))
        build = layout.choice(commits)
        covered = layout.random() < 0.8
        brief = layout.randrange(100, 900)
        kind = layout.randrange(3)
        cut_off = results.random() < 0.25
        gone = results.random() < 0.1
        no_errors = results.random() < 0.1
        log.task(
            start=MONDAY + timedelta(hours=hour),
            parent=parent,
            arms=[withheld] * packs,
            turns=results.randrange(5, 60),
            prs_created=results.randrange(0, 3),
            commits=results.randrange(0, 5),
            tool_errors=None if no_errors else results.randrange(0, 6),
            ended_on_error=cut_off,
            measures_missing=gone,
            rate=rate,
            pack_metadata=[provenance(build)] * packs,
            covariates=(
                covariates_payload(brief_chars=brief, agent_type=f"kind-{kind}")
                if covered
                else None
            ),
        )
        hour += 1
    for index in range(7):
        log.join(
            f"parent-{index}",
            at=MONDAY + timedelta(hours=hour),
            pack_ids=[],
            parent=None,
            outcome=outcome_payload(
                turns=results.randrange(3, 30), ended_on_error=results.random() < 0.3
            ),
        )
        hour += 1
