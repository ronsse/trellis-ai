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
  exactly as ``save_memory`` does; NOOP drops the candidate, SUPERSEDE
  stale-marks the prior doc (SCD-2, never a delete) and stamps a successor
  marker, UPDATE stamps an addendum marker. Each non-fallback verdict emits
  the leak-safe ``MEMORY_OP_JUDGED`` event.
* **No near duplicate** → plain ADD.

Fallback (model down) resolves to ADD marked ``skipped`` — reconcile is
fail-open, so a judge outage never loses an already-distilled memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.classify.dedup.minhash import MinHashIndex
from trellis.core.hashing import content_hash
from trellis.ingest_corpus.models import is_chunk_doc_id
from trellis.mcp.reconcile import (
    MARKER_SKIPPED,
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
) -> list[CandidateMemory]:
    """Reconcile candidates against stored captures; return the survivors.

    Mutates each surviving candidate's ``reconciliation`` marker (and
    ``updates_doc_id`` / ``supersedes_doc_id`` where applicable) so the writer
    can stamp it into document metadata.
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
            if not outcome.fallback:
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
                doc_store=doc_store,
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
    doc_store: Any,
    report: CaptureReport,
) -> None:
    """Translate a verdict into candidate markers / store side effects."""
    if is_fallback:
        candidate.reconciliation = MARKER_SKIPPED
        return
    if outcome_decision == ReconcileDecision.NOOP:
        candidate.reconciliation = ReconcileDecision.NOOP.value
        report.candidates_reconciled_noop += 1
        return
    if outcome_decision == ReconcileDecision.SUPERSEDE:
        # Successor doc_id is content-derived and known before the write.
        mark_document_superseded(
            doc_store, old_doc_id=match_id, new_doc_id=candidate.doc_id
        )
        candidate.reconciliation = ReconcileDecision.SUPERSEDE.value
        candidate.supersedes_doc_id = match_id
        return
    if outcome_decision == ReconcileDecision.UPDATE:
        candidate.reconciliation = ReconcileDecision.UPDATE.value
        candidate.updates_doc_id = match_id
        return
    candidate.reconciliation = ReconcileDecision.ADD.value
