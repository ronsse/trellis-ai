"""Flag-gated reconcile interplay for captured memories (#263 reuse).

Auto-capture writes memories that are near-duplicates of each other and of
hand-written memories; without adjudication, "day-one captures duplicate"
(the #255 guide's ordering rationale for landing after #263). This module
routes each distilled candidate through the **existing** reconcile-on-write
machinery — it imports :mod:`trellis.mcp.reconcile` wholesale and adds no new
verdict logic, so there is no core change and no second reconcile
implementation to drift.

Behaviour, per candidate, only when ``TRELLIS_ENABLE_RECONCILE_ON_WRITE`` is
set (off by default — the same flag #263 gates ``save_memory`` with):

* **Exact duplicate** already stored under this source's prefix, with every
  chunk row it should have → dropped (deterministic NOOP; no model call, no
  event). One whose chunks are not all stored is not a duplicate yet; when it
  is this candidate's own parent row, the candidate keeps the verdict that row
  was stored with and is written again, without a second judge call.
* **Near duplicate** → the local model judges ADD / UPDATE / SUPERSEDE / NOOP
  exactly as ``save_memory`` does; NOOP drops the candidate, SUPERSEDE stamps
  a successor marker, UPDATE stamps an addendum marker. Each non-fallback
  verdict emits the leak-safe ``MEMORY_OP_JUDGED`` event.
* **No near duplicate** → plain ADD.

Fallback (model down) resolves to ADD marked ``skipped`` — reconcile is
fail-open, so a judge outage never loses an already-distilled memory.

**Adjudication decides; it does not write** (#407, #408). :func:`adjudicate`
stamps markers on candidates and emits verdict events; the one store side
effect a SUPERSEDE verdict has — SCD-2 stale-marking the prior doc, never a
delete — is applied by :func:`apply_supersessions` *after* the sweep's write
seam has run. Two things forced that ordering, and neither is a race:

* ``adjudicate`` adds each surviving candidate to its own ``index`` and
  ``existing`` snapshot inside the candidate loop, but nothing is persisted
  until ``capture._write_records`` runs afterwards. So a second candidate of
  one session superseding the first asked ``mark_document_superseded`` for a
  row that did not exist yet — ordinary operation of a multi-candidate
  session, silently discarded, leaving the earlier doc written with no
  lifecycle marker while the later one claimed to have superseded it (#407).
* Even against an already-stored target, stale-marking during adjudication
  set ``superseded_by`` to a successor that had not been written — and under
  ``--dry-run`` never would be, which is how a sweep that promised to write
  nothing mutated the lifecycle state of pre-existing documents (#408).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.document_write import put_document
from trellis.core.hashing import content_hash
from trellis.core.vector_metadata import resolve_vector_store
from trellis.ingest_corpus.models import is_chunk_doc_id
from trellis.ingest_corpus.sync import record_landed
from trellis.mcp.reconcile import (
    MARKER_SKIPPED,
    RECONCILIATION_KEY,
    SUPERSEDES_DOC_KEY,
    UPDATES_DOC_KEY,
    ReconcileCandidate,
    ReconcileDecision,
    ReconcileFallbackReason,
    configured_model_id,
    emit_reconcile_verdict,
    judge_reconcile,
    mark_document_superseded,
    marker_for_fallback,
    reconcile_timeout_seconds,
)
from trellis.schemas.classification import lifecycle_state_of

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from trellis.llm import LLMClient
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.vector import VectorStore
    from trellis.stores.registry import StoreRegistry
    from trellis_workers.session_capture.models import CandidateMemory, CaptureReport

logger = structlog.get_logger(__name__)

_LIST_PAGE_SIZE = 500

#: A MinHash match at/above this Jaccard is effectively identical; the exact
#: content-hash check already covers true identity, so this only guards the
#: (rare) hash-collision-free near-identical case from being called "near".
_NEAR_CEILING = 0.999


def _load_existing(
    registry: StoreRegistry,
    id_prefix: str,
    hidden_doc_ids: AbstractSet[str] = frozenset(),
) -> dict[str, str]:
    """Return ``{doc_id: content}`` for stored captures under *id_prefix*.

    A doc id in *hidden_doc_ids* is left out; see :func:`adjudicate`.
    """
    doc_store = registry.knowledge.document_store
    existing: dict[str, str] = {}
    offset = 0
    while True:
        page = doc_store.list_documents(limit=_LIST_PAGE_SIZE, offset=offset)
        if not page:
            break
        offset += len(page)
        for doc in page:
            doc_id = doc.get("doc_id", "")
            if (
                doc_id.startswith(id_prefix)
                and not is_chunk_doc_id(doc_id)
                and doc_id not in hidden_doc_ids
            ):
                existing[doc_id] = doc.get("content", "")
        if len(page) < _LIST_PAGE_SIZE:
            break
    return existing


def adjudicate(
    registry: StoreRegistry,
    candidates: list[CandidateMemory],
    *,
    client: LLMClient | None,
    id_prefix: str,
    report: CaptureReport,
    dry_run: bool,
    hidden_doc_ids: AbstractSet[str] = frozenset(),
) -> list[CandidateMemory]:
    """Reconcile candidates against stored captures; return the survivors.

    Mutates each surviving candidate's ``reconciliation`` marker (and
    ``updates_doc_id`` / ``supersedes_doc_id`` where applicable) so the writer
    can stamp it into document metadata. Reads the document store; its only
    write is the supersession repair of an exact duplicate, below.

    ``dry_run`` withholds the one remaining side effect — the
    ``MEMORY_OP_JUDGED`` emit. The model is still called and the verdicts are
    still computed, reported and returned: a dry run that stopped adjudicating
    would preview a *different* sweep from the one it is previewing.

    ``hidden_doc_ids`` are documents this sweep wrote itself at an earlier
    checkpoint. A sweep that writes once, at its end, never shows its own
    candidates to a later session's adjudication, and one that checkpoints must
    not start to: the ids are left out of the stored captures compared against
    and out of the exact-duplicate check, so a candidate is reconciled against
    what was stored before the sweep began and nothing else.

    **An exact duplicate counts only once it has landed.** The writer puts a
    document's parent row before its chunks, so a stored parent with this
    content may be one a run died in the middle of. :func:`record_landed` reads
    its chunk rows; when one is missing or different the candidate is not a
    duplicate of anything, skips the exact-duplicate branch, and goes through the
    near-duplicate judge like any other, whose verdicts are unchanged. The writer
    then rewrites the parent and its chunks.

    **An unlanded exact duplicate of this very candidate is not judged again.**
    Its stored parent already records what an earlier judge decided, so the
    candidate takes that verdict, its marker and the ids it names, and goes
    straight to the writer, which completes the chunk rows; the stale-mark is
    then applied by :func:`apply_supersessions` like any other survivor's, but
    only to a target that still reads as current (an archived, superseded or
    deleted one is left alone and counts no failure, as in the repair below).
    Judging it again would be a second verdict on a record that already has one:
    a NOOP would drop it with its chunks still missing, and a run with no judge
    would stamp ``add`` over a stored ``supersede`` and never mark the target.
    It adds nothing to the report's verdict counts, which the sweep that judged
    it already made.

    **An exact duplicate also repairs the supersession it names.** A document
    that claims ``supersedes_doc_id`` was written by an earlier sweep whose
    stale-mark may never have happened (the process died, or the store refused,
    between the write and :func:`apply_supersessions`). That sweep's session is
    left un-watermarked and judged again, its candidate lands here as an exact
    duplicate, and without this the retry would report clean while the target
    stays servable and unmarked. See :func:`_repair_supersession`.
    """
    doc_store = registry.knowledge.document_store
    event_log = registry.operational.event_log
    model_id = configured_model_id(registry)
    timeout = reconcile_timeout_seconds(registry)

    existing = _load_existing(registry, id_prefix, hidden_doc_ids)
    index = MinHashIndex()
    for doc_id, content in existing.items():
        index.add(doc_id, content)

    survivors: list[CandidateMemory] = []
    for candidate in candidates:
        chash = content_hash(candidate.content)
        exact = doc_store.get_by_hash(chash)
        exact_id = str(exact.get("doc_id", "")) if exact is not None else ""
        if (
            exact is not None
            and exact_id.startswith(id_prefix)
            and exact_id not in hidden_doc_ids
        ):
            if record_landed(doc_store, exact_id, candidate.content):
                candidate.reconciliation = ReconcileDecision.NOOP.value
                report.candidates_reconciled_noop += 1
                if not dry_run:
                    _repair_supersession(registry, exact, report)
                continue
            if exact_id == candidate.doc_id:
                # Already in ``existing`` and ``index``, which loaded it.
                _take_stored_verdict(candidate, exact)
                target_id = candidate.supersedes_doc_id
                if target_id:
                    # The stamp rewrites the target's lifecycle record, so it
                    # goes only to one that still reads as current, as in
                    # _repair_supersession. The row keeps its stored claim.
                    target = doc_store.get(target_id)
                    if (
                        target is None
                        or lifecycle_state_of(target.get("metadata")) != "current"
                    ):
                        candidate.supersedes_doc_id = None
                survivors.append(candidate)
                continue

        matches = [
            (mid, sim)
            for mid, sim in index.query(candidate.content)
            if sim < _NEAR_CEILING and mid in existing
        ]
        if matches and client is not None:
            match_id, similarity = matches[0]
            outcome = judge_reconcile(
                client,
                new_content=candidate.content,
                candidate=ReconcileCandidate(
                    doc_id=match_id,
                    content=existing[match_id],
                    similarity=similarity,
                ),
                timeout=timeout,
                model_id=model_id,
            )
            if not outcome.fallback and not dry_run:
                emit_reconcile_verdict(
                    event_log,
                    outcome=outcome,
                    new_content=candidate.content,
                    candidate=ReconcileCandidate(
                        doc_id=match_id,
                        content=existing[match_id],
                        similarity=similarity,
                    ),
                    subject_ref_type="document",
                    subject_ref_id=candidate.doc_id,
                )
            _apply_verdict(
                candidate,
                outcome_decision=outcome.decision,
                is_fallback=outcome.fallback,
                match_id=match_id,
                report=report,
            )
            if candidate.reconciliation == ReconcileDecision.NOOP.value:
                continue
        else:
            candidate.reconciliation = ReconcileDecision.ADD.value

        index.add(candidate.doc_id, candidate.content)
        existing[candidate.doc_id] = candidate.content
        survivors.append(candidate)
    return survivors


def _take_stored_verdict(candidate: CandidateMemory, stored: dict[str, Any]) -> None:
    """Copy the markers of *stored*, this candidate's own parent row, onto it.

    The writer merges stored metadata under the fresh, so a marker left off the
    candidate would overwrite the stored one with a different verdict. A row
    with no marker, written before markers existed, is an ADD.
    """
    metadata = stored.get("metadata") or {}
    marker = metadata.get(RECONCILIATION_KEY)
    candidate.reconciliation = (
        marker if isinstance(marker, str) and marker else ReconcileDecision.ADD.value
    )
    supersedes = metadata.get(SUPERSEDES_DOC_KEY)
    if isinstance(supersedes, str) and supersedes:
        candidate.supersedes_doc_id = supersedes
    updates = metadata.get(UPDATES_DOC_KEY)
    if isinstance(updates, str) and updates:
        candidate.updates_doc_id = updates


def _apply_verdict(
    candidate: CandidateMemory,
    *,
    outcome_decision: ReconcileDecision,
    is_fallback: bool,
    match_id: str,
    report: CaptureReport,
) -> None:
    """Translate a verdict into candidate markers. No store side effects."""
    if is_fallback:
        candidate.reconciliation = MARKER_SKIPPED
        return
    if outcome_decision == ReconcileDecision.NOOP:
        candidate.reconciliation = ReconcileDecision.NOOP.value
        report.candidates_reconciled_noop += 1
        return
    if outcome_decision == ReconcileDecision.SUPERSEDE:
        # The marker only; the SCD-2 stale-mark it implies is applied by
        # apply_supersessions once both documents exist.
        candidate.reconciliation = ReconcileDecision.SUPERSEDE.value
        candidate.supersedes_doc_id = match_id
        report.candidates_reconciled_supersede += 1
        return
    if outcome_decision == ReconcileDecision.UPDATE:
        candidate.reconciliation = ReconcileDecision.UPDATE.value
        candidate.updates_doc_id = match_id
        return
    candidate.reconciliation = ReconcileDecision.ADD.value


def apply_supersessions(
    doc_store: DocumentStore,
    written: list[CandidateMemory],
    report: CaptureReport,
    *,
    vector_store: VectorStore | None,
) -> None:
    """Stale-mark every superseded doc, after the successors are persisted.

    ``vector_store`` is required rather than defaulted to ``None`` for the
    same reason :func:`~trellis.mcp.reconcile.mark_document_superseded` makes
    it required: both writes below stamp a lifecycle marker, and a lifecycle
    stamp the vector row never sees is #337 exactly. A default would let a
    caller forget it and still typecheck.

    Call this only from the live write path, once ``_write_records`` has run.
    A supersession is a claim about two documents, so both have to exist for
    it to be true: the successor is checked here (a candidate the write seam
    dropped supersedes nothing) and the target's existence is
    ``mark_document_superseded``'s return, which is *read*, counted on
    ``CaptureReport.supersessions_failed`` and warned about rather than
    discarded (#407). The warning's ``kind`` follows the reason returned: a
    vanished target is ``supersede_target_missing``, and a promoted
    precedent's row or a redaction tombstone, which the seam refuses to
    stamp, is ``supersede_target_protected`` (#994, #1039). The sweep's own
    candidates come
    from :func:`_load_existing`, which reads only the capture id prefix, so
    the second is not reachable from a sweep today; it is mapped here so a
    wider loader cannot report a protected target as missing.
    """
    for candidate in written:
        match_id = candidate.supersedes_doc_id
        if not match_id:
            continue
        successor = doc_store.get(candidate.doc_id)
        if successor is None:
            # The successor never landed. Stale-marking now would point
            # ``superseded_by`` at nothing — the same defect aimed the other
            # way. There is no stored claim to withdraw either.
            _record_supersede_failure(
                report, "supersede_successor_missing", match_id, candidate.doc_id
            )
            continue
        _stamp_or_withdraw(
            doc_store,
            candidate.doc_id,
            successor,
            match_id,
            report,
            vector_store=vector_store,
        )


def _stamp_or_withdraw(
    doc_store: DocumentStore,
    successor_id: str,
    successor: dict[str, Any],
    match_id: str,
    report: CaptureReport,
    *,
    vector_store: VectorStore | None,
) -> None:
    """Stale-mark *match_id* as superseded by a stored *successor*, or give up.

    A refusal is counted and warned about, and the successor stops claiming a
    supersession that did not happen.
    """
    refusal = mark_document_superseded(
        doc_store,
        old_doc_id=match_id,
        new_doc_id=successor_id,
        vector_store=vector_store,
    )
    if refusal is None:
        return
    # mark_document_superseded logs the refusal; the report is what an
    # operator reading the sweep sees, and the successor must stop
    # claiming a supersession that did not happen.
    kind = (
        "supersede_target_protected"
        if refusal is ReconcileFallbackReason.TARGET_PROTECTED
        else "supersede_target_missing"
    )
    _record_supersede_failure(report, kind, match_id, successor_id)
    _withdraw_supersede_claim(
        doc_store,
        successor_id,
        successor,
        reason=refusal,
        vector_store=vector_store,
    )


def _repair_supersession(
    registry: StoreRegistry, stored: dict[str, Any], report: CaptureReport
) -> None:
    """Apply the supersession a stored capture names but its target lacks.

    ``stored`` is a document an exact-duplicate check just matched. When its
    metadata names ``supersedes_doc_id`` and that target carries no superseded
    marker, the stale-mark is applied now, through the same seam
    :func:`apply_supersessions` uses (so a vanished or protected target is
    counted and the claim withdrawn the same way). Idempotent: a target that
    already declares a successor, this document or another, is left alone, and
    a withdrawn claim leaves no key to act on a second time. Live runs only;
    the caller skips a dry run.

    Acts only on a target that reads as current. One that is archived or
    superseded is left as it is, because the stamp rewrites the lifecycle record
    and would return an archived document to packs. One that is gone is left
    alone too: the supersession was good when it was applied, and a target
    deleted afterwards is neither a failure nor a claim to strip. A target an
    operator restored (``state == "current"``) reads the same as one never
    stamped, so a re-sweep supersedes it again; the store cannot tell the two
    apart and this does not try to.
    """
    metadata = stored.get("metadata") or {}
    match_id = metadata.get(SUPERSEDES_DOC_KEY)
    if not isinstance(match_id, str) or not match_id:
        return
    doc_store = registry.knowledge.document_store
    target = doc_store.get(match_id)
    if target is None or lifecycle_state_of(target.get("metadata")) != "current":
        return
    _stamp_or_withdraw(
        doc_store,
        str(stored.get("doc_id", "")),
        stored,
        match_id,
        report,
        vector_store=resolve_vector_store(registry),
    )


def _record_supersede_failure(
    report: CaptureReport, kind: str, old_doc_id: str, new_doc_id: str
) -> None:
    """Count and describe one supersession that could not be applied."""
    report.supersessions_failed += 1
    report.warnings.append(
        {"kind": kind, "old_doc_id": old_doc_id, "new_doc_id": new_doc_id}
    )


def _withdraw_supersede_claim(
    doc_store: DocumentStore,
    doc_id: str,
    stored: dict[str, Any],
    *,
    reason: ReconcileFallbackReason,
    vector_store: VectorStore | None,
) -> None:
    """Strip an unapplied ``supersedes_doc_id`` off a written successor.

    The marker is rewritten to the one *reason* maps to — ``stale_recheck`` for
    a vanished target, ``target_protected`` for a precedent's row or a
    redaction tombstone — through the
    same :func:`~trellis.mcp.reconcile.marker_for_fallback` ``mcp.server``
    uses for a verdict it declined to apply. ``stale_recheck`` is also the
    queue a later reconciliation sweep reads, which is why a protected target
    is not filed there: re-judging it would be refused again. Metadata-only, so
    ``preserve_updated_at`` (#397): withdrawing a claim is not a modification
    of the memory and must not reset its recency clock.
    """
    metadata = {
        k: v
        for k, v in (stored.get("metadata") or {}).items()
        if k != SUPERSEDES_DOC_KEY
    }
    metadata[RECONCILIATION_KEY] = marker_for_fallback(reason)
    put_document(
        doc_store,
        vector_store,
        doc_id,
        stored["content"],
        metadata,
        preserve_updated_at=True,
    )
