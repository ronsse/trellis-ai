"""Data structures for Claude Code session auto-capture.

Plain dataclasses (mirroring :mod:`trellis.ingest_corpus.models`) for the
reader → distiller → writer flow. None of them is wire-shaped, so these are
dataclasses rather than ``TrellisModel`` schemas. What the worker persists
from them is plain event payloads (the sweep funnel and each session's pack
join, both in :mod:`trellis_workers.session_capture.capture`) beside the
existing leak-safe :class:`~trellis.schemas.memory_op.MemoryOpJudgedPayload`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trellis_workers.session_capture.outcome import SessionOutcome


@dataclass
class ToolCall:
    """A single ``tool_use`` seen in a transcript — name only, never input.

    Tool *inputs* (a bash command line, a file path with an inline token)
    and tool *outputs* (``op read`` results, env dumps) are deliberately
    excluded from the digest: only the tool name and whether its result
    errored survive parsing on the call, so no raw tool payload can reach
    the distiller. :attr:`SessionDigest.outcome` keeps counts read from a
    ``Bash`` command and its result, never their text.

    ``is_error`` is set by :meth:`SessionDigest.mark_tool_result`, which
    joins a ``tool_result`` block back to the ``tool_use`` it answers on
    ``tool_use_id``. It was declared and documented here from the start and
    **written by nothing** until #306: the flag was read off the result and
    folded straight into the session-level :attr:`SessionDigest.has_error`,
    so per-call attribution was parsed and then discarded. Measured on the
    live corpus at the time: 1,397 errored results across 308 of 451
    sessions, all of it reaching the judge as one boolean.
    """

    name: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolUseRollup:
    """How often one tool was called in a session, and how often it errored.

    The unit the distiller prompt renders. Deliberately counts and nothing
    else — a rollup carries no argument, no path, no output, so it inherits
    :class:`ToolCall`'s guarantee rather than reopening it.
    """

    name: str
    calls: int
    errors: int = 0


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

    Carries only natural-language turns, tool *names*, the pack ids of the
    session's Trellis retrieval results, the session's outcome as counts,
    and structural signals. Raw ``tool_result`` / ``toolUseResult`` content
    — the fields that embed secrets — never lands here (F8 threat model,
    #255 guide): a pack id is read out of a result and kept only once it
    matches the alphabet the server generates it in, and the outcome keeps
    only what it counts.
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

    #: ``tool_use_id`` -> the call it identifies, for the ``tool_result``
    #: join. Parse-time scaffolding, not part of the digest's contract: it
    #: is excluded from ``repr`` and ``==`` so a digest still compares by
    #: the facts it carries.
    _calls_by_use_id: dict[str, ToolCall] = field(
        default_factory=dict, repr=False, compare=False
    )
    has_error: bool = False
    has_correction: bool = False
    #: ``True`` when every recovered turn came from sub-agent records — a
    #: dedicated ``agent-*.jsonl`` transcript rather than a main session.
    #: Rides the captured memory's metadata so a reader can tell that the
    #: "user" turns were an orchestrator's prompt, not a person's.
    is_subagent: bool = False
    #: Distinct pack ids the session's Trellis retrieval results named, in
    #: the order first served. Read from the sidechain records too: a
    #: sub-agent's retrieval is still this file's.
    pack_ids: list[str] = field(default_factory=list)
    #: Results of the Trellis retrieval tools, errored ones included. The
    #: denominator that tells a session that never retrieved (``0``) from one
    #: whose retrievals served no pack.
    retrieval_results: int = 0
    #: How many of :attr:`retrieval_results` came back as errors.
    retrieval_errors: int = 0
    #: Non-error retrieval results that yielded no pack id although they
    #: mention the ``pack_id`` label or arrived in a shape the parser does
    #: not know: a truncated result, or a header off the formatters' layout.
    #: Non-zero means :attr:`pack_ids` may be missing a pack.
    pack_ids_unparsed: int = 0
    #: 1-based position, among :attr:`tool_calls`, of the call whose result
    #: first served a pack (the call behind ``pack_ids[0]``), or ``None``
    #: when no pack was parsed. The pack holdout's unit takes that pack as
    #: its treatment, so this is when the treatment happened.
    first_retrieval_ordinal: int | None = None
    #: 1-based position, among :attr:`tool_calls`, of the first ``Bash`` call
    #: that ran ``git commit`` and did not error (the calls
    #: :attr:`SessionOutcome.commits` counts), or ``None`` when none did. Set
    #: by the parser from the outcome tally, in the ordinal space of
    #: :attr:`first_retrieval_ordinal`. A position and never the command.
    first_commit_ordinal: int | None = None
    #: The character count of the first user record the outcome counts as a
    #: user turn (see :attr:`OutcomeTally.brief_chars`), or ``None`` when the
    #: file has none. In a sub-agent's file that is the dispatcher's brief. A
    #: count and never the text.
    brief_chars: int | None = None
    #: A 12-hex-character SHA-256 prefix of the ``agentType`` in a sub-agent
    #: transcript's ``.meta.json`` sidecar, or ``None`` when there is no
    #: usable one. A digest rather than the name: the holdout needs only to
    #: tell one type from another.
    agent_type_digest: str | None = None
    #: How many Stop or SubagentStop hook feedback messages the transcript
    #: holds, however late. A count rather than a flag so the analysis can
    #: tell "no hook fired" from "the detector matches nothing": see
    #: :data:`~trellis_workers.session_capture.transcripts.HOOK_FEEDBACK_PATTERN`.
    hook_feedback_messages: int = 0
    #: ``len(tool_calls)`` when the first hook feedback message was read, so
    #: the calls up to and including that count precede it. ``None`` when no
    #: hook feedback was read.
    hook_feedback_after_calls: int | None = None
    #: What the session did, as counts, a duration and flags, read from the
    #: whole file and from nothing a pack touched. Counts derived from tool
    #: inputs and results, never their text.
    outcome: SessionOutcome = field(default_factory=SessionOutcome)

    def add_turn(self, role: str, text: str, *, sidechain: bool = False) -> None:
        """Append one turn, preserving transcript order."""
        self.turns.append(Turn(role=role, text=text, sidechain=sidechain))

    def record_tool_use(self, name: str, use_id: str | None = None) -> None:
        """Append one ``tool_use``, indexed by ``use_id`` when it has one.

        The index is what lets :meth:`mark_tool_result` attribute a failure
        to the call it answers. A call with no usable id is still recorded —
        it just cannot be marked, which is the honest outcome rather than a
        dropped call.
        """
        call = ToolCall(name=name)
        self.tool_calls.append(call)
        if use_id:
            self._calls_by_use_id[use_id] = call

    def tool_name(self, use_id: str | None) -> str | None:
        """The name of the call *use_id* identifies, if this file held it."""
        call = self._calls_by_use_id.get(use_id or "")
        return call.name if call is not None else None

    def record_first_retrieval(self, use_id: str | None) -> None:
        """Note the call *use_id* identifies as the one that first served a pack.

        A no-op after the first, and for an id this file never held, which
        leaves :attr:`first_retrieval_ordinal` ``None``: an unknown position
        is not a guessed one.
        """
        if self.first_retrieval_ordinal is not None:
            return
        call = self._calls_by_use_id.get(use_id or "")
        self.first_retrieval_ordinal = next(
            (i for i, held in enumerate(self.tool_calls, 1) if held is call), None
        )

    def record_hook_feedback(self) -> None:
        """Count one hook feedback message, dating the first by calls made so far."""
        self.hook_feedback_messages += 1
        if self.hook_feedback_after_calls is None:
            self.hook_feedback_after_calls = len(self.tool_calls)

    @property
    def hook_feedback_before_first_retrieval(self) -> bool | None:
        """Whether hook feedback came before the call that first served a pack.

        ``None`` when no pack was parsed, because there is no first retrieval
        to be before. The first retrieval call precedes the feedback when its
        ordinal is among the calls made by then.
        """
        if self.first_retrieval_ordinal is None:
            return None
        return (
            self.hook_feedback_after_calls is not None
            and self.hook_feedback_after_calls < self.first_retrieval_ordinal
        )

    @property
    def commit_before_first_retrieval(self) -> bool | None:
        """Whether a commit came before the call that first served a pack.

        ``None`` when no pack was parsed, as for the hook comparison. A
        session that never committed, or committed only after, is ``False``:
        that is a decided answer, where ``None`` is an unknown one.
        """
        if self.first_retrieval_ordinal is None:
            return None
        return (
            self.first_commit_ordinal is not None
            and self.first_commit_ordinal < self.first_retrieval_ordinal
        )

    def retrieval_timing_payload(self) -> dict[str, int | bool | None]:
        """The join's ``retrieval_timing``: where the first retrieval sat.

        ``tool_call_count`` is :attr:`tool_calls` as the ordinal counts it,
        not ``outcome.tool_calls``, which counts a record written again once.
        The ordinal over it is the share of the session's calls that came
        before the first retrieval. ``first_commit_ordinal`` is in the same
        space, so the holdout can tell a retrieval made after the work was
        committed from one made before it.
        """
        return {
            "first_retrieval_ordinal": self.first_retrieval_ordinal,
            "tool_call_count": len(self.tool_calls),
            "hook_feedback_messages": self.hook_feedback_messages,
            "hook_feedback_before_first_retrieval": (
                self.hook_feedback_before_first_retrieval
            ),
            "first_commit_ordinal": self.first_commit_ordinal,
            "commit_before_first_retrieval": self.commit_before_first_retrieval,
        }

    def covariates_payload(self) -> dict[str, int | str | None]:
        """The join's ``covariates``: what was fixed before the session ran.

        ``brief_chars`` is the length of the first user turn and
        ``agent_type_digest`` a pseudonymous handle for the sub-agent's type,
        so the pack holdout can adjust its secondary analysis for both. The
        digest is a 12-hex SHA-256 of a small vocabulary, which trying names
        reverses, so it is kept to the join payload in the operational event
        log (which an admin event read can return) and never reaches a
        report. Both are ``None`` when unknown; the holdout never reads that
        as zero.
        """
        return {
            "brief_chars": self.brief_chars,
            "agent_type_digest": self.agent_type_digest,
        }

    def mark_tool_result(self, use_id: str | None, *, errored: bool) -> None:
        """Attribute one ``tool_result`` back to its ``tool_use``.

        Only failures are recorded, because only failures are asked for: a
        rollup reports calls and errors, and "not errored" is the difference.

        An unresolvable ``use_id`` — absent, or answering a call this file
        never held because a compaction boundary or a truncated write cut it
        away — marks nothing and is **not** treated as an error in the
        parse. What it must not do is narrow :attr:`has_error`, which is set
        by the caller on *any* errored result whether or not this join
        resolves. Routing an existing session-level gate through a new join
        is how a fix quietly removes coverage, so the two are kept
        independent and
        ``tests/unit/workers/session_capture/test_transcripts.py`` pins it.
        """
        if not errored:
            return
        call = self._calls_by_use_id.get(use_id or "")
        if call is not None:
            call.is_error = True

    @property
    def tool_rollup(self) -> list[ToolUseRollup]:
        """Per-tool call and error counts, busiest first.

        Ordered by calls descending then name ascending, so the same digest
        always renders the same string — the prompt is an LLM input and a
        set's iteration order would make two identical sessions two
        different requests.
        """
        calls: dict[str, int] = {}
        errors: dict[str, int] = {}
        for call in self.tool_calls:
            calls[call.name] = calls.get(call.name, 0) + 1
            if call.is_error:
                errors[call.name] = errors.get(call.name, 0) + 1
        return [
            ToolUseRollup(name=name, calls=count, errors=errors.get(name, 0))
            for name, count in sorted(calls.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

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
    #: Sessions whose judge responded but did not return a candidate array.
    #: They remain judged and are watermarked, preserving the retry posture,
    #: but are distinct from a valid empty judgment.
    sessions_judge_malformed: int = 0
    #: Sessions that raised mid-sweep — a fault in reading, recording the
    #: pack join, distilling, gating, reconciling or hashing one session,
    #: caught at ``run_capture``'s per-session boundary so it cannot end the
    #: sweep. No memory or training pair from such a session is written, and
    #: it is left un-watermarked, so a transient fault retries next sweep and
    #: a deterministic one is counted here every night until it is fixed.
    #: What the session did before the fault is not rolled back: the counters
    #: it moved (``sessions_parsed``, ``pack_ids_unparsed``,
    #: ``sessions_triggered``, the candidate counters) keep their increments,
    #: and a pack join or reconcile verdict it emitted stays emitted.
    sessions_errored: int = 0
    #: Distinct sessions that yielded at least one memory surviving every
    #: gate — the coverage numerator. Counted per **session**, not per
    #: document: one session commonly distils to several memories, and
    #: ``memories_written`` therefore cannot answer "did this session produce
    #: anything at all".
    sessions_with_memory: int = 0
    malformed_lines: int = 0
    #: Sessions whose pack join — the ``CAPTURE_SESSION_PACKS`` event naming
    #: the packs a session was served — this sweep wrote, an empty one
    #: included. Every parsed transcript with a turn or a tool call lands
    #: here, in :attr:`pack_joins_unchanged`, or in :attr:`sessions_errored`
    #: when recording its join raised. A dry run counts the joins it would
    #: write and writes none.
    pack_joins_recorded: int = 0
    #: Sessions parsed again whose join was already on record as it stands,
    #: so nothing was written: a judge-outage retry, a watermark reset.
    pack_joins_unchanged: int = 0
    #: :attr:`SessionDigest.pack_ids_unparsed` summed over the sessions this
    #: sweep considered for a join. Non-zero means some join may be missing
    #: a pack; each join's own payload says which.
    pack_ids_unparsed: int = 0
    candidates_distilled: int = 0
    #: Total rejections at the worthiness gate. Kept as the total — not
    #: narrowed to the judged subset — so a figure taken from a sweep before
    #: the split means the same thing as one taken after it. The two
    #: counters below decompose it, and
    #: ``candidates_rejected_worthiness == candidates_rejected_judged_unworthy
    #: + candidates_rejected_floor`` on every sweep.
    candidates_rejected_worthiness: int = 0
    #: The judge's **own** verdict: its ``durable`` / ``actionable`` booleans
    #: came back False. This is the negative class of the #264 distillation
    #: training pairs — it was counted and dropped, which is why that arm
    #: recorded the literal ``keep`` on 869 of 869 production rows. Its
    #: density is the reason it has its own counter: over the 16 sweeps to
    #: 2026-09-12, 15 of 780 distilled candidates (1.9%), which is the entire
    #: discriminative signal that arm has.
    candidates_rejected_judged_unworthy: int = 0
    #: Trellis's deterministic floor on the form of the model's output — no
    #: evidence to attribute the claim to, or a memory under
    #: :data:`~trellis_workers.session_capture.gating.MIN_MEMORY_CHARS`.
    #: Counted apart from the judged verdict because it is *this repo's*
    #: rule, not the judge's, and no training pair is emitted for it.
    candidates_rejected_floor: int = 0
    #: Candidates dropped by the deterministic capture-instruction injection
    #: guard (imperative "remember this" shapes / rubric-stuffing).
    candidates_rejected_injection: int = 0
    #: Candidates dropped by the deterministic secret-scan gate. An integer
    #: count only — the gate never surfaces matched content anywhere.
    candidates_blocked_scan: int = 0
    candidates_reconciled_noop: int = 0
    #: Candidates not judged because their nearest stored capture is labelled
    #: above ``internal`` and the ``session_capture`` route is not declared
    #: local (:mod:`trellis.llm.egress`); each is a plain ADD. Named apart from
    #: ``candidates_reconciled_noop`` and the judge-failure counts: no model
    #: was asked, and nothing failed.
    candidates_skipped_egress_withheld: int = 0
    #: Sessions not distilled because a pack they were served named a document
    #: labelled above ``internal`` (or a chunk whose parent row is gone) and the
    #: ``session_capture`` route is not declared local
    #: (:mod:`trellis.llm.egress`). Left un-watermarked, so a sweep over a local
    #: route distils them; they are re-read on every sweep until then. Not a
    #: judge outage: nothing was asked and nothing failed, so
    #: ``sessions_judge_unavailable`` does not move.
    sessions_skipped_egress_withheld: int = 0
    #: Sessions kept off the route because a store could not answer: the event
    #: log, a pack payload with no readable item record, or a served document's
    #: row. A fault and not a label, so it warns, fails a strict run like
    #: ``sessions_errored`` and is kept apart from the count above. Also left
    #: un-watermarked.
    sessions_skipped_egress_unreadable: int = 0
    #: SUPERSEDE *verdicts* — what the judge decided, counted whether or not
    #: the sweep went on to apply them (a ``--dry-run`` reports these and
    #: applies none). Paired with ``supersessions_failed`` deliberately: a
    #: lone failure count cannot distinguish "none failed" from "none were
    #: attempted", which is the same ambiguity ``sessions_skipped_empty``
    #: exists to remove.
    candidates_reconciled_supersede: int = 0
    #: SUPERSEDE verdicts whose SCD-2 stale-mark could not be applied after
    #: the write seam ran — the target doc had vanished, the target was a
    #: promoted precedent's row (never stamped, #994), or the successor never
    #: landed. What
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
            "sessions_judge_malformed": self.sessions_judge_malformed,
            "sessions_errored": self.sessions_errored,
            "sessions_with_memory": self.sessions_with_memory,
            "malformed_lines": self.malformed_lines,
            "pack_joins_recorded": self.pack_joins_recorded,
            "pack_joins_unchanged": self.pack_joins_unchanged,
            "pack_ids_unparsed": self.pack_ids_unparsed,
            "candidates_distilled": self.candidates_distilled,
            "candidates_rejected_worthiness": self.candidates_rejected_worthiness,
            "candidates_rejected_judged_unworthy": (
                self.candidates_rejected_judged_unworthy
            ),
            "candidates_rejected_floor": self.candidates_rejected_floor,
            "candidates_rejected_injection": self.candidates_rejected_injection,
            "candidates_blocked_scan": self.candidates_blocked_scan,
            "candidates_reconciled_noop": self.candidates_reconciled_noop,
            "candidates_skipped_egress_withheld": (
                self.candidates_skipped_egress_withheld
            ),
            "sessions_skipped_egress_withheld": self.sessions_skipped_egress_withheld,
            "sessions_skipped_egress_unreadable": (
                self.sessions_skipped_egress_unreadable
            ),
            "candidates_reconciled_supersede": self.candidates_reconciled_supersede,
            "supersessions_failed": self.supersessions_failed,
            "memories_written": self.memories_written,
            "memories_skipped_unchanged": self.memories_skipped_unchanged,
            "scan_hits_by_class": dict(self.scan_hits_by_class),
            "warnings": list(self.warnings),
        }
