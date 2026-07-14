"""Judged-memory-operation event payload — the training-pair contract.

North star (``docs/design/plan-memory-lifecycle.md`` §0.1): every judged
memory operation — extraction verdicts, reconciliation
ADD/UPDATE/SUPERSEDE/NOOP calls, distillation summaries, curation
verdicts — has the shape of a training example ``(input context,
decision, downstream outcome)``. Logging them from day one accrues the
dataset a future *local memory model* trains on; the dataset accrues now
or never.

This module ships the **payload contract** for the
:attr:`~trellis.stores.base.event_log.EventType.MEMORY_OP_JUDGED` event.
Emission from the judged-operation paths, and the feedback-attribution
join that supplies the downstream-outcome *label*, land separately
(#263). Core's deliverable ends at the payload shape plus the join being
possible; the training exporter lives in the operator ``trellis-evals``
repo, not here.

**Leak-safe by construction.** Verdicts about
:class:`~trellis.schemas.classification.DataClassification`-restricted
content must not carry the content. The payload carries only content
*digests* (hash + length + source refs), item/subject ids, a verdict
label, a model identifier, and a confidence score — never raw memory
content or model prose, because the event log has a different
access/retention profile than the doc store. The training-export step
re-resolves ``subject_ref`` / ``source_refs`` through the same
access-control path as any reader, so restricted examples drop out of
exports for unscoped callers automatically. Do not add a raw-content
field "because it's just telemetry."
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from trellis.core.base import TrellisModel


class JudgedOpType(StrEnum):
    """Which judged memory-lifecycle stage produced the verdict.

    The judged stages enumerated in ``plan-memory-lifecycle.md`` §0.1. A
    closed set today; adding a stage is an enum addition — the same
    sanctioned shape as adding an
    :class:`~trellis.stores.base.event_log.EventType` member.
    """

    EXTRACTION = "extraction"
    RECONCILIATION = "reconciliation"
    DISTILLATION = "distillation"
    CURATION = "curation"


class InputDigest(TrellisModel):
    """Leak-safe fingerprint of the input a judged op saw.

    Hash + length + source refs — never the raw content. ``hash`` is a
    truncated SHA-256 hex digest (see
    :func:`trellis.core.hashing.content_hash`); ``length`` is the
    character length of the hashed input, so consumers can bucket by
    input size without re-resolving it; ``source_refs`` are opaque
    pointers the export step re-resolves through access control.
    """

    hash: str
    """Truncated SHA-256 hex digest of the judged input. Never the
    content itself."""

    length: int = Field(ge=0)
    """Character length of the hashed input — a size signal that carries
    no content."""

    source_refs: list[str] = Field(default_factory=list)
    """Opaque pointers (``doc_id`` / ``entity_id`` / ``trace_id`` / URN)
    to the input's sources. Re-resolved through access control at export
    time so restricted sources drop out for unscoped callers."""


class SubjectRef(TrellisModel):
    """Leak-safe reference to the memory item the verdict is *about*.

    Identity only — a ``ref_type`` discriminator plus an opaque
    ``ref_id`` — so the payload can be joined to feedback attribution
    (the downstream-outcome *label*) and re-resolved through access
    control at export time. No name, title, or content.
    """

    ref_type: str
    """Open-string kind of the referent (``doc`` / ``entity`` /
    ``observation`` / ...), per the CLAUDE.md type-extensibility rule."""

    ref_id: str
    """Opaque identifier of the referent within its store."""


class MemoryOpJudgedPayload(TrellisModel):
    """Typed payload for ``memory_op.judged`` — one half of a training pair.

    Carries the ``(input, decision)`` half of a training example; the
    downstream-outcome *label* is supplied later by the feedback
    attribution join (mirrors :mod:`trellis.learning.pack_observations`;
    #263). Consumers reading a raw event payload dict can re-validate it
    by constructing this model.

    The field set *is* the whole contract — ``extra="forbid"`` (via
    :class:`~trellis.core.base.TrellisModel`) rejects anything else, and
    there is deliberately **no free-prose content field** (leak-safety;
    see the module docstring).
    """

    op_type: JudgedOpType
    """Which judged stage produced this verdict."""

    model_id: str
    """Identifier of the model or tier that made the call (e.g.
    ``"hermes3:8b"``, ``"deterministic"``). A label, not content."""

    input_digest: InputDigest
    """Leak-safe fingerprint of the input the op judged."""

    decision: str
    """The verdict label — a short slug whose vocabulary depends on
    ``op_type`` (reconciliation: ``add`` / ``update`` / ``supersede`` /
    ``noop``; curation: ``keep`` / ``discard``; ...). A label, not
    prose."""

    confidence: float = Field(ge=0.0, le=1.0)
    """Producer's confidence in the decision, in ``[0.0, 1.0]``."""

    subject_ref: SubjectRef
    """Leak-safe reference to the memory item the verdict is about — the
    join key to the downstream feedback attribution that labels the
    pair."""
