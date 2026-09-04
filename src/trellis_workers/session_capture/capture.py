"""The capture sweep — orchestrates reader → distiller → gates → writer.

One nightly pass over the Claude Code transcript directory:

#. **Discover** transcript files; **watermark**-skip unchanged ones.
#. **Parse** each new/changed file into a secret-free digest (F8-safe).
#. **Trigger** deterministically — error/correction sessions are mandatory,
   clean ones sampled.
#. **Distil** triggered sessions with the local model (fail-closed).
#. **Gate** each candidate: secret-scan (hard drop), then the deterministic
   capture-instruction injection guard, then worthiness.
#. **Reconcile** survivors against stored captures (flag-gated, #263 reuse).
#. **Write** through :func:`~trellis.ingest_corpus.sync.sync_records` — the
   sanctioned reader→core seam ``ingest conversations`` already uses. No
   direct store writes; content-hash idempotency; per-source id-prefix
   scoping; ``MEMORY_STORED`` events; embed-on-ingest.
#. Emit a leak-safe ``MEMORY_OP_JUDGED`` distillation training pair per
   written memory, then advance the watermark for judged sessions only.
"""

from __future__ import annotations

import os
from collections import Counter
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
from trellis.stores.base.event_log import EventType
from trellis_workers.session_capture import distill, gating, reconcile_pass
from trellis_workers.session_capture.models import (
    CandidateMemory,
    CaptureReport,
    SessionDigest,
)
from trellis_workers.session_capture.secret_scan import scan as scan_for_leak_classes
from trellis_workers.session_capture.transcripts import (
    discover_sessions,
    is_ephemeral_project,
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


def _stat_or_none(path: Path) -> os.stat_result | None:
    """A pre-read stat snapshot; ``None`` if the file vanished (retry later)."""
    try:
        return path.stat()
    except OSError:
        return None


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
        "subagent": candidate.is_subagent,
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
    """Apply the secret-scan, injection, and worthiness gates in order."""
    survivors: list[CandidateMemory] = []
    for candidate in candidates:
        report.candidates_distilled += 1
        rendered = render_memory(candidate)
        # Class *labels* only — the scan never returns matched content, so
        # nothing downstream of this call can log or store what it caught.
        matched_classes = scan_for_leak_classes(rendered)
        if matched_classes:
            for label in matched_classes:
                report.scan_hits_by_class[label] = (
                    report.scan_hits_by_class.get(label, 0) + 1
                )
            report.candidates_blocked_scan += 1
            logger.warning(
                "capture_secret_blocked",
                session_id=candidate.session_id,
                matched_classes=matched_classes,
            )
            continue
        if gating.looks_like_injection(candidate):
            # Counted, never silent — same pattern as the secret gate.
            report.candidates_rejected_injection += 1
            logger.warning("capture_injection_blocked", session_id=candidate.session_id)
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
        if is_ephemeral_project(path, transcripts_root):
            # No durable project for the memory to be about. Counted, not
            # watermarked: if the rule is ever narrowed, these become
            # eligible again without a watermark reset.
            report.sessions_skipped_ephemeral += 1
            continue
        if watermark.is_unchanged(path):
            report.sessions_skipped_watermark += 1
            continue

        # Snapshot BEFORE reading: a tail appended between read-EOF and a
        # post-read stat would otherwise be claimed by the cursor and
        # permanently skipped. With the pre-read snapshot, an appended tail
        # makes the file compare as changed and the session re-processes.
        pre_read_stat = _stat_or_none(path)
        digest = parse_session(path)
        report.sessions_parsed += 1
        report.malformed_lines += digest.malformed_lines

        if not gating.should_distill(digest, sample_denominator):
            # Two different outcomes hide behind one `False`. An empty digest
            # is the reader finding no conversation; sampling is a deliberate
            # cost decision. Counting them together is how #332 stayed
            # invisible — see ``CaptureReport.sessions_skipped_empty``.
            if digest.is_empty:
                report.sessions_skipped_empty += 1
            else:
                report.sessions_sampled_out += 1
            if not dry_run and pre_read_stat is not None:
                watermark.record(path, stat=pre_read_stat)
            continue

        survivors = _capture_session(
            registry,
            digest,
            report=report,
            llm_client=llm_client,
            source_system=source_system,
            id_prefix=id_prefix,
            reconcile_enabled=reconcile_enabled,
            dry_run=dry_run,
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
        if not dry_run and pre_read_stat is not None:
            watermark.record(path, stat=pre_read_stat)

    report.sessions_with_memory = len({c.session_id for c in written})
    _write_records(registry, records, report, source_system, id_prefix, dry_run)
    if not dry_run:
        # After the write seam, never before it: a SUPERSEDE verdict is a
        # claim about two documents, and until _write_records has run the
        # successor does not exist — nor, for a candidate superseded by a
        # later candidate of the same sweep, does the target (#407).
        reconcile_pass.apply_supersessions(
            registry.knowledge.document_store, written, report
        )
        _emit_training_pairs(registry, written, distill_model_id)
        watermark.save()

    _emit_sweep_completed(registry, report, source_system=source_system)
    logger.info("capture_sweep_complete", **report.to_payload())
    return report


def _emit_sweep_completed(
    registry: StoreRegistry,
    report: CaptureReport,
    *,
    source_system: str,
) -> None:
    """Persist the sweep funnel as one ``CAPTURE_SWEEP_COMPLETED`` event.

    **Unconditional**, including a sweep that adjudicated nothing and a dry
    run (flagged, the ``CORPUS_SYNCED`` convention). A conditional emit would
    reproduce the hole this exists to close: ``CORPUS_SYNCED`` fires from the
    write seam and so only when something was written, which makes a sweep
    that judged forty sessions and kept none look identical to a sweep that
    never ran.

    Fail-soft, for the same reason :func:`trellis.ops.write_health.
    record_write_rejection` is: a telemetry outage must not turn a completed
    sweep into a crashed one, and the sweep's real output is already durable
    in the document store by the time this runs.
    """
    payload = report.to_payload()
    # Warning *bodies* carry transcript paths and near-duplicate doc ids;
    # the funnel only needs their shape, and the event log has a different
    # retention profile than the run log.
    warnings = payload.pop("warnings", [])
    payload["warning_kinds"] = dict(
        Counter(str(w.get("kind", "unknown")) for w in warnings)
    )
    payload["source_system"] = source_system
    try:
        registry.operational.event_log.emit(
            EventType.CAPTURE_SWEEP_COMPLETED,
            source=_REQUESTED_BY,
            entity_id=f"capture:{source_system}",
            entity_type="capture_sweep",
            payload=payload,
        )
    except Exception:
        logger.warning("capture_sweep_event_emit_failed", exc_info=True)


def _capture_session(
    registry: StoreRegistry,
    digest: SessionDigest,
    *,
    report: CaptureReport,
    llm_client: LLMClient | None,
    source_system: str,
    id_prefix: str,
    reconcile_enabled: bool,
    dry_run: bool,
) -> list[CandidateMemory] | None:
    """Distil, gate, and reconcile one session; ``None`` if the judge is down.

    ``dry_run`` reaches ``adjudicate`` so the verdict events are withheld too
    (#408). It is a required keyword the whole way down: the parameter was
    simply absent here, which is how a dry run came to write. The verdicts are
    still computed and reported — the plan a dry run prints has to be the plan
    the live sweep would execute.
    """
    distill_result = distill.distill_session(llm_client, digest)
    if distill_result.outcome is distill.DistillOutcome.UNAVAILABLE:
        report.sessions_judge_unavailable += 1
        report.warnings.append(
            {"kind": "distill_unavailable", "session_id": digest.session_id}
        )
        return None
    if distill_result.outcome is distill.DistillOutcome.MALFORMED:
        report.sessions_judge_malformed += 1
        logger.warning(
            "distill_response_malformed",
            session_id=digest.session_id,
            parse_error=distill_result.parse_error,
        )

    report.sessions_triggered += 1
    candidates = list(distill_result.candidates)
    input_hash = content_hash(digest.salient_text)
    input_length = len(digest.salient_text)
    for candidate in candidates:
        candidate.input_hash = input_hash
        candidate.input_length = input_length
        # Provenance, not a judgement: a sub-agent transcript's "user" turns
        # are an orchestrator's prompt, so a reader (and any later weighting)
        # needs to know which kind of conversation this came from.
        candidate.is_subagent = digest.is_subagent

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
            dry_run=dry_run,
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
