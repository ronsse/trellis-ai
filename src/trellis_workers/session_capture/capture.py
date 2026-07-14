"""The capture sweep — orchestrates reader → distiller → gates → writer.

One nightly pass over the Claude Code transcript directory:

#. **Discover** transcript files; **watermark**-skip unchanged ones.
#. **Parse** each new/changed file into a secret-free digest (F8-safe).
#. **Trigger** deterministically — error/correction sessions are mandatory,
   clean ones sampled.
#. **Distil** triggered sessions with the local model (fail-closed).
#. **Gate** each candidate: secret-scan (hard drop) then worthiness.
#. **Reconcile** survivors against stored captures (flag-gated, #263 reuse).
#. **Write** through :func:`~trellis.ingest_corpus.sync.sync_records` — the
   sanctioned reader→core seam ``ingest conversations`` already uses. No
   direct store writes; content-hash idempotency; per-source id-prefix
   scoping; ``MEMORY_STORED`` events; embed-on-ingest.
#. Emit a leak-safe ``MEMORY_OP_JUDGED`` distillation training pair per
   written memory, then advance the watermark for judged sessions only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from trellis.core.hashing import content_hash
from trellis.ingest_corpus.models import SyncRecord
from trellis.ingest_corpus.sync import sync_records
from trellis.mcp.reconcile import (
    RECONCILIATION_KEY,
    SUPERSEDES_DOC_KEY,
    UPDATES_DOC_KEY,
    reconcile_on_write_enabled,
)
from trellis_workers.session_capture import distill, gating, reconcile_pass, secret_scan
from trellis_workers.session_capture.models import (
    CandidateMemory,
    CaptureReport,
    SessionDigest,
)
from trellis_workers.session_capture.transcripts import (
    discover_sessions,
    parse_session,
)
from trellis_workers.session_capture.watermark import WatermarkStore

if TYPE_CHECKING:
    from pathlib import Path

    from trellis.llm import LLMClient
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Corpus namespace for captured Claude Code memories. Stored as
#: ``metadata.source_system`` and used as the doc-id prefix so a capture run
#: can never touch another source's documents.
DEFAULT_SOURCE_SYSTEM = "claude-code"

#: Default 1-in-N sampling for clean (non-mandatory) sessions.
DEFAULT_SAMPLE_DENOMINATOR = 5

#: Marker for a captured memory not yet adjudicated (reconcile flag off) — a
#: later reconcile sweep can find these by this metadata value.
MARKER_PENDING = "pending"

_REQUESTED_BY = "worker:session-capture"


def capture_id_prefix(source_system: str) -> str:
    """Doc-id prefix owned by captured memories of one source."""
    return f"capture:{source_system}:"


def capture_doc_id(source_system: str, content: str) -> str:
    """Content-derived doc id — identical memories collapse to one row."""
    return f"capture:{source_system}:{content_hash(content)}"


def render_memory(candidate: CandidateMemory) -> str:
    """Render a candidate into the stored markdown memory document."""
    return (
        f"# {candidate.title}\n\n"
        f"{candidate.memory}\n\n"
        f"**Signal:** {candidate.signal}\n"
        f"**Evidence:** {candidate.evidence}\n"
        f"**Source:** Claude Code session `{candidate.session_id}`\n"
    )


def _candidate_metadata(candidate: CandidateMemory) -> dict[str, object]:
    """Per-document metadata, including the #263 reconciliation markers."""
    metadata: dict[str, object] = {
        "session_id": candidate.session_id,
        "signal": candidate.signal,
        "memory_type": candidate.memory_type,
        "capture_title": candidate.title,
        "distilled": True,
        RECONCILIATION_KEY: candidate.reconciliation or MARKER_PENDING,
    }
    if candidate.updates_doc_id:
        metadata[UPDATES_DOC_KEY] = candidate.updates_doc_id
    if candidate.supersedes_doc_id:
        metadata[SUPERSEDES_DOC_KEY] = candidate.supersedes_doc_id
    return metadata


def _gate_candidates(
    candidates: list[CandidateMemory],
    report: CaptureReport,
) -> list[CandidateMemory]:
    """Apply the secret-scan then worthiness gates; return survivors."""
    survivors: list[CandidateMemory] = []
    for candidate in candidates:
        report.candidates_distilled += 1
        rendered = render_memory(candidate)
        hits = secret_scan.scan(rendered)
        if hits:
            for cls in hits:
                report.secret_hits_by_class[cls] = (
                    report.secret_hits_by_class.get(cls, 0) + 1
                )
            report.candidates_blocked_secret += 1
            logger.warning(
                "capture_secret_blocked",
                session_id=candidate.session_id,
                classes=hits,
            )
            continue
        if not gating.passes_worthiness(candidate):
            report.candidates_rejected_worthiness += 1
            continue
        candidate.content = rendered
        survivors.append(candidate)
    return survivors


