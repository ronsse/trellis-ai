"""A synthetic store for the operational-mode tests, post-treatment values as knobs.

:func:`seed_store` writes the same experiment every time except for what
:class:`PostTreatment` names: the outcome magnitudes (turns, commits, PRs, tool
errors, tokens), which tasks are cut off, where the zeros and the missing
field fall, how many tool calls a session made, how much hook feedback it drew,
what its later packs were and how many pack ids the capture could not parse.
Every arm, build, covariate and timing flag a first pack settles, and which
joins carry an outcome block at all, are fixed by the task's index, so two
stores that differ only in a :class:`PostTreatment` differ only in values a
treatment could have moved.
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
    unparsed_pack_ids: int = 0


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
                start = MONDAY + timedelta(weeks=week, hours=slot, minutes=index)
                pack_ids = [
                    log.pack(
                        at=start,
                        withheld=withheld,
                        items=2 + slot,
                        metadata=provenance(commit),
                    )
                ]
                if index % 5 == 4 or post.later_packs_everywhere:
                    later_commit = COMMIT_B if post.later_pack_other_build else commit
                    pack_ids.append(
                        log.pack(
                            at=start + timedelta(minutes=1),
                            withheld=withheld
                            if post.later_pack_same_arm
                            else not withheld,
                            items=2,
                            metadata=provenance(later_commit),
                        )
                    )
                log.join(
                    f"task-{index:03d}",
                    at=start + timedelta(hours=1),
                    pack_ids=pack_ids,
                    parent=parent,
                    outcome=None if index == 11 else outcome,
                    pack_ids_unparsed=post.unparsed_pack_ids,
                    timing=(
                        timing_payload(
                            hook_before=False,
                            commit_before=False,
                            hook_messages=post.hook_messages,
                            ordinal=post.first_retrieval_ordinal,
                            calls=post.tool_calls,
                        )
                        if index % 4
                        else None
                    ),
                    covariates=(
                        covariates_payload(
                            brief_chars=100 + 7 * index,
                            agent_type="a" * 11 + str(index % 2) if index % 2 else None,
                        )
                        if index % 3
                        else None
                    ),
                )
                index += 1
    main = log.task(start=MONDAY + timedelta(days=2), parent=None, arms=[False])
    log.task(
        start=MONDAY + timedelta(days=2, hours=1),
        parent=main,
        arms=[True],
        turns=9 * magnitude,
        covariates=covariates_payload(),
    )
    log.sweep()
