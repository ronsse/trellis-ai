"""Data structures for Claude Code session auto-capture.

Plain dataclasses (mirroring :mod:`trellis.ingest_corpus.models`) for the
reader → distiller → writer flow. Nothing here is persisted or wire-shaped,
so these are dataclasses rather than ``TrellisModel`` schemas; the one
persisted contract the worker emits is the existing leak-safe
:class:`~trellis.schemas.memory_op.MemoryOpJudgedPayload`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A single ``tool_use`` seen in a transcript — name only, never input.

    Tool *inputs* (a bash command line, a file path with an inline token)
    and tool *outputs* (``op read`` results, env dumps) are deliberately
    excluded from the digest: only the tool name and whether its result
    errored survive parsing, so no raw tool payload can reach the distiller.
    """

    name: str
    is_error: bool = False


@dataclass
class SessionDigest:
    """A secret-free structured view of one transcript file.

    Carries only natural-language turns, tool *names*, and structural
    signals. Raw ``tool_result`` / ``toolUseResult`` content — the fields
    that embed secrets — never lands here (F8 threat model, #255 guide).
    """

    session_id: str
    source_path: str
    record_count: int = 0
    malformed_lines: int = 0
    sidechain_records: int = 0
    summary_records: int = 0
    unknown_records: int = 0
    user_texts: list[str] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    has_error: bool = False
    has_correction: bool = False

    @property
    def is_empty(self) -> bool:
        """``True`` when no natural-language turns were recovered."""
        return not self.user_texts and not self.assistant_texts

    @property
    def salient_text(self) -> str:
        """Distiller input: the natural-language turns joined, no tool I/O."""
        parts = [f"USER: {text}" for text in self.user_texts]
        parts.extend(f"ASSISTANT: {text}" for text in self.assistant_texts)
        return "\n".join(parts)


@dataclass
class CandidateMemory:
    """One distilled memory the local model proposes for a session.

    The four worthiness booleans are the model's self-assessment of the
    lifecycle-plan §2 gate; :func:`trellis_workers.session_capture.gating`
    enforces them deterministically (a claimed-worthy candidate with no
    evidence is still rejected).
    """

    title: str
    memory: str
    memory_type: str
    signal: str
    evidence: str
    non_derivable: bool
    durable: bool
    actionable: bool
    confidence: float
    session_id: str = ""
    # Leak-safe fingerprint of the session input the judge saw (for the
    # distillation training-pair event) — a hash + length, never content.
    input_hash: str = ""
    input_length: int = 0
    # Populated by the writer after gating:
    content: str = ""
    doc_id: str = ""
    reconciliation: str = ""
    updates_doc_id: str | None = None
    supersedes_doc_id: str | None = None


@dataclass
class CaptureReport:
    """Full report of one capture sweep — the machine-readable run summary."""

    transcripts_root: str
    dry_run: bool = False
    reconcile_enabled: bool = False
    sessions_seen: int = 0
    sessions_skipped_watermark: int = 0
    sessions_parsed: int = 0
    sessions_triggered: int = 0
    sessions_sampled_out: int = 0
    malformed_lines: int = 0
    candidates_distilled: int = 0
    candidates_rejected_worthiness: int = 0
    #: Candidates dropped by the deterministic capture-instruction injection
    #: guard (imperative "remember this" shapes / rubric-stuffing).
    candidates_rejected_injection: int = 0
    #: Candidates dropped by the deterministic secret-scan gate. An integer
    #: count only — the gate never surfaces matched content anywhere.
    candidates_blocked_scan: int = 0
    candidates_reconciled_noop: int = 0
    memories_written: int = 0
    memories_skipped_unchanged: int = 0
    #: Per-class hit counters from the secret-scan gate: class *label* → int.
    #: Plainly named (labels + counts, nothing else) so the report payload is
    #: structurally and nominally safe for every log/print sink.
    scan_hits_by_class: dict[str, int] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """JSON-ready shape for the CLI and structured logs."""
        return {
            "transcripts_root": self.transcripts_root,
            "dry_run": self.dry_run,
            "reconcile_enabled": self.reconcile_enabled,
            "sessions_seen": self.sessions_seen,
            "sessions_skipped_watermark": self.sessions_skipped_watermark,
            "sessions_parsed": self.sessions_parsed,
            "sessions_triggered": self.sessions_triggered,
            "sessions_sampled_out": self.sessions_sampled_out,
            "malformed_lines": self.malformed_lines,
            "candidates_distilled": self.candidates_distilled,
            "candidates_rejected_worthiness": self.candidates_rejected_worthiness,
            "candidates_rejected_injection": self.candidates_rejected_injection,
            "candidates_blocked_scan": self.candidates_blocked_scan,
            "candidates_reconciled_noop": self.candidates_reconciled_noop,
            "memories_written": self.memories_written,
            "memories_skipped_unchanged": self.memories_skipped_unchanged,
            "scan_hits_by_class": dict(self.scan_hits_by_class),
            "warnings": list(self.warnings),
        }
