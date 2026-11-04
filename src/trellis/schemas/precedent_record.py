"""The structured precedent: situation, action, because, evidence.

A promoted precedent today is an excerpt of an item that was already served,
re-wrapped, so it says nothing its source did not. A :class:`PrecedentRecord`
is the lesson itself, in four parts: the *situation* that makes it apply, the
*action* to do or avoid, *because* (why, from the trace), and the *evidence*
it came from.

A record is a **candidate** and nothing more here. It lives only as the payload
of one ``PRECEDENT_CANDIDATE_MINED`` event written through the governed
``precedent.candidate`` operation. It is never written to the document, graph
or vector store and no retrieval surface reads it. ``lifecycle`` is the
record's own vocabulary (it is not a ``LifecycleState`` value); activation,
meaning minting a servable precedent from a candidate, is a separate step that
this module does not take.

``provenance_tier`` says who or what filled the record. ``evidence.feedback_ids``
and ``evidence.pack_ids`` stay empty from the deterministic tier and exist so a
later tier can fill them.
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal

from pydantic import Field

from trellis.core.base import TrellisModel

#: Per-field text bounds. The distiller truncates to these with
#: ``truncate_excerpt``; the schema refuses anything longer, so a hand-built
#: record cannot smuggle an unbounded body into the append-only event log.
MAX_TRIGGER_CHARS = 240
MAX_ACTION_CHARS = 240
MAX_BECAUSE_CHARS = 400
MAX_INTENT_FAMILY_CHARS = 80
MAX_DOMAIN_CHARS = 120
MAX_ID_CHARS = 200
MAX_LIST_ITEMS = 20

#: The record's own lifecycle vocabulary. One value for now: activation is a
#: later step and would add its own states.
RECORD_LIFECYCLE_CANDIDATE: Final = "candidate"

ActionKind = Literal["do", "avoid"]
ProvenanceTier = Literal["deterministic", "local_model", "human"]


class PrecedentSituation(TrellisModel):
    """When the lesson applies."""

    intent_family: str = Field(max_length=MAX_INTENT_FAMILY_CHARS)
    domain: str | None = Field(default=None, max_length=MAX_DOMAIN_CHARS)
    entity_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    trigger: str = Field(max_length=MAX_TRIGGER_CHARS)


class PrecedentAction(TrellisModel):
    """What to do, or what to avoid."""

    kind: ActionKind
    text: str = Field(min_length=1, max_length=MAX_ACTION_CHARS)


class PrecedentEvidence(TrellisModel):
    """Where the lesson came from."""

    trace_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    step_indexes: list[int] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    feedback_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    pack_ids: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class PrecedentRecord(TrellisModel):
    """One candidate lesson. Never served; see the module docstring."""

    candidate_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    rule: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    situation: PrecedentSituation
    action: PrecedentAction
    because: str = Field(max_length=MAX_BECAUSE_CHARS)
    evidence: PrecedentEvidence
    confidence: float = Field(ge=0.0, le=1.0)
    provenance_tier: ProvenanceTier = "deterministic"
    distiller_version: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    lifecycle: Literal["candidate"] = RECORD_LIFECYCLE_CANDIDATE

    def expected_candidate_id(self) -> str:
        """The id this record's own content derives; ``candidate_id`` must equal it."""
        return derive_candidate_id(
            trace_ids=self.evidence.trace_ids,
            rule=self.rule,
            step_indexes=self.evidence.step_indexes,
            distiller_version=self.distiller_version,
        )


def derive_candidate_id(
    *,
    trace_ids: list[str],
    rule: str,
    step_indexes: list[int],
    distiller_version: str,
) -> str:
    """A stable id from (trace ids, rule, step indexes, distiller version).

    The same input always gives the same id, so a rerun over the same trace
    derives the candidates it derived before and the idempotency key catches
    them. A new ``distiller_version`` mints new ids on purpose: a changed rule
    is a different candidate.
    """
    parts = [
        ",".join(trace_ids),
        rule,
        ",".join(str(i) for i in step_indexes),
        distiller_version,
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return f"pcand_{digest[:24]}"


__all__ = [
    "MAX_ACTION_CHARS",
    "MAX_BECAUSE_CHARS",
    "MAX_TRIGGER_CHARS",
    "RECORD_LIFECYCLE_CANDIDATE",
    "PrecedentAction",
    "PrecedentEvidence",
    "PrecedentRecord",
    "PrecedentSituation",
    "derive_candidate_id",
]
