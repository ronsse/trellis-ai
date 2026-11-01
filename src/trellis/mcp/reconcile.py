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
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import model_validator

from trellis.core import write_config
from trellis.core.base import TrellisModel
from trellis.core.document_write import is_redaction_tombstone, put_document
from trellis.core.hashing import content_hash
from trellis.core.memory_op_judged import emit_memory_op_judged
from trellis.core.precedent_document import is_promoted_precedent_document
from trellis.core.write_config import resolve_write_behaviour
from trellis.llm.json_response import (
    JSONParseOutcome,
    coerce_finite_float,
    parse_json_response,
)
from trellis.llm.types import Message
from trellis.schemas.classification import Lifecycle
from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    SubjectRef,
)

if TYPE_CHECKING:
    from trellis.llm.protocol import LLMClient
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import EventLog
    from trellis.stores.base.vector import VectorStore
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

# Back-compat re-exports. :mod:`trellis.core.write_config` owns every
# write-behaviour name, default, and parsing rule — including the docstrings
# these used to carry. They keep this module's historical spellings so
# existing importers do not break. The write path still stays flagged off
# unless an operator opts in.
RECONCILE_FLAG_ENV = write_config.RECONCILE_FLAG_ENV
RECONCILE_MODEL_ENV = write_config.RECONCILE_MODEL_ENV
RECONCILE_TIMEOUT_ENV = write_config.RECONCILE_TIMEOUT_ENV
DEFAULT_RECONCILE_MODEL = write_config.DEFAULT_RECONCILE_MODEL
DEFAULT_TIMEOUT_S = write_config.DEFAULT_RECONCILE_TIMEOUT_S

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

#: Marker: the model's SUPERSEDE verdict named a promoted precedent's row, which
#: reconcile never stamps (#957), so the successor was kept as a plain ADD. Not
#: :data:`MARKER_STALE`: that marker queues the memory for a later re-judge, and
#: re-judging cannot change the answer, because the refusal is of the target and
#: not of the verdict.
MARKER_TARGET_PROTECTED = "target_protected"


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


class ReconcileFallbackReason(StrEnum):
    """Why a reconciliation produced a fallback ADD instead of a verdict.

    A closed vocabulary rather than a free ``str``, because the only thing
    recording it buys is a group-by: a reason minted at one construction site
    and spelled differently at another splits the count that answers *how
    often does the judge fail, and how*. ``mypy`` is the roster guard here —
    #443 declared three control keys against six live sites, and a hand-kept
    list of slugs rots in exactly that direction.
    """

    #: The model call exceeded ``reconcile_timeout_s``.
    TIMEOUT = "timeout"
    #: The model call raised — transport, auth, or provider error.
    MODEL_ERROR = "model_error"
    #: The model answered, and the answer was not a usable verdict. This is
    #: the reason #514 is about: the JSON seam parsed, and what it parsed was
    #: not a verdict this code can act on.
    MALFORMED_RESPONSE = "malformed_response"
    #: No client could be built — the tier is off, or no provider is wired.
    MODEL_UNAVAILABLE = "model_unavailable"
    #: Phase B escaped its own exhaustive handling and hit the
    #: belt-and-suspenders guard in ``server._save_memory_reconciled``.
    JUDGE_ERROR = "judge_error"
    #: **Not a judge failure.** The candidate changed between verdict and
    #: commit, so a real verdict was deliberately downgraded under the lock.
    #: It shares this vocabulary because it reaches the same fallback ADD, and
    #: :data:`JUDGE_FAILURE_REASONS` is what keeps a health count from
    #: reporting a correctly-handled race as a degradation — the
    #: ``sessions_skipped_empty`` split (#332), one layer over.
    STALE_RECHECK = "stale_recheck"
    #: **Not a judge failure either, and not a race.** The judge answered
    #: SUPERSEDE and the answer was usable; the target is a promoted
    #: precedent's row, which this seam refuses to stamp
    #: (:func:`mark_document_superseded`). Kept apart from
    #: :attr:`STALE_RECHECK` because folding them made a refused verdict read
    #: as "a correctly handled race" (#994), and the two want different
    #: follow-ups: a race is re-judged by a later sweep, a protected target is
    #: not. The reason implies the verdict (``supersede``); the event's
    #: ``decision`` is still the fallback ADD.
    TARGET_PROTECTED = "target_protected"
    #: **Not a judge failure: a policy.** The incoming memory or the stored
    #: candidate is labelled above ``internal`` and the reconcile route is not
    #: declared local (:mod:`trellis.llm.egress`), so neither text was sent to
    #: the model. The memory is stored as a plain ADD; nothing is lost and
    #: nothing degraded.
    EGRESS_WITHHELD = "egress_withheld"


