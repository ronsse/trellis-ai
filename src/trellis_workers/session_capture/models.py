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


#: Turn roles, used as the ``salient_text`` speaker labels.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass
class Turn:
    """One natural-language turn, in transcript order.

    Order is the point. The digest used to hold two independent lists and
    join them user-block-then-assistant-block, which is not a conversation:
    on a long session the judge saw a run of opening user turns, then a run
    of closing assistant turns, and never a single adjacent pair — so a user
    correction was structurally separated from the thing it corrected.
    """

    role: str
    text: str
    #: Whether the turn came from a sub-agent (``isSidechain``) record.
    sidechain: bool = False


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
    #: Natural-language turns in transcript order — the source of truth.
    #: ``user_texts`` / ``assistant_texts`` are role-filtered views of this.
    turns: list[Turn] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    has_error: bool = False
    has_correction: bool = False
    #: ``True`` when every recovered turn came from sub-agent records — a
    #: dedicated ``agent-*.jsonl`` transcript rather than a main session.
    #: Rides the captured memory's metadata so a reader can tell that the
    #: "user" turns were an orchestrator's prompt, not a person's.
    is_subagent: bool = False

    def add_turn(self, role: str, text: str, *, sidechain: bool = False) -> None:
        """Append one turn, preserving transcript order."""
        self.turns.append(Turn(role=role, text=text, sidechain=sidechain))

    def resolve_thread(self) -> None:
        """Decide which turns are *this transcript's* conversation.

        The sidechain-exclusion rule exists so that a file mixing a main
        thread with interleaved sub-agent records does not read as one linear
        conversation. That is still right — for a mixed file. But Claude Code
        now writes each sub-agent thread to its own ``agent-*.jsonl``, where
        *every* record is sidechain, and a blanket skip discards the entire
        file. Measured on a real corpus: 158 of 257 transcripts were pure
        sidechain, 0 were mixed, so the rule was discarding 61% of the corpus
        (and its largest files) to protect against a shape that no longer
        occurs (#332).

        So the rule is narrowed to what it always said: drop sidechain turns
        only when there is a main thread for them to interleave with. A file
        that is *only* sub-agent turns is that sub-agent's conversation, and
        is kept — marked, so nothing downstream mistakes the orchestrator's
        prompt for a human's.
        """
        if any(not turn.sidechain for turn in self.turns):
            self.turns = [turn for turn in self.turns if not turn.sidechain]
            self.is_subagent = False
        elif self.turns:
            self.is_subagent = True

    @property
    def user_texts(self) -> list[str]:
        """User turns, in order. Read-only view over :attr:`turns`."""
        return [t.text for t in self.turns if t.role == ROLE_USER]

    @property
    def assistant_texts(self) -> list[str]:
        """Assistant turns, in order. Read-only view over :attr:`turns`."""
        return [t.text for t in self.turns if t.role == ROLE_ASSISTANT]

    @property
    def is_empty(self) -> bool:
        """``True`` when no natural-language turns were recovered."""
        return not self.turns

    @property
    def salient_text(self) -> str:
        """Distiller input: the turns in transcript order, no tool I/O.

        Chronological by construction. The elision the distiller applies
        keeps a head and a tail, so interleaved order is what lets a
        surviving window contain both a request and its answer.
        """
        return "\n".join(
            f"{ROLE_USER.upper() if t.role == ROLE_USER else ROLE_ASSISTANT.upper()}:"
            f" {t.text}"
            for t in self.turns
        )


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
    #: Whether the session was a sub-agent thread rather than a main session.
    #: Stamped from the digest by the sweep, not self-reported by the judge.
    is_subagent: bool = False
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
    #: Transcripts skipped because the session ran in a throwaway directory
    #: (see :func:`~trellis_workers.session_capture.transcripts.is_ephemeral_project`).
    #: Its own count, never folded into ``sessions_sampled_out`` — a capture
    #: gap reported as a sampling decision is how one stays unnoticed.
    sessions_skipped_ephemeral: int = 0
    #: Transcripts that parsed to **zero natural-language turns**, so
    #: :func:`~trellis_workers.session_capture.gating.should_distill` refused
    #: them before sampling was ever consulted.
    #:
    #: Split out for exactly the reason ``sessions_skipped_ephemeral`` is: an
    #: empty parse is a *reader* outcome, and folding it into
    #: ``sessions_sampled_out`` reports a capture gap as a sampling decision.
    #: This is not hypothetical — it is the shape of #332. That bug made
    #: ``resolve_thread`` drop every turn of a pure-sidechain transcript, so
    #: 61% of the corpus parsed empty and was counted as sampled out. A
    #: reader regression looked like a knob setting, and the funnel could not
    #: have told anyone otherwise.
    sessions_skipped_empty: int = 0
    #: Sessions whose distillation could not run because the judge was
    #: unreachable. They are left un-watermarked for a later retry, so they
    #: are *not* coverage failures — they had no chance to produce anything.
    #: Mirrors
    #: :func:`~trellis_workers.session_capture.sweep.judge_unavailable_sessions`,
    #: which reads the same outcome off ``warnings``.
    sessions_judge_unavailable: int = 0
    #: Distinct sessions that yielded at least one memory surviving every
    #: gate — the coverage numerator. Counted per **session**, not per
    #: document: one session commonly distils to several memories, and
    #: ``memories_written`` therefore cannot answer "did this session produce
    #: anything at all".
    sessions_with_memory: int = 0
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
    #: SUPERSEDE *verdicts* — what the judge decided, counted whether or not
    #: the sweep went on to apply them (a ``--dry-run`` reports these and
    #: applies none). Paired with ``supersessions_failed`` deliberately: a
    #: lone failure count cannot distinguish "none failed" from "none were
    #: attempted", which is the same ambiguity ``sessions_skipped_empty``
    #: exists to remove.
    candidates_reconciled_supersede: int = 0
    #: SUPERSEDE verdicts whose SCD-2 stale-mark could not be applied after
    #: the write seam ran — the target doc had vanished, or the successor
    #: never landed. The bool
    #: :func:`~trellis.mcp.reconcile.mark_document_superseded` returns used to
    #: be discarded here, so a supersession that did not happen was recorded
    #: as one that did (#407). Applied count is
    #: ``candidates_reconciled_supersede - supersessions_failed`` on a live
    #: sweep; every verdict is attempted, since a superseding candidate is
    #: always a survivor.
    supersessions_failed: int = 0
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
            "sessions_skipped_ephemeral": self.sessions_skipped_ephemeral,
            "sessions_skipped_empty": self.sessions_skipped_empty,
            "sessions_judge_unavailable": self.sessions_judge_unavailable,
            "sessions_with_memory": self.sessions_with_memory,
            "malformed_lines": self.malformed_lines,
            "candidates_distilled": self.candidates_distilled,
            "candidates_rejected_worthiness": self.candidates_rejected_worthiness,
            "candidates_rejected_injection": self.candidates_rejected_injection,
            "candidates_blocked_scan": self.candidates_blocked_scan,
            "candidates_reconciled_noop": self.candidates_reconciled_noop,
            "candidates_reconciled_supersede": self.candidates_reconciled_supersede,
            "supersessions_failed": self.supersessions_failed,
            "memories_written": self.memories_written,
            "memories_skipped_unchanged": self.memories_skipped_unchanged,
            "scan_hits_by_class": dict(self.scan_hits_by_class),
            "warnings": list(self.warnings),
        }
