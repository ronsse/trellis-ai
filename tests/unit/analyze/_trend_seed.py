"""Synthetic event logs for the weekly trend report, shared by its three suites.

The scenario spans three ISO weeks and three builds, ending on a Wednesday so
the last week is partial. Every timestamp is explicit and the report's
``until`` is fixed at :data:`UNTIL`, so nothing here depends on the wall clock.

====== ============================== ========================================
Week   Build (commit)                 What is seeded
====== ============================== ========================================
W43    ``aaaaaaa``                    2 packs in one session, one precedent,
                                      a judged memory served and cited, 3
                                      captured sessions, 3 accepted capture
                                      writes and 1 rejected
W44    ``aaaaaaa`` then ``bbbbbbb``   6 attributed packs (one without a
                                      session, one precedent), a judged
                                      memory never served, 10 captured
                                      sessions of which 4 name a pack
W45    a build with no commit         1 pack in a partial week
====== ============================== ========================================
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trellis.core.pack_holdout import HOLDOUT_KEY
from trellis.core.write_provenance import WRITE_PROVENANCE_KEY
from trellis.schemas.memory_op import (
    REF_TYPE_DOCUMENT,
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import Event, EventLog, EventType

UNTIL = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
W43 = datetime(2026, 10, 19, tzinfo=UTC)
W44 = datetime(2026, 10, 26, tzinfo=UTC)
W45 = datetime(2026, 11, 2, tzinfo=UTC)

OLD = {"version": "0.9.1.dev100+gaaaaaaa", "commit": "aaaaaaa"}
NEW = {"version": "0.9.1.dev200+gbbbbbbb", "commit": "bbbbbbb"}
UNIDENTIFIED = {"version": "0.9.2", "commit": None}
HELD_BUILD = {"version": "0.9.1.dev300+ghhhhhhh", "commit": "hhhhhhh"}

PRECEDENT_A = "precedent://learning/alpha"
PRECEDENT_B = "precedent://learning/beta"


def _append(
    log: EventLog,
    event_type: EventType,
    *,
    at: datetime,
    entity_id: str | None,
    payload: dict[str, Any],
    stamp: dict[str, Any] | None = None,
    source: str = "test",
) -> None:
    log.append(
        Event(
            event_type=event_type,
            source=source,
            entity_id=entity_id,
            occurred_at=at,
            recorded_at=at,
            payload=payload,
            metadata={} if stamp is None else {WRITE_PROVENANCE_KEY: stamp},
        )
    )


def pack(
    log: EventLog,
    pack_id: str,
    at: datetime,
    stamp: dict[str, Any] | None,
    items: list[tuple[str, int]],
    *,
    session: str | None = None,
    phase: str | None = None,
    holdout: bool = False,
) -> None:
    """One flat ``PACK_ASSEMBLED``; items are ``(item_id, estimated_tokens)``."""
    payload: dict[str, Any] = {
        "intent": "test",
        "injected_item_ids": [item_id for item_id, _ in items],
        "injected_items": [
            {"item_id": item_id, "estimated_tokens": tokens, "rank": rank}
            for rank, (item_id, tokens) in enumerate(items)
        ],
        "session_id": session,
        "retrieval_phase": phase,
    }
    if holdout:
        payload[HOLDOUT_KEY] = True
        payload["injected_item_ids"] = []
        payload["injected_items"] = []
    _append(
        log,
        EventType.PACK_ASSEMBLED,
        at=at,
        entity_id=pack_id,
        payload=payload,
        stamp=stamp,
    )


def feedback(
    log: EventLog,
    pack_id: str,
    at: datetime,
    *,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
    source: str = "test",
) -> None:
    _append(
        log,
        EventType.FEEDBACK_RECORDED,
        at=at,
        entity_id=pack_id,
        source=source,
        payload={
            "pack_id": pack_id,
            "helpful_item_ids": list(helpful),
            "unhelpful_item_ids": list(unhelpful),
            "rating": 0.5,
            "success": True,
        },
    )


def judged(log: EventLog, ref_id: str, at: datetime) -> None:
    payload = MemoryOpJudgedPayload(
        op_type=JudgedOpType.CLASSIFICATION,
        model_id="test-model",
        input_digest=InputDigest(hash="0123abcd", length=42),
        decision="reference",
        confidence=0.9,
        subject_ref=SubjectRef(ref_type=REF_TYPE_DOCUMENT, ref_id=ref_id),
    ).model_dump(mode="json")
    _append(
        log,
        EventType.MEMORY_OP_JUDGED,
        at=at,
        entity_id=ref_id,
        payload=payload,
    )


def capture(
    log: EventLog, session: str, at: datetime, pack_ids: tuple[str, ...] = ()
) -> None:
    """A ``CAPTURE_SESSION_PACKS`` join row: no outcome, no covariate."""
    _append(
        log,
        EventType.CAPTURE_SESSION_PACKS,
        at=at,
        entity_id=session,
        payload={"pack_ids": list(pack_ids)},
    )


def accepted_write(log: EventLog, at: datetime, tool: str = "mcp:save_memory") -> None:
    _append(
        log,
        EventType.MUTATION_EXECUTED,
        at=at,
        entity_id=None,
        payload={"requested_by": tool, "operation": "memory.save"},
    )


def rejected_write(log: EventLog, at: datetime, tool: str = "save_memory") -> None:
    _append(
        log,
        EventType.WRITE_REJECTED,
        at=at,
        entity_id=None,
        payload={"tool": tool, "rejections": [{"kind": "missing", "loc": "x"}]},
    )


def seed(log: EventLog, *, with_holdout: bool = False) -> None:
    """The three-week scenario; ``with_holdout`` adds a withheld pack and its grade."""
    day = timedelta(days=1)

    # --- W43: one build, one session that retrieved twice -------------------
    judged(log, "doc1", W43 + timedelta(hours=8))
    pack(
        log,
        "a1",
        W43 + day,
        OLD,
        [("doc1", 100), ("doc1b", 100)],
        session="s1",
        phase="pre_work",
    )
    pack(
        log,
        "a2",
        W43 + 2 * day,
        OLD,
        [(PRECEDENT_A, 50)],
        session="s1",
        phase="mid_task",
    )
    feedback(log, "a1", W43 + day + timedelta(hours=1), helpful=("doc1",), source="mcp")
    for index in range(3):
        capture(
            log,
            f"cs{index}",
            W43 + 3 * day + timedelta(hours=index),
            pack_ids=("a1",) if index < 2 else (),
        )
    for index in range(3):
        accepted_write(log, W43 + 3 * day + timedelta(minutes=index))
    rejected_write(log, W43 + 3 * day + timedelta(minutes=10))

    # --- W44: a build change inside the week --------------------------------
    judged(log, "doc2", W44 + timedelta(hours=8))
    for index in range(1, 6):
        pack(
            log,
            f"b{index}",
            W44 + index * timedelta(hours=6),
            OLD if index <= 2 else NEW,
            [(f"doc:b{index}:a", 100), (f"doc:b{index}:b", 100)],
            session=f"s{index + 1}",
            phase="pre_work",
        )
        feedback(
            log,
            f"b{index}",
            W44 + index * timedelta(hours=6) + timedelta(minutes=5),
            helpful=(f"doc:b{index}:a",),
            unhelpful=(f"doc:b{index}:b",),
        )
    # A session-less pack with no stated phase, carrying the second precedent.
    pack(log, "b6", W44 + 2 * day, NEW, [(PRECEDENT_B, 100)])
    feedback(log, "b6", W44 + 2 * day + timedelta(minutes=5), helpful=(PRECEDENT_B,))
    for index in range(10):
        capture(
            log,
            f"ds{index}",
            W44 + 3 * day + timedelta(hours=index),
            pack_ids=(f"b{index + 1}",) if index < 4 else (),
        )

    # --- W45: partial week, a build that stamped no commit ------------------
    pack(
        log,
        "c1",
        W45 + timedelta(hours=9),
        UNIDENTIFIED,
        [("doc:c1", 10)],
        session="s9",
    )

    if with_holdout:
        # Withheld in W44, in a session that also has a served pack, graded,
        # on a build nothing else ran: if any metric read it, a count, a
        # session, a token ratio or the builds list would move.
        pack(
            log,
            "held1",
            W44 + 4 * day,
            HELD_BUILD,
            [],
            session="s2",
            phase="pre_work",
            holdout=True,
        )
        feedback(
            log,
            "held1",
            W44 + 4 * day + timedelta(minutes=5),
            helpful=("doc:held",),
        )