#: The reasons that mean *the judge did not answer usably*. The complement is
#: deliberate and is the whole reason this set is written down rather than
#: derived as "every reason": see :attr:`ReconcileFallbackReason.STALE_RECHECK`
#: and :attr:`ReconcileFallbackReason.TARGET_PROTECTED`, both of which are a
#: usable verdict that was not applied, and
#: :attr:`ReconcileFallbackReason.EGRESS_WITHHELD`, where the judge was never
#: asked.
JUDGE_FAILURE_REASONS: frozenset[ReconcileFallbackReason] = frozenset(
    ReconcileFallbackReason
) - {
    ReconcileFallbackReason.STALE_RECHECK,
    ReconcileFallbackReason.TARGET_PROTECTED,
    ReconcileFallbackReason.EGRESS_WITHHELD,
}


def marker_for_fallback(reason: ReconcileFallbackReason | None) -> str:
    """The ``reconciliation`` metadata marker a fallback reason is stored under.

    One mapping for every writer of the marker, so the stored document and the
    ``RECONCILE_DEGRADED`` event cannot name different things: the MCP commit,
    and the session-capture sweep's withdrawal of an unapplied supersession.
    Every reason without a marker of its own is :data:`MARKER_SKIPPED` — the
    judge did not produce a verdict, so the memory waits for a later sweep.
    """
    if reason is ReconcileFallbackReason.STALE_RECHECK:
        return MARKER_STALE
    if reason is ReconcileFallbackReason.TARGET_PROTECTED:
        return MARKER_TARGET_PROTECTED
    return MARKER_SKIPPED


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
    — no model judged, so there is no training pair. They emit
    :attr:`~trellis.stores.base.event_log.EventType.RECONCILE_DEGRADED`
    instead, so that "the judge was never consulted" and "the judge failed
    every time" stop being the same silence (#514).
    """

    decision: ReconcileDecision
    confidence: float
    model_id: str
    fallback: bool = False
    fallback_reason: ReconcileFallbackReason | None = None

    @model_validator(mode="after")
    def _fallback_carries_a_reason(self) -> ReconcileOutcome:
        """A fallback with no reason is a degradation nothing can attribute.

        Enforced on the model rather than checked at the emit seam, because
        the seam can only report what the construction sites handed it — the
        #447 shape, where a gate's *filtering* half was covered and its
        *emission* half was not. An outcome that cannot say why it degraded
        should not be constructible.
        """
        if self.fallback and self.fallback_reason is None:
            msg = "a fallback ReconcileOutcome must carry a fallback_reason"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Configuration readers
# ---------------------------------------------------------------------------


def reconcile_on_write_enabled(registry: StoreRegistry | None = None) -> bool:
    """Return whether the model-judged verdict tier is enabled.

    Env > settings > default; see
    :func:`trellis.core.write_config.resolve_write_behaviour`.
    """
    return resolve_write_behaviour(registry).reconcile_on_write


def reconcile_timeout_seconds(registry: StoreRegistry | None = None) -> float:
    """Return the per-verdict timeout, defaulting on absent/invalid config."""
    return resolve_write_behaviour(registry).reconcile_timeout_s


def configured_model_id(registry: StoreRegistry | None = None) -> str:
    """Return the fallback model identifier label for emitted events."""
    return resolve_write_behaviour(registry).reconcile_model


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
    valid — an unknown decision, a non-numeric or non-finite (NaN, ±Infinity,
    too large for a float) confidence, or non-JSON. A ``None`` return is the
    malformed-response signal the caller turns into a safe fallback ADD.
    """
    parsed = parse_json_response(raw)
    if parsed.outcome is JSONParseOutcome.MALFORMED:
        return None
    data = parsed.value
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
    confidence = coerce_finite_float(confidence_raw)
    if confidence is None:
        return None
    # Clamp rather than reject a finite out-of-range score — the decision is the
    # load-bearing field; the confidence is advisory.
    confidence = max(0.0, min(1.0, confidence))
    return decision, confidence


