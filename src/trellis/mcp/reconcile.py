"""Reconcile-on-write — a locally-hosted model's ADD/UPDATE/SUPERSEDE/NOOP
verdict at memory capture.

The **deterministic tier** already lives in :func:`trellis.mcp.server.save_memory`
(exact content-hash short-circuit, then MinHash/LSH near-duplicate detection,
serialized by ``_save_memory_lock``). This module layers the **verdict tier**
on top: when the deterministic tier finds a *near* (not exact) match, a local
model decides ADD / UPDATE / SUPERSEDE / NOOP instead of today's binary
keep/drop. It is the first genuinely model-judged stage of the memory
lifecycle and the first emitter of the ``MEMORY_OP_JUDGED`` training-pair event
(``docs/design/plan-memory-lifecycle.md`` §0.1).

Design invariants (from the binding implementation guide, #263):

* **The model is never in the write path's critical section.** The verdict is
  computed *outside* ``_save_memory_lock``; the caller re-verifies preconditions
  under the lock before committing. This module's :func:`judge_reconcile`
  performs the (slow) model call and must only ever be invoked with the lock
  released.
* **The model is never a hard dependency.** Model unavailable / timeout /
  malformed response → :func:`judge_reconcile` returns a *fallback* outcome
  (ADD, ``fallback=True``); the caller stores the memory as a plain ADD marked
  ``reconciliation="skipped"`` for a later sweep. Capture never loses data
  because a judge was down.
* **SUPERSEDE rides SCD-2 semantics — never a destructive delete.** Documents
  are not SCD-2 rows, so supersede = new doc + Lifecycle stale-marking of the
  old (:func:`mark_document_superseded`), using the real
  :class:`~trellis.schemas.classification.Lifecycle` states.
* **UPDATE is non-destructive in v1** — an 8B model rewriting stored content can
  lose facts, so UPDATE means *annotate* (the new memory is stored and linked to
  the candidate as an addendum), not a destructive rewrite. Destructive merge
  waits for feedback data.
* **Leak-safe emission.** :func:`emit_reconcile_verdict` writes only the typed,
  digest-only :class:`~trellis.schemas.memory_op.MemoryOpJudgedPayload` — never
  raw memory content or model prose.

Feature-flagged **off by default** via ``TRELLIS_ENABLE_RECONCILE_ON_WRITE``;
when off, ``save_memory`` behaves exactly as the deterministic tier does today.
"""

from __future__ import annotations

import asyncio
import json
import os
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from trellis.core.base import TrellisModel
from trellis.core.hashing import content_hash
from trellis.llm.types import Message
from trellis.schemas.classification import Lifecycle
from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.llm.protocol import LLMClient
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all read from the environment; the write path stays flagged
# off unless an operator opts in).
# ---------------------------------------------------------------------------

#: Truthy → the model-judged verdict tier runs. Off by default: capture keeps
#: the deterministic-only behavior every existing deployment sees today.
RECONCILE_FLAG_ENV = "TRELLIS_ENABLE_RECONCILE_ON_WRITE"

#: Optional override for the verdict model identifier used in emitted events
#: when the provider does not report a model back. Defaults to
#: :data:`DEFAULT_RECONCILE_MODEL`.
RECONCILE_MODEL_ENV = "TRELLIS_RECONCILE_MODEL"

#: Optional per-verdict timeout (seconds). An 8B verdict takes seconds; the
#: cap bounds how long capture waits before falling back to a plain ADD.
RECONCILE_TIMEOUT_ENV = "TRELLIS_RECONCILE_TIMEOUT_S"

#: Default verdict model — a small local model over an OpenAI-compatible
#: endpoint (Ollama), per the guide's north-star ladder.
DEFAULT_RECONCILE_MODEL = "hermes3:8b"

#: Default verdict timeout in seconds.
DEFAULT_TIMEOUT_S = 20.0

# ---------------------------------------------------------------------------
# Document metadata markers. The verdict's effect is auditable from the stored
# document alone (and queryable for a later reconciliation sweep) without ever
# storing model prose.
# ---------------------------------------------------------------------------

#: Metadata key naming the reconciliation outcome recorded on a stored doc.
RECONCILIATION_KEY = "reconciliation"

#: Metadata key on an UPDATE addendum pointing at the annotated candidate.
UPDATES_DOC_KEY = "updates_doc_id"

#: Metadata key on a SUPERSEDE successor pointing at the superseded candidate.
SUPERSEDES_DOC_KEY = "supersedes_doc_id"

#: Metadata key carrying the serialized :class:`Lifecycle` on a superseded doc.
LIFECYCLE_KEY = "lifecycle"

