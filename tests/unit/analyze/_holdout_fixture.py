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


def outcome_payload(
    *,
    turns: int,
    ended_on_error: bool = False,
    prs_created: int = 0,
    commits: int = 0,
    tokens: int | None = 10_000,
) -> dict[str, Any]:
    """A ``SessionOutcome.to_payload()`` dict, every field present."""
    return {
        "tool_calls": turns,
        "tool_errors": 0,
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

    def join(
        self,
        task_id: str,
        *,
        at: datetime,
        pack_ids: Sequence[str],
        parent: str | None,
        outcome: dict[str, Any] | None,
    ) -> None:
        """Append one ``CAPTURE_SESSION_PACKS`` join for ``task_id``."""
        payload: dict[str, Any] = {
            "pack_ids": list(pack_ids),
            "retrieval_results": len(pack_ids),
            "retrieval_errors": 0,
            "pack_ids_unparsed": 0,
            "parent_session_id": parent,
            "source_system": SOURCE_SYSTEM,
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