def _fallback_outcome(
    model_id: str, reason: ReconcileFallbackReason
) -> ReconcileOutcome:
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
        return _fallback_outcome(model_id, ReconcileFallbackReason.TIMEOUT)
    except Exception as exc:
        # Any model / transport failure resolves to a fallback ADD — the
        # judge is never a hard dependency of capture.
        logger.warning("reconcile_verdict_model_error", error=str(exc))
        return _fallback_outcome(model_id, ReconcileFallbackReason.MODEL_ERROR)

    parsed = parse_verdict(response.content)
    if parsed is None:
        logger.warning("reconcile_verdict_malformed")
        return _fallback_outcome(model_id, ReconcileFallbackReason.MALFORMED_RESPONSE)

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
    document_store: DocumentStore,
    *,
    old_doc_id: str,
    new_doc_id: str,
    vector_store: VectorStore | None,
) -> ReconcileFallbackReason | None:
    """SCD-2 stale-mark the superseded doc — never delete it.

    Sets the old document's :class:`Lifecycle` to ``state="superseded"`` with
    ``superseded_by`` pointing at the successor, preserving content and audit.

    Returns ``None`` when the stale-mark was applied, and otherwise the
    :class:`ReconcileFallbackReason` it was not: ``STALE_RECHECK`` when the old
    doc has vanished, ``TARGET_PROTECTED`` when it is a promoted precedent's
    row or a redaction tombstone (stamping that would unmark the redaction).
    **Test the return with** ``is None`` **and never for truthiness**: a
    reason is a non-empty string, so ``if mark_document_superseded(...)`` would
    read a refusal as success. It used to be a ``bool``, which could not say
    which of the two it was, and the refused-precedent case was recorded as a
    lock race (#994). **Every caller reads it, and the not-applied path is
    logged here** so no caller can drop it silently (#407):

    * ``mcp.server._commit_reconcile_verdict`` downgrades the just-stored
      memory to a plain ADD carrying the returned reason, so neither the tool
      result nor the ``MEMORY_OP_JUDGED`` event asserts a supersession that
      did not happen, and the ``RECONCILE_DEGRADED`` event and the stored
      marker both name why. It is additionally preceded, inside the same
      ``_save_memory_lock`` hold, by ``_reverify_candidate`` →
      ``_candidate_is_stale``, which treats a missing row as stale; that guard
      is process-local, so a cross-process delete is precisely what reaches
      the ``STALE_RECHECK`` here.
    * ``session_capture.capture.run_capture`` calls this **after**
      ``_write_records``, never during adjudication, and counts a refusal on
      ``CaptureReport.supersessions_failed``, naming the kind of each. The
      ordering is the fix and the counter is only the residue: a sweep's own
      candidates reach the store when the write seam runs, so stale-marking
      from inside ``adjudicate``
      asked this function for a row that structurally could not exist yet
      whenever one candidate of a session superseded an earlier one — and
      pointed ``superseded_by`` at a successor that had not been written
      either.

    **A promoted precedent's row is never stamped** (#957). Its lifecycle belongs
    to the precedent handlers alone: the noise pass, retention and the classify
    refresh all skip it for that reason, and a ``superseded`` stamp written here
    would be a fourth writer none of them know about. A precedent is retired by
    ``precedent.demote``, which needs evidence; a judge's verdict about two
    pieces of text is not that. The refusal reads the id and nothing else
    (:func:`~trellis.core.precedent_document.is_promoted_precedent_document`)
    and is made here, at the one seam every supersession goes through, so a
    caller added later cannot reopen it. It answers ``TARGET_PROTECTED``, so
    both callers take their not-applied path: the successor is kept, without
    the claim, as a plain ADD, and no ``MEMORY_OP_JUDGED`` is emitted for it,
    for the reason a vanished target emits none: that stream is the
    verdict-to-outcome training pair, and a verdict whose effect never
    happened has no outcome to pair with. The ``RECONCILE_DEGRADED`` event,
    with ``judge_failure=false``, is where the refused verdict is counted.

    ``preserve_updated_at`` is load-bearing, not hygiene (#397). A supersession
    stamp is a metadata-only write by definition — the content passed back in is
    the row's own — and ``updated_at`` is what
    :class:`~trellis.retrieve.strategies.KeywordSearch` feeds to its recency
    decay. Without the flag, *declaring a document stale resets its recency
    clock to zero*: a year-old note becomes the freshest thing in the corpus at
    the instant it is superseded, ranked above the successor that replaced it.
    The failure is perfectly inverted — the operation exists to demote the old
    document and its side effect promotes it.

    That matters here and not merely in principle, because no *per-item* gate
    filters this state out. ``retrieve.lifecycle.is_archived`` tests
    ``state == "archived"`` and ``"superseded"`` is a different state, so a
    superseded document stays servable — deliberately.
    ``docs/design/plan-memory-lifecycle.md`` §4 pins the design as "SCD-2
    supersede + **recency-wins-at-retrieval**, with the losing version
    retrievable on demand", which makes the loser's recency rank the whole
    mechanism rather than an incidental score. The one exclusion is §4's
    *pairwise* half (#613): when the successor this stamp names is in the same
    candidate pool, ``PackBuilder`` withholds the loser (reason
    ``superseded``, via ``retrieve.lifecycle.partition_superseded``) so a pack
    never serves both sides. With the successor absent, this rank still
    decides.

    **The `preserve_updated_at` flag is scoped to the keyword axis, and only
    that — but not for the reason #411 gave.** That docstring said no writer
    produces an ``updated_at`` *metadata* key, so the semantic axis could never
    see one. **The universal negative was false**:
    :mod:`trellis.ingest_corpus.conversations` writes both ``created_at`` and
    ``updated_at`` into a conversation document's metadata,
    ``build_vector_row`` splats document metadata into the vector row, and 148
    live production rows carry the key (#417). What actually scopes the flag to
    the keyword axis is narrower and holds regardless: it protects a store
    **column**, and no writer copies that column into a metadata bag.

    **The supersession stamp itself now does reach the vector row**, and that
    is new. This function wrote the document alone until 2026-09-12, on the
    reasoning that ``lifecycle`` was outside the mirrored key set — "the #337
    shape, inert only for as long as the state goes unfiltered. Add such a
    filter and the mirror becomes required." Rather than wait for that filter,
    the write goes through
    :func:`~trellis.core.document_write.put_document`, which mirrors whatever
    the bag carries. ``vector_store`` is a required argument for the same
    reason: a default would restore the convention the seam exists to replace.

    One consequence of #417 worth knowing before reasoning about ranking here:
    :func:`~trellis.retrieve.strategies.resolve_recency_stamp` now makes *both*
    document-backed axes prefer a source-supplied stamp in the metadata bag
    over the row's column. So for a document whose source carried its own
    clock, this flag no longer affects recency at all — the column it protects
    is not the stamp being read. It stays load-bearing for every document
    without one, which is the overwhelming majority, and for the other
    column readers named on ``DocumentStore.put``.
    """
    if is_promoted_precedent_document(old_doc_id):
        logger.warning(
            "supersede_target_is_promoted_precedent",
            old_doc_id=old_doc_id,
            new_doc_id=new_doc_id,
        )
        return ReconcileFallbackReason.TARGET_PROTECTED
    doc = document_store.get(old_doc_id)
    if doc is None:
        logger.warning(
            "supersede_target_missing",
            old_doc_id=old_doc_id,
            new_doc_id=new_doc_id,
        )
        return ReconcileFallbackReason.STALE_RECHECK
    if is_redaction_tombstone(doc):
        # A redacted row is blank and archived already; a ``superseded`` stamp
        # would replace the archived one and unmark the tombstone. The write
        # seam refuses that, and refusing here keeps it a degrade, not a raise.
        logger.warning(
            "supersede_target_is_redaction_tombstone",
            old_doc_id=old_doc_id,
            new_doc_id=new_doc_id,
        )
        return ReconcileFallbackReason.TARGET_PROTECTED
    metadata = dict(doc.get("metadata") or {})
    metadata[LIFECYCLE_KEY] = Lifecycle(
        state="superseded", superseded_by=new_doc_id
    ).model_dump(mode="json")
    put_document(
        document_store,
        vector_store,
        old_doc_id,
        doc["content"],
        metadata,
        preserve_updated_at=True,
    )
    return None


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
    emit_memory_op_judged(
        event_log,
        op_type=JudgedOpType.RECONCILIATION,
        source="save_memory.reconcile",
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


def emit_reconcile_degraded(
    event_log: EventLog,
    *,
    outcome: ReconcileOutcome,
    subject_ref_type: str,
    subject_ref_id: str,
) -> None:
    """Emit ``RECONCILE_DEGRADED`` for one fail-closed reconciliation.

    The counterpart of :func:`emit_reconcile_verdict`, and the two are
    mutually exclusive by construction: a fallback ADD judged nothing, so it
    is not a training pair and must not enter the ``MEMORY_OP_JUDGED`` stream
    that #582's exporter reads. Suppressing it there was right; suppressing it
    *everywhere* is what made a judge that fails every time look exactly like
    a judge nobody consulted (#514).

    Best-effort — a broken event log must not turn a degraded reconciliation
    into a failed write, which would invert the whole point of failing closed
    to an ADD.

    Emits nothing for a genuine verdict. That is a caller error rather than a
    reachable state, but the failure mode is a *phantom degradation* in a
    count whose whole job is to be trusted, so it is checked here and not
    left to the one call site.
    """
    reason = outcome.fallback_reason
    if not outcome.fallback or reason is None:
        logger.warning(
            "reconcile_degraded_emit_skipped_not_a_fallback",
            decision=outcome.decision.value,
            subject_ref_id=subject_ref_id,
        )
        return

    from trellis.stores.base.event_log import EventType  # noqa: PLC0415

    payload: dict[str, Any] = {
        "reason": reason.value,
        "judge_failure": reason in JUDGE_FAILURE_REASONS,
        "model_id": outcome.model_id,
        "decision": outcome.decision.value,
        "subject_ref_type": subject_ref_type,
        "subject_ref_id": subject_ref_id,
    }
    try:
        event_log.emit(
            EventType.RECONCILE_DEGRADED,
            source="save_memory.reconcile",
            entity_id=subject_ref_id,
            payload=payload,
        )
    # GRACEFUL-DEGRADATION: the write already succeeded under the lock. An
    # event-log outage must cost the measurement, never the memory.
    except Exception:
        logger.exception(
            "reconcile_degraded_emit_failed",
            reason=reason.value,
            subject_ref_id=subject_ref_id,
        )
