"""A synthetic store for the operational-mode tests, with outcome magnitudes as a knob.

:func:`seed_store` writes the same experiment every time except for the
outcome magnitudes (turns, commits, PRs, tool errors, tokens), which scale with
``magnitude``. Which tasks are cut off, which lack an outcome field, and every
arm, build, covariate and timing value are fixed by the task's index, so two
stores seeded with different ``magnitude`` differ only in outcome values.
"""

from __future__ import annotations

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


def seed_store(log: HoldoutLog, *, magnitude: int = 1) -> None:
    """Tasks across two builds and weeks, cut-offs in the withheld arm only.

    ``magnitude`` multiplies every outcome count and nothing else. Withheld
    tasks (every third) are the only cut-offs, so a leak of the arm's cut-off
    count would show; one task has no ``assistant_turns`` and one carries no
    outcome block at all; covariates are present on two tasks in three, the
    agent type on half.
    """
    index = 0
    for parent in ("parent-a", "parent-b"):
        for week, commit in enumerate((COMMIT_A, COMMIT_B)):
            for slot in range(6):
                withheld = index % 3 == 0
                outcome = outcome_payload(
                    turns=(5 + index) * magnitude,
                    ended_on_error=withheld and slot < 2,
                    prs_created=(index % 2) * magnitude,
                    commits=(index % 4) * magnitude,
                    tokens=(1_000 + index) * magnitude,
                    tool_errors=(index % 5) * magnitude,
                )
                if index == 7:
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
                if index % 5 == 4:
                    pack_ids.append(
                        log.pack(
                            at=start + timedelta(minutes=1),
                            withheld=not withheld,
                            items=2,
                            metadata=provenance(commit),
                        )
                    )
                log.join(
                    f"task-{index:03d}",
                    at=start + timedelta(hours=1),
                    pack_ids=pack_ids,
                    parent=parent,
                    outcome=None if index == 11 else outcome,
                    timing=(
                        timing_payload(hook_before=False, commit_before=False)
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
