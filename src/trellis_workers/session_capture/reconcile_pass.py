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

* **Exact duplicate** already stored under this source's prefix → dropped
  (deterministic NOOP; no model call, no event).
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
from trellis.core.hashing import content_hash
from trellis.ingest_corpus.models import is_chunk_doc_id
from trellis.mcp.reconcile import (
    MARKER_SKIPPED,
    MARKER_STALE,
    RECONCILIATION_KEY,
    SUPERSEDES_DOC_KEY,
    ReconcileCandidate,
    ReconcileDecision,
    configured_model_id,
    emit_reconcile_verdict,
    judge_reconcile,
    mark_document_superseded,
    reconcile_timeout_seconds,
)

if TYPE_CHECKING:
    from trellis.llm import LLMClient
    from trellis.stores.registry import StoreRegistry
    from trellis_workers.session_capture.models import CandidateMemory, CaptureReport

logger = structlog.get_logger(__name__)

_LIST_PAGE_SIZE = 500

#: A MinHash match at/above this Jaccard is effectively identical; the exact
#: content-hash check already covers true identity, so this only guards the
#: (rare) hash-collision-free near-identical case from being called "near".
_NEAR_CEILING = 0.999


def _load_existing(registry: StoreRegistry, id_prefix: str) -> dict[str, str]:
    """Return ``{doc_id: content}`` for stored captures under *id_prefix*."""
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
            if doc_id.startswith(id_prefix) and not is_chunk_doc_id(doc_id):
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
) -> list[CandidateMemory]:
    """Reconcile candidates against stored captures; return the survivors.

    Mutates each surviving candidate's ``reconciliation`` marker (and
    ``updates_doc_id`` / ``supersedes_doc_id`` where applicable) so the writer
    can stamp it into document metadata. Reads the document store; never
    writes to it.

    ``dry_run`` withholds the one remaining side effect — the
    ``MEMORY_OP_JUDGED`` emit. The model is still called and the verdicts are
    still computed, reported and returned: a dry run that stopped adjudicating
    would preview a *different* sweep from the one it is previewing.
    """
    doc_store = registry.knowledge.document_store
    event_log = registry.operational.event_log
    model_id = configured_model_id()
    timeout = reconcile_timeout_seconds()

    existing = _load_existing(registry, id_prefix)
    index = MinHashIndex()
    for doc_id, content in existing.items():
        index.add(doc_id, content)

    survivors: list[CandidateMemory] = []
    for candidate in candidates:
        chash = content_hash(candidate.content)
        exact = doc_store.get_by_hash(chash)
        if exact is not None and str(exact.get("doc_id", "")).startswith(id_prefix):
            candidate.reconciliation = ReconcileDecision.NOOP.value
            report.candidates_reconciled_noop += 1
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
    doc_store: Any,
    written: list[CandidateMemory],
    report: CaptureReport,
) -> None:
    """Stale-mark every superseded doc, after the successors are persisted.

    Call this only from the live write path, once ``_write_records`` has run.
    A supersession is a claim about two documents, so both have to exist for
    it to be true: the successor is checked here (a candidate the write seam
    dropped supersedes nothing) and the target's existence is
    ``mark_document_superseded``'s return, which is *read*, counted on
    ``CaptureReport.supersessions_failed`` and warned about rather than
    discarded (#407).
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
        if mark_document_superseded(
            doc_store, old_doc_id=match_id, new_doc_id=candidate.doc_id
        ):
            continue
        # mark_document_superseded logs the miss; the report is what an
        # operator reading the sweep sees, and the successor must stop
        # claiming a supersession that did not happen.
        _record_supersede_failure(
            report, "supersede_target_missing", match_id, candidate.doc_id
        )
        _withdraw_supersede_claim(doc_store, candidate.doc_id, successor)


def _record_supersede_failure(
    report: CaptureReport, kind: str, old_doc_id: str, new_doc_id: str
) -> None:
    """Count and describe one supersession that could not be applied."""
    report.supersessions_failed += 1
    report.warnings.append(
        {"kind": kind, "old_doc_id": old_doc_id, "new_doc_id": new_doc_id}
    )


def _withdraw_supersede_claim(
    doc_store: Any, doc_id: str, stored: dict[str, Any]
) -> None:
    """Strip an unapplied ``supersedes_doc_id`` off a written successor.

    The marker is rewritten to ``stale_recheck`` — the same marker
    ``mcp.server`` uses for a verdict it declined to apply, and the same
    queue a later reconciliation sweep reads. Metadata-only, so
    ``preserve_updated_at`` (#397): withdrawing a claim is not a modification
    of the memory and must not reset its recency clock.
    """
    metadata = {
        k: v
        for k, v in (stored.get("metadata") or {}).items()
        if k != SUPERSEDES_DOC_KEY
    }
    metadata[RECONCILIATION_KEY] = MARKER_STALE
    doc_store.put(
        doc_id, stored["content"], metadata=metadata, preserve_updated_at=True
    )
