"""A synthetic store for the operational-mode tests, post-treatment values as knobs.

:func:`seed_store` writes the same experiment every time except for what
:class:`PostTreatment` names: the outcome magnitudes (turns, commits, PRs, tool
errors, tokens), which tasks are cut off, where the zeros and the missing
field fall, how many tool calls a session made, how much hook feedback it drew,
what its later packs were (their arm, build and rate, and whether they carry a
holdout key at all), how many packs it drew, how long after its first pack its
join was written, whether the join was written again (and whether the second
join also changes the timing flags, covariates and first pack id the first
join fixed), whether it ended interrupted, how many pack ids the capture could
not parse, and which pack rows another surface wrote that no join names. Every
arm, build, covariate and timing flag a first pack settles, which joins carry an
outcome block at all, and when capture swept, are fixed by the task's index, so
two stores that differ only in a :class:`PostTreatment` differ only in values a
treatment could have moved.

A join is written before the first sweep that could admit its task (at most 25
minutes after the first pack, where the nightly tests settle for 30), because a
join written later would be missing from the night a sweep admitted it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from tests.unit.analyze._holdout_fixture import (
    MONDAY,
    HoldoutLog,
    covariates_payload,
    outcome_payload,
    provenance,
    timing_payload,
)

COMMIT_A = "a1b2c3d4e5f6"
COMMIT_B = "9f8e7d6c5b4a"
#: Tasks written by :func:`seed_store`, by the analysis's own reckoning:
#: 2 parents x 2 weeks x 6 tasks, then the odd ones.
TASKS = 24
#: Window ends that admit the seed's tasks a few at a time.
WINDOW_ENDS = [MONDAY + timedelta(minutes=40 * step) for step in range(13)] + [
    MONDAY + timedelta(hours=9 * step) for step in range(1, 40)
]


@dataclass(frozen=True)
class PostTreatment:
    """Every value the seed writes that follows a task's first pack.

    The defaults are the store :func:`seed_store` has always written: cut-offs
    in the withheld arm only, so a leak of the arm's cut-off count would show;
    one task with no ``assistant_turns``; later packs of the opposite arm on
    every fifth task.
    """

    magnitude: int = 1
    #: Which arm the cut-offs fall in: ``withheld``, ``served`` or ``none``.
    cut_offs_in: str = "withheld"
    #: Zeros of the commit and PR counts by arm, not by the task's index.
    zeros_by_arm: bool = False
    #: The task whose outcome lacks ``assistant_turns`` (a withheld one is 9).
    missing_turns_at: int = 7
    tool_calls: int = 10
    first_retrieval_ordinal: int = 3
    hook_messages: int = 0
    #: Later packs on every fifth task, in the same arm, and in the other build.
    later_pack_same_arm: bool = False
    later_pack_other_build: bool = False
    later_packs_everywhere: bool = False
    #: The rate a later pack was written at (every first pack is at 0.5).
    later_pack_rate: float = 0.5
    #: Write a later pack as a row from a build before the holdout (no key).
    later_pack_unkeyed: bool = False
    unparsed_pack_ids: int = 0
    #: Minutes from a task's first pack to its join, for (served, withheld) tasks.
    join_lag_minutes: tuple[int, int] = (10, 10)
    #: Write the join again 30 hours on, for tasks of this arm: ``none``,
    #: ``served``, ``withheld`` or ``all``. The second join differs in every
    #: post-treatment value.
    reemit_in: str = "none"
    #: Make the second join differ in what the first one fixed too: the timing
    #: flags (both set), the covariates and the first pack id.
    reemit_pretreatment: bool = False
    #: Pack rows no join names, written by another surface: ``none``,
    #: ``rate0`` (rate 0 rows), ``unkeyed`` (no holdout key) or ``both``,
    #: from two days before the first task on.
    stray_rows: str = "none"
    #: Mark the outcome ``ended_interrupted``, for tasks of this arm (same values).
    interrupted_in: str = "none"
    #: Packs drawn after the first, for (served, withheld) tasks, in their arm.
    extra_packs: tuple[int, int] = (0, 0)


def seed_store(
    log: HoldoutLog, *, magnitude: int = 1, post: PostTreatment | None = None
) -> None:
    """Tasks across two builds and weeks, cut-offs in the withheld arm only.

    ``magnitude`` multiplies every outcome count and nothing else; ``post``
    varies the other post-treatment values. One task carries no outcome block
    at all, whatever either says; covariates are present on two tasks in
    three, the agent type on half.
    """
    post = post or PostTreatment(magnitude=magnitude)
    magnitude = post.magnitude
    index = 0
    for parent in ("parent-a", "parent-b"):
        for week, commit in enumerate((COMMIT_A, COMMIT_B)):
            for slot in range(6):
                withheld = index % 3 == 0
                cut_arm = {"withheld": withheld, "served": not withheld}
                zero_key = withheld if post.zeros_by_arm else index % 2
                outcome = outcome_payload(
                    turns=(5 + index) * magnitude,
                    ended_on_error=cut_arm.get(post.cut_offs_in, False) and slot < 2,
                    prs_created=int(bool(zero_key)) * magnitude,
                    commits=(index % 4 if not post.zeros_by_arm else 2 * zero_key)
                    * magnitude,
                    tokens=(1_000 + index) * magnitude,
                    tool_errors=(index % 5) * magnitude,
                )
                if index == post.missing_turns_at:
                    outcome["assistant_turns"] = None
                arm_name = "withheld" if withheld else "served"
                if post.interrupted_in in (arm_name, "all"):
                    outcome["ended_interrupted"] = True
                start = MONDAY + timedelta(weeks=week, hours=slot, minutes=index)
                pack_ids = [
                    log.pack(
                        at=start,
                        withheld=withheld,
                        items=2 + slot,
                        metadata=provenance(commit),
                    )
                ]
                pack_ids.extend(
                    log.pack(
                        at=start + timedelta(minutes=2 + extra),
                        withheld=withheld,
                        items=2,
                        metadata=provenance(commit),
                    )
                    for extra in range(post.extra_packs[int(withheld)])
                )
                if index % 5 == 4 or post.later_packs_everywhere:
                    later_commit = COMMIT_B if post.later_pack_other_build else commit
                    pack_ids.append(
                        log.pack(
                            at=start + timedelta(minutes=1),
                            withheld=None
                            if post.later_pack_unkeyed
                            else withheld
                            if post.later_pack_same_arm
                            else not withheld,
                            items=2,
                            rate=post.later_pack_rate,
                            metadata=provenance(later_commit),
                        )
                    )
                timing = (
                    timing_payload(
                        hook_before=False,
                        commit_before=False,
                        hook_messages=post.hook_messages,
                        ordinal=post.first_retrieval_ordinal,
                        calls=post.tool_calls,
                    )
                    if index % 4
                    else None
                )
                covariates = (
                    covariates_payload(
                        brief_chars=100 + 7 * index,
                        agent_type="a" * 11 + str(index % 2) if index % 2 else None,
                    )
                    if index % 3
                    else None
                )
                lag = timedelta(minutes=post.join_lag_minutes[int(withheld)])
                log.join(
                    f"task-{index:03d}",
                    at=start + lag,
                    pack_ids=pack_ids,
                    parent=parent,
                    outcome=None if index == 11 else outcome,
                    pack_ids_unparsed=post.unparsed_pack_ids,
                    timing=timing,
                    covariates=covariates,
                )
                if post.reemit_in in (arm_name, "all"):
                    again = dict(outcome, assistant_turns=999, input_tokens=77_777)
                    second_ids, second_timing, second_covariates = (
                        pack_ids,
                        timing,
                        covariates,
                    )
                    if post.reemit_pretreatment:
                        other = log.pack(
                            at=start + timedelta(minutes=3),
                            withheld=not withheld,
                            items=2,
                            metadata=provenance(COMMIT_B),
                        )
                        second_ids = [other, *pack_ids[1:]]
                        second_timing = timing_payload(
                            hook_before=True,
                            commit_before=True,
                            hook_messages=1,
                            ordinal=1,
                            calls=2,
                        )
                        second_covariates = covariates_payload(
                            brief_chars=9_999, agent_type="z" * 12
                        )
                    log.join(
                        f"task-{index:03d}",
                        at=start + timedelta(hours=30),
                        pack_ids=second_ids,
                        parent=parent,
                        outcome=None if index == 11 else again,
                        pack_ids_unparsed=post.unparsed_pack_ids,
                        timing=second_timing,
                        covariates=second_covariates,
                    )
                index += 1
    for step in range(8) if post.stray_rows != "none" else ():
        at = MONDAY + timedelta(days=step - 2, hours=5)
        if post.stray_rows in ("rate0", "both"):
            log.pack(
                at=at,
                withheld=step % 2 == 0,
                items=1,
                rate=0.0,
                metadata=provenance(COMMIT_B),
            )
        if post.stray_rows in ("unkeyed", "both"):
            log.pack(
                at=at + timedelta(minutes=7),
                withheld=None,
                items=1,
                metadata=provenance(COMMIT_B),
            )
    main = log.task(start=MONDAY + timedelta(days=2), parent=None, arms=[False])
    log.task(
        start=MONDAY + timedelta(days=2, hours=1),
        parent=main,
        arms=[True],
        turns=9 * magnitude,
        covariates=covariates_payload(),
    )
    log.sweep()
    for end in WINDOW_ENDS:
        log.sweep(at=end - timedelta(seconds=1))
