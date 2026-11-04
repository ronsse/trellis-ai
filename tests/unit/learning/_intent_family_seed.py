"""A synthetic event log for the intent-family relabel report, shared by its suites.

Every timestamp is explicit and the window's end is fixed at :data:`UNTIL`, so
nothing here depends on the wall clock. The counts the suites assert were
worked out by hand from this table, not read back from the report.

Under the shipped rules and the legacy ``normalize_intent_family`` map, each
group is ``intent`` (packs) -> new family / rule id, legacy family:

- ``fix the parser crash N`` (17 packs, ``fix-00``..``fix-16``): bugfix /
  bugfix.fix, general_context
- ``flaky ingest job N`` (3, ``fix-17``..``fix-19``): bugfix / bugfix.defect,
  general_context
- ``review PR #12`` (5, ``rev-*``): review / review.verbs, general_context
- ``add a regression test for the cache`` (3, ``tst-*``): test / test.core,
  validation_diagnostics
- ``write the release notes`` (2, ``doc-*``): docs / docs.core, general_context
- ``check data quality of the orders dataset`` (2, ``dat-*``): data /
  data.platform, validation_diagnostics
- ``redeploy the stack`` (1, ``ops-0``): ops / ops.deploy, general_context
- ``hello there N`` (3, ``gen-0``..``gen-2``): general / fallback,
  general_context
- ``latest news`` (1, ``gen-3``): general / fallback, validation_diagnostics
  (the legacy map matches the substring "test" inside "latest")
- an empty intent (1, ``gen-4``): general / fallback, general_context

Also seeded and expected to stay out of every count: a pack with no item rows
(``norows``), a pack before the window (``old``), and, unless ``holdout=False``,
a held-out pack with feedback naming it (``held``).

Grading: all 20 ``fix`` packs carry feedback citing two helpful items and one
unhelpful (ten at rating 1.0, ten at 0.5). The five ``rev`` packs cite one
helpful item each; ``rev-0`` has two feedback events naming the same item, so
it unions to one. ``doc-0`` has a rating and no citation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.stores.base.event_log import Event, EventLog, EventType

UNTIL = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
BEFORE_WINDOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

#: A string no rule keyword and no legacy keyword can match.
SENTINEL = "zq7-sentinel-4d1e9b"


def item(pack_id: str, index: int) -> str:
    return f"doc:{pack_id}:{index}"


def _append(
    log: EventLog,
    event_type: EventType,
    *,
    at: datetime,
    entity_id: str,
    payload: dict[str, Any],
) -> None:
    log.append(
        Event(
            event_type=event_type,
            source="test",
            entity_id=entity_id,
            occurred_at=at,
            recorded_at=at,
            payload=payload,
            metadata={},
        )
    )


def pack(
    log: EventLog,
    pack_id: str,
    intent: str,
    *,
    at: datetime = AT,
    rows: bool = True,
    holdout: bool = False,
    stamp: str | None = None,
) -> None:
    """One flat ``PACK_ASSEMBLED`` serving three items (none when ``rows`` is off)."""
    ids = [item(pack_id, k) for k in range(3)] if rows and not holdout else []
    payload: dict[str, Any] = {
        "intent": intent,
        "injected_item_ids": ids,
        "injected_items": [
            {"item_id": item_id, "estimated_tokens": 10, "rank": rank}
            for rank, item_id in enumerate(ids)
        ],
    }
    if stamp is not None:
        payload["intent_family"] = stamp
    if holdout:
        payload[HOLDOUT_KEY] = True
    _append(log, EventType.PACK_ASSEMBLED, at=at, entity_id=pack_id, payload=payload)


def feedback(
    log: EventLog,
    pack_id: str,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
    rating: float | None = None,
    at: datetime = AT,
) -> None:
    payload: dict[str, Any] = {
        "pack_id": pack_id,
        "helpful_item_ids": list(helpful),
        "unhelpful_item_ids": list(unhelpful),
    }
    if rating is not None:
        payload["rating"] = rating
        payload["success"] = rating >= 0.5
    _append(log, EventType.FEEDBACK_RECORDED, at=at, entity_id=pack_id, payload=payload)


def grade(
    log: EventLog,
    pack_id: str,
    *,
    helpful: int = 0,
    unhelpful: int = 0,
    rating: float | None = None,
) -> None:
    """Feedback citing the pack's first ``helpful`` items, then ``unhelpful`` more."""
    feedback(
        log,
        pack_id,
        helpful=tuple(item(pack_id, k) for k in range(helpful)),
        unhelpful=tuple(item(pack_id, k) for k in range(helpful, helpful + unhelpful)),
        rating=rating,
    )


def seed(log: EventLog, *, holdout: bool = True) -> None:
    for i in range(17):
        pack(log, f"fix-{i:02d}", f"fix the parser crash {i}")
    for i in range(17, 20):
        pack(log, f"fix-{i:02d}", f"flaky ingest job {i}")
    for i in range(20):
        grade(
            log, f"fix-{i:02d}", helpful=2, unhelpful=1, rating=1.0 if i < 10 else 0.5
        )

    for i in range(5):
        pack(log, f"rev-{i}", "review PR #12")
    grade(log, "rev-0", helpful=1, rating=0.0)
    grade(log, "rev-0", helpful=1, rating=1.0)
    for i in range(1, 5):
        grade(log, f"rev-{i}", helpful=1, rating=1.0)

    for i in range(3):
        pack(log, f"tst-{i}", "add a regression test for the cache")
    for i in range(2):
        pack(log, f"doc-{i}", "write the release notes")
    grade(log, "doc-0", rating=0.8)
    for i in range(2):
        pack(log, f"dat-{i}", "check data quality of the orders dataset")
    pack(log, "ops-0", "redeploy the stack")

    pack(log, "gen-0", "hello there 0", stamp="custom_family")
    pack(log, "gen-1", "hello there 1", stamp="general_context")
    pack(log, "gen-2", "hello there 2")
    pack(log, "gen-3", "latest news")
    pack(log, "gen-4", "")

    pack(log, "norows", "refactor the writer", rows=False)
    feedback(log, "norows", helpful=(item("norows", 0),), rating=1.0)
    pack(log, "old", "fix old crash", at=BEFORE_WINDOW)

    if holdout:
        pack(log, "held", "deploy the stack", holdout=True)
        feedback(log, "held", helpful=(item("held", 0),), rating=0.1)


def seed_sentinel(log: EventLog) -> None:
    """A graded pack whose intent carries :data:`SENTINEL`."""
    pack(log, "sentinel-0", f"fix the {SENTINEL} crash")
    grade(log, "sentinel-0", helpful=1, unhelpful=1, rating=0.7)