#: Marker: model was unavailable / timed out / returned garbage — stored as a
#: plain ADD for a later reconciliation sweep (the offline fallback).
MARKER_SKIPPED = "skipped"

#: Marker: the candidate changed between verdict and commit, so the verdict was
#: downgraded to a plain ADD under the lock (re-verify-under-lock race).
MARKER_STALE = "stale_recheck"


class ReconcileDecision(StrEnum):
    """The four verdicts a reconciliation call can produce.

    The slug vocabulary is fixed by
    :class:`~trellis.schemas.memory_op.MemoryOpJudgedPayload` (``decision``
    for ``op_type=reconciliation``): ``add`` / ``update`` / ``supersede`` /
    ``noop``.
    """

    ADD = "add"
    UPDATE = "update"
    SUPERSEDE = "supersede"
    NOOP = "noop"


class ReconcileCandidate(TrellisModel):
    """A near-duplicate the deterministic tier surfaced for adjudication."""

    doc_id: str
    content: str
    similarity: float


class ReconcileOutcome(TrellisModel):
    """The verdict for one incoming memory against one candidate.

    ``fallback=True`` marks an outcome the model did *not* produce (it was
    unavailable, timed out, or returned malformed JSON). Fallback outcomes are
    always :attr:`ReconcileDecision.ADD` and are *not* emitted as judged events
    — no model judged, so there is no training pair.
    """

    decision: ReconcileDecision
    confidence: float
    model_id: str
    fallback: bool = False
    fallback_reason: str | None = None


# ---------------------------------------------------------------------------
# Configuration readers
# ---------------------------------------------------------------------------


def reconcile_on_write_enabled() -> bool:
    """Return whether the model-judged verdict tier is enabled."""
    return os.environ.get(RECONCILE_FLAG_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def reconcile_timeout_seconds() -> float:
    """Return the per-verdict timeout, defaulting on absent/invalid config."""
    raw = os.environ.get(RECONCILE_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def configured_model_id() -> str:
    """Return the fallback model identifier label for emitted events."""
    return os.environ.get(RECONCILE_MODEL_ENV, "").strip() or DEFAULT_RECONCILE_MODEL


# ---------------------------------------------------------------------------
# Prompt + verdict parsing (strict — the failure mode is a safe fallback ADD)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You reconcile a NEW memory against an EXISTING near-duplicate memory. "
    "Choose exactly one verdict:\n"
    '- "add": the NEW memory is meaningfully different; keep both.\n'
    '- "noop": the NEW memory adds nothing over the EXISTING one; keep only '
    "the existing.\n"
    '- "update": the NEW memory elaborates or annotates the EXISTING one; the '
    "existing memory stays and the new one is attached as an addendum.\n"
    '- "supersede": the NEW memory corrects or replaces the EXISTING one, '
    "which becomes stale.\n"
    "Respond with ONLY a JSON object and no other text: "
    '{"decision": "add|noop|update|supersede", "confidence": 0.0-1.0}.'
)


def build_reconcile_messages(
    new_content: str, candidate: ReconcileCandidate
) -> list[Message]:
    """Build the verdict prompt. Content stays in the prompt, never in events."""
    user = (
        f"NEW memory:\n{new_content}\n\n"
        f"EXISTING memory (near-duplicate, similarity "
        f"{candidate.similarity:.2f}):\n{candidate.content}\n\n"
        "Respond with the verdict JSON."
    )
    return [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=user),
    ]


