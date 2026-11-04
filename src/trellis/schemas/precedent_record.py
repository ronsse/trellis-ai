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
import json
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal

from pydantic import Field, StringConstraints

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

#: An id list holds ids, each bounded like ``candidate_id``: a list cap alone
#: would let twenty unbounded strings into the append-only event log.
BoundedId = Annotated[str, StringConstraints(max_length=MAX_ID_CHARS)]

#: The record's own lifecycle vocabulary. One value for now: activation is a
#: later step and would add its own states.
RECORD_LIFECYCLE_CANDIDATE: Final = "candidate"

ActionKind = Literal["do", "avoid"]
ProvenanceTier = Literal["deterministic", "local_model", "human"]


class PrecedentSituation(TrellisModel):
    """When the lesson applies."""

    intent_family: str = Field(max_length=MAX_INTENT_FAMILY_CHARS)
    domain: str | None = Field(default=None, max_length=MAX_DOMAIN_CHARS)
    entity_ids: list[BoundedId] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    trigger: str = Field(max_length=MAX_TRIGGER_CHARS)


class PrecedentAction(TrellisModel):
    """What to do, or what to avoid."""

    kind: ActionKind
    text: str = Field(min_length=1, max_length=MAX_ACTION_CHARS)


class PrecedentEvidence(TrellisModel):
    """Where the lesson came from."""

    trace_ids: list[BoundedId] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    step_indexes: list[int] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    feedback_ids: list[BoundedId] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )
    pack_ids: list[BoundedId] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    #: Candidate ids a merge combined (``precedent.merge``). Empty for every
    #: other record, and then left out of the id's input so records recorded
    #: before this field existed keep the ids they were recorded under.
    merged_from: list[BoundedId] = Field(
        default_factory=list, max_length=MAX_LIST_ITEMS
    )


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
        return derive_candidate_id(self.model_dump(mode="json"))

    @classmethod
    def derive(cls, **content: Any) -> PrecedentRecord:
        """Build a record whose ``candidate_id`` is the id its content derives.

        ``content`` is every field but ``candidate_id``. The record is built
        first, with a placeholder id, and the id is then derived from the
        record's own dump, so no caller lists the fields that go into it.
        """
        draft = cls(candidate_id=_PENDING_ID, **content)
        return draft.model_copy(update={"candidate_id": draft.expected_candidate_id()})


#: The record fields the id does not cover. ``candidate_id`` is the id itself;
#: ``lifecycle`` has one legal value, so it cannot tell two records apart.
#: Everything else a record carries, including a field added later, is hashed:
#: the canonical form is built from the model dump minus these keys, never from
#: a hand-kept list (``tests/unit/schemas/test_precedent_record_id.py``).
ID_EXCLUDED_FIELDS: Final = frozenset({"candidate_id", "lifecycle"})

_PENDING_ID: Final = "pcand_pending"


def derive_candidate_id(record: Mapping[str, Any]) -> str:
    """A stable id from a record's whole content.

    ``record`` is a record's JSON dump. Every key but :data:`ID_EXCLUDED_FIELDS`
    is hashed, nested fields included, so a forged record cannot carry the id
    the distiller derives for a real trace: its own content derives another,
    and ``candidate_id`` must equal that. The same input always gives the same
    id, so a rerun over the same trace derives the candidates it derived before
    and the idempotency key catches them. A new ``distiller_version`` mints new
    ids on purpose: a changed rule is a different candidate.

    The input is encoded as JSON with sorted keys, not joined with a separator,
    so ``["x,y"]`` and ``["x", "y"]`` cannot collide. Every list under
    ``evidence`` is sorted first: the same lesson cited in another order is the
    same lesson. ``ensure_ascii`` keeps the encoding total over any string.
    """
    canonical = {k: v for k, v in record.items() if k not in ID_EXCLUDED_FIELDS}
    evidence = canonical.get("evidence")
    if isinstance(evidence, Mapping):
        canonical["evidence"] = {
            key: sorted(value) if isinstance(value, list) else value
            for key, value in evidence.items()
            if not (key == "merged_from" and value == [])
        }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    return f"pcand_{digest[:24]}"


__all__ = [
    "MAX_ACTION_CHARS",
    "MAX_BECAUSE_CHARS",
    "MAX_DOMAIN_CHARS",
    "MAX_ID_CHARS",
    "MAX_LIST_ITEMS",
    "MAX_TRIGGER_CHARS",
    "ID_EXCLUDED_FIELDS",
    "RECORD_LIFECYCLE_CANDIDATE",
    "PrecedentAction",
    "PrecedentEvidence",
    "PrecedentRecord",
    "PrecedentSituation",
    "derive_candidate_id",
]
