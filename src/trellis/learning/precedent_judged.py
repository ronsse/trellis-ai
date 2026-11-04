"""The judged operation behind a ``local_model`` precedent record.

A ``local_model`` candidate is accepted only when the event log holds the
``MEMORY_OP_JUDGED`` event that produced it. That ties acceptance to something a
caller cannot supply: ``requested_by`` is the caller's own claim on every
surface, but ``MEMORY_OP_JUDGED`` is emitted only by in-process code (the local
distillation command among them). No MCP tool, REST route or governed operation
takes a caller-chosen event type, source or subject, so a forged record has no
judged operation to point at.

The event is found by its subject, never by a scan. A distillation record names
its trace (``evidence.trace_ids``); the event is ``op_type=distillation``,
``decision=lesson``, from the distil prompt's ``source``, about that trace. A
merge record names the two candidates it combined (``evidence.merged_from``); the
event is ``op_type=reconciliation``, ``decision=same``, from the merge prompt's
``source``, about the ``precedent_pair`` those two ids make.

This proves a model verdict was logged for the trace or pair. It does not prove the
record's text is what the model said, since the judged payload is a pinned
contract with no field for it.

This module is light on purpose: the governed handler imports it, so it takes
nothing from the model client or the routing layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from trellis.schemas.memory_op import (
    REF_TYPE_PRECEDENT_PAIR,
    REF_TYPE_TRACE,
    JudgedOpType,
)
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trellis.schemas.precedent_record import PrecedentRecord
    from trellis.stores.base.event_log import EventLog

#: Bump on any change to the prompt, the cut limits or the reply shape. Judged
#: events carry it, so pairs from different prompts stay separable.
DISTILL_PROMPT_VERSION: Final = "precedent-distill/v1"
MERGE_PROMPT_VERSION: Final = "precedent-merge/v1"

#: The verdicts that produce a record; any other verdict produced none.
DECISION_LESSON: Final = "lesson"
DECISION_SAME: Final = "same"

#: Most events read for one subject. The lookup is keyed on the subject, source and
#: verdict, so every row it returns would do; the cap only bounds a subject that
#: was judged on many reruns.
JUDGED_LOOKUP_LIMIT: Final = 5


def judged_source(prompt_version: str) -> str:
    """The ``source`` a judged event carries for a prompt version."""
    return f"learning:{prompt_version}"


def pair_ref_id(candidate_ids: Iterable[str]) -> str:
    """The ``precedent_pair`` ``ref_id`` for candidate ids: sorted, ``|``-joined."""
    return "|".join(sorted(candidate_ids))


def _logged(
    event_log: EventLog,
    *,
    op_type: JudgedOpType,
    decision: str,
    prompt_version: str,
    ref_type: str,
    ref_id: str,
) -> bool:
    events = event_log.get_events(
        event_type=EventType.MEMORY_OP_JUDGED,
        entity_id=ref_id,
        source=judged_source(prompt_version),
        limit=JUDGED_LOOKUP_LIMIT,
        payload_filters={"op_type": op_type.value, "decision": decision},
    )
    for event in events:
        subject = (event.payload or {}).get("subject_ref")
        if (
            isinstance(subject, dict)
            and subject.get("ref_type") == ref_type
            and subject.get("ref_id") == ref_id
        ):
            return True
    return False


def judged_op_backs(event_log: EventLog, record: PrecedentRecord) -> bool:
    """Whether the event log holds the judged operation that produced ``record``.

    A record with ``merged_from`` is a merge and needs the pair's ``same``
    verdict; any other is a distillation and needs its one trace's ``lesson``
    verdict. A record naming other than exactly two merged candidates, or other
    than exactly one trace, has no judged operation it could be backed by.
    """
    merged_from = record.evidence.merged_from
    if merged_from:
        if len(merged_from) != 2 or len(set(merged_from)) != 2:  # noqa: PLR2004
            return False
        return _logged(
            event_log,
            op_type=JudgedOpType.RECONCILIATION,
            decision=DECISION_SAME,
            prompt_version=MERGE_PROMPT_VERSION,
            ref_type=REF_TYPE_PRECEDENT_PAIR,
            ref_id=pair_ref_id(merged_from),
        )
    trace_ids = record.evidence.trace_ids
    if len(trace_ids) != 1:
        return False
    return _logged(
        event_log,
        op_type=JudgedOpType.DISTILLATION,
        decision=DECISION_LESSON,
        prompt_version=DISTILL_PROMPT_VERSION,
        ref_type=REF_TYPE_TRACE,
        ref_id=trace_ids[0],
    )


__all__ = [
    "DECISION_LESSON",
    "DECISION_SAME",
    "DISTILL_PROMPT_VERSION",
    "JUDGED_LOOKUP_LIMIT",
    "MERGE_PROMPT_VERSION",
    "judged_op_backs",
    "judged_source",
    "pair_ref_id",
]