def run_capture(
    registry: StoreRegistry,
    *,
    transcripts_root: Path,
    watermark_path: Path,
    llm_client: LLMClient | None,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    sample_denominator: int = DEFAULT_SAMPLE_DENOMINATOR,
    distill_model_id: str = distill.DEFAULT_DISTILL_MODEL,
    dry_run: bool = False,
) -> CaptureReport:
    """Run one capture sweep and return its :class:`CaptureReport`."""
    reconcile_enabled = reconcile_on_write_enabled()
    report = CaptureReport(
        transcripts_root=str(transcripts_root),
        dry_run=dry_run,
        reconcile_enabled=reconcile_enabled,
    )
    watermark = WatermarkStore(watermark_path)
    id_prefix = capture_id_prefix(source_system)

    written: list[CandidateMemory] = []
    records: list[SyncRecord] = []
    for path in discover_sessions(transcripts_root):
        report.sessions_seen += 1
        if watermark.is_unchanged(path):
            report.sessions_skipped_watermark += 1
            continue

        digest = parse_session(path)
        report.sessions_parsed += 1
        report.malformed_lines += digest.malformed_lines

        if not gating.should_distill(digest, sample_denominator):
            report.sessions_sampled_out += 1
            if not dry_run:
                watermark.record(path)
            continue

        survivors = _capture_session(
            registry,
            digest,
            report=report,
            llm_client=llm_client,
            source_system=source_system,
            id_prefix=id_prefix,
            reconcile_enabled=reconcile_enabled,
        )
        if survivors is None:
            # Judge unavailable — leave un-watermarked so a later run retries.
            continue

        for i, candidate in enumerate(survivors):
            records.append(
                SyncRecord(
                    doc_id=candidate.doc_id,
                    source_key=f"session/{candidate.session_id}#{i}",
                    content=candidate.content,
                    handler_metadata=_candidate_metadata(candidate),
                )
            )
            written.append(candidate)
        if not dry_run:
            watermark.record(path)

    _write_records(registry, records, report, source_system, id_prefix, dry_run)
    if not dry_run:
        _emit_training_pairs(registry, written, distill_model_id)
        watermark.save()

    logger.info("capture_sweep_complete", **report.to_payload())
    return report


def _capture_session(
    registry: StoreRegistry,
    digest: SessionDigest,
    *,
    report: CaptureReport,
    llm_client: LLMClient | None,
    source_system: str,
    id_prefix: str,
    reconcile_enabled: bool,
) -> list[CandidateMemory] | None:
    """Distil, gate, and reconcile one session; ``None`` if the judge is down."""
    candidates = distill.distill_session(llm_client, digest)
    if candidates is None:
        report.warnings.append(
            {"kind": "distill_unavailable", "session_id": digest.session_id}
        )
        return None

    report.sessions_triggered += 1
    input_hash = content_hash(digest.salient_text)
    input_length = len(digest.salient_text)
    for candidate in candidates:
        candidate.input_hash = input_hash
        candidate.input_length = input_length

    survivors = _gate_candidates(candidates, report)
    for candidate in survivors:
        candidate.doc_id = capture_doc_id(source_system, candidate.content)

    if reconcile_enabled:
        return reconcile_pass.adjudicate(
            registry,
            survivors,
            client=llm_client,
            id_prefix=id_prefix,
            report=report,
        )
    for candidate in survivors:
        candidate.reconciliation = MARKER_PENDING
    return survivors


def _write_records(
    registry: StoreRegistry,
    records: list[SyncRecord],
    report: CaptureReport,
    source_system: str,
    id_prefix: str,
    dry_run: bool,
) -> None:
    """Write survivors through the sanctioned sync_records seam."""
    if not records:
        return
    sync_report = sync_records(
        registry,
        records,
        source_system=source_system,
        id_prefix=id_prefix,
        root_label=report.transcripts_root,
        requested_by=_REQUESTED_BY,
        dry_run=dry_run,
        detect_moves=False,
    )
    counts = sync_report.counts()
    report.memories_written += counts["ingested"] + counts["updated"]
    report.memories_skipped_unchanged += counts["skipped_unchanged"]
    report.warnings.extend(sync_report.warnings)


def _emit_training_pairs(
    registry: StoreRegistry,
    written: list[CandidateMemory],
    model_id: str,
) -> None:
    """Emit one distillation training pair per newly written memory."""
    doc_store = registry.knowledge.document_store
    for candidate in written:
        # Only emit for a memory that actually landed in the store.
        if doc_store.get(candidate.doc_id) is None:
            continue
        distill.emit_distillation_judged(
            registry.operational.event_log,
            candidate=candidate,
            decision="keep",
            model_id=model_id,
        )