def parse_verdict(raw: str) -> tuple[ReconcileDecision, float] | None:
    """Parse a strict verdict JSON blob.

    Returns ``(decision, confidence)`` or ``None`` when the response is not
    valid — an unknown decision, a non-numeric confidence, or non-JSON. A
    ``None`` return is the malformed-response signal the caller turns into a
    safe fallback ADD.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Tolerate a fenced code block: drop the fence lines.
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    decision_raw = data.get("decision")
    if not isinstance(decision_raw, str):
        return None
    try:
        decision = ReconcileDecision(decision_raw.strip().lower())
    except ValueError:
        return None

    confidence_raw = data.get("confidence", 0.5)
    if isinstance(confidence_raw, bool):
        # bool is an int subclass; a boolean confidence is malformed.
        return None
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        return None
    # Clamp rather than reject an out-of-range score — the decision is the
    # load-bearing field; the confidence is advisory.
    confidence = max(0.0, min(1.0, confidence))
    return decision, confidence


def _fallback_outcome(model_id: str, reason: str) -> ReconcileOutcome:
    """A model-unavailable outcome: plain ADD, marked for a later sweep."""
    return ReconcileOutcome(
        decision=ReconcileDecision.ADD,
        confidence=0.0,
        model_id=model_id,
        fallback=True,
        fallback_reason=reason,
    )


def judge_reconcile(
    client: LLMClient,
    *,
    new_content: str,
    candidate: ReconcileCandidate,
    timeout: float,
    model_id: str,
) -> ReconcileOutcome:
    """Compute the verdict via a model call — **only ever outside the lock**.

    An 8B verdict takes seconds; running it under ``_save_memory_lock`` would
    serialize every memory save. The caller gathers candidates under the lock,
    releases it, calls this, then re-acquires the lock to commit.

    Never raises for a model problem: timeout, transport error, or malformed
    JSON all resolve to a fallback ADD outcome so capture never fails on the
    judge's availability.
    """
    messages = build_reconcile_messages(new_content, candidate)
    try:
        response = asyncio.run(
            asyncio.wait_for(
                client.generate(messages=messages, temperature=0.0, max_tokens=200),
                timeout=timeout,
            )
        )
    except TimeoutError:
        # asyncio.TimeoutError is an alias of the builtin on 3.11+.
        logger.warning("reconcile_verdict_timeout", timeout=timeout)
        return _fallback_outcome(model_id, "timeout")
    except Exception as exc:
        # Any model / transport failure resolves to a fallback ADD — the
        # judge is never a hard dependency of capture.
        logger.warning("reconcile_verdict_model_error", error=str(exc))
        return _fallback_outcome(model_id, "model_error")

    parsed = parse_verdict(response.content)
    if parsed is None:
        logger.warning("reconcile_verdict_malformed")
        return _fallback_outcome(model_id, "malformed_response")

    decision, confidence = parsed
    resolved_model = response.model or model_id
    logger.debug(
        "reconcile_verdict",
        decision=decision.value,
        confidence=round(confidence, 3),
        candidate_id=candidate.doc_id,
    )
    return ReconcileOutcome(
        decision=decision, confidence=confidence, model_id=resolved_model
    )


# ---------------------------------------------------------------------------
# Verdict effects that touch the document store (run under the caller's lock)
# ---------------------------------------------------------------------------


def mark_document_superseded(
    document_store: DocumentStore, *, old_doc_id: str, new_doc_id: str
) -> bool:
    """SCD-2 stale-mark the superseded doc — never delete it.

    Sets the old document's :class:`Lifecycle` to ``state="superseded"`` with
    ``superseded_by`` pointing at the successor, preserving content and audit.
    Returns ``False`` if the old doc has vanished (the caller downgrades).
    """
    doc = document_store.get(old_doc_id)
    if doc is None:
        return False
    metadata = dict(doc.get("metadata") or {})
    metadata[LIFECYCLE_KEY] = Lifecycle(
        state="superseded", superseded_by=new_doc_id
    ).model_dump(mode="json")
    document_store.put(old_doc_id, doc["content"], metadata=metadata)
    return True


# ---------------------------------------------------------------------------
# Emission — the first MEMORY_OP_JUDGED emitter (digests only, never content)
# ---------------------------------------------------------------------------


def emit_reconcile_verdict(
    event_log: EventLog,
    *,
    outcome: ReconcileOutcome,
    new_content: str,
    candidate: ReconcileCandidate,
    subject_ref_type: str,
    subject_ref_id: str,
) -> None:
    """Emit ``MEMORY_OP_JUDGED`` for one applied model verdict.

    Leak-safe by construction: the payload carries a content *digest* (hash +
    length + candidate ref), the verdict slug, the model id, and a confidence —
    never the memory content or the model's prose. Best-effort: a telemetry
    failure must never roll back a committed verdict.
    """
    payload = MemoryOpJudgedPayload(
        op_type=JudgedOpType.RECONCILIATION,
        model_id=outcome.model_id,
        input_digest=InputDigest(
            hash=content_hash(new_content),
            length=len(new_content),
            source_refs=[candidate.doc_id],
        ),
        decision=outcome.decision.value,
        confidence=outcome.confidence,
        subject_ref=SubjectRef(ref_type=subject_ref_type, ref_id=subject_ref_id),
    )
    try:
        event_log.emit(
            EventType.MEMORY_OP_JUDGED,
            source="save_memory.reconcile",
            entity_id=subject_ref_id,
            entity_type=subject_ref_type,
            payload=payload.model_dump(mode="json"),
        )
    except Exception:
        # GRACEFUL-DEGRADATION: the verdict is already committed to the doc
        # store; a failed training-pair emit must not undo a good write.
        logger.exception("reconcile_verdict_emit_failed", decision=outcome.decision)
