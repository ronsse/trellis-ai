"""Local-model distillation — the judged stage of capture.

The deterministic tier decided *that* a session is worth examining; this
module decides *what*, if anything, in it is a memory. Per the north-star
ladder (``docs/design/plan-memory-lifecycle.md`` §0.1) the judge is a small
local model (hermes3:8b over an OpenAI-compatible endpoint); it is mocked in
every test.

Two invariants from the #255 guide:

* **Fail-closed.** If the model is unavailable, times out, or returns
  malformed JSON, distillation yields **no** candidates — capture nothing
  rather than capture raw. This is the deliberate opposite of #263's
  reconcile fail-*open*: reconcile guards a user-initiated save (losing the
  save is worse than a dup), capture is autonomous (a bad autonomous write is
  worse than a miss).
* **Never quote raw tool output.** The prompt is built only from the digest's
  natural-language turns and tool *names*; the model is instructed to
  summarize in its own words. The deterministic secret gate is the backstop.

Each kept candidate emits a leak-safe ``MEMORY_OP_JUDGED`` (op_type
``distillation``) training-pair event — digests only, never content — so the
future local memory model's dataset accrues from the first run (#264).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.elision import elide_text
from trellis.core.memory_op_judged import emit_memory_op_judged
from trellis.llm import Message
from trellis.llm.json_response import JSONParseOutcome, parse_json_response
from trellis.schemas.memory_op import (
    REF_TYPE_DOCUMENT,
    InputDigest,
    JudgedOpType,
    SubjectRef,
)
from trellis_workers.session_capture.models import CandidateMemory, SessionDigest

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trellis.llm import LLMClient
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Default local judge model id (label only; the endpoint is machine config).
DEFAULT_DISTILL_MODEL = "hermes3:8b"

#: Cap on salient text sent to the judge — bounds prompt size on long
#: sessions. The elision keeps a head and a tail, and
#: :attr:`~trellis_workers.session_capture.models.SessionDigest.salient_text`
#: is chronological, so the surviving window spans the start and the end of
#: the conversation rather than one speaker's block.
#:
#: **This value is coupled to the judge endpoint's context window, which the
#: client cannot set.** Ollama's OpenAI-compatible endpoint ignores
#: ``num_ctx`` in ``extra_body`` (verified — the request is accepted and the
#: window is unchanged), so a prompt over the server's window is silently
#: truncated server-side and the model answers from the remnant. hermes3:8b
#: does not fail on a truncated prompt; it *fabricates* plausible-looking
#: memories. Raising this constant alone is therefore unsafe. Raise the
#: server window first (``OLLAMA_CONTEXT_LENGTH``, or a Modelfile
#: ``PARAMETER num_ctx``), declare it via
#: :data:`ENV_JUDGE_CONTEXT_TOKENS`, and rely on
#: :func:`_prompt_exceeds_window` to refuse the case where it was not raised
#: enough.
_MAX_SALIENT_CHARS = 8000

#: Cap on the rendered tool-use rollup, in characters.
#:
#: The rollup is **charged against** :data:`_MAX_SALIENT_CHARS` rather than
#: added to it, because the two failure modes are not symmetric: an
#: over-budget prompt does not lose its tail, it is refused outright by
#: :func:`_prompt_exceeds_window` and the session is captured *not at all*.
#: Buying tool density with coverage would be a bad trade and a silent one.
#:
#: Measured over the 451 real sessions of the reference corpus: the rollup
#: renders a median **51** chars, p90 191, max **896** — so this cap
#: truncates **0** of them and exists only so a session with an unusual
#: tool spread cannot starve the conversation it is supposed to describe.
_MAX_TOOL_SUMMARY_CHARS = 900

#: Label the rollup is rendered behind, charged against
#: :data:`_MAX_SALIENT_CHARS` along with the rollup itself.
#:
#: It is a named constant because the charge is against the *line*, not
#: the payload: the label is 30 chars longer than the one it replaced, and
#: charging only the rollup left that difference uncharged — which showed
#: up as 267 of 451 sessions growing (median +26) where the arithmetic
#: predicted 79. A label is prompt text like any other, and an invariant
#: that holds for part of a line is the kind that quietly stops holding.
_TOOL_LINE_LABEL = "Tools used (calls, and how many errored): "

#: Chars-per-token estimate for the truncation check — the same ~4:1
#: convention ``PackBuilder`` uses for its token budgets.
_CHARS_PER_TOKEN = 4

#: Tokens the judge's context window holds. Declared, not detected — the
#: response cannot tell us. Ollama reports ``usage.prompt_tokens`` as the
#: tokens *newly evaluated*, so an identical prompt returns 1 on a cache hit
#: (measured: 1212, then 1, then 1). Reading it as "prompt size" would fire
#: hardest on a retry, which is exactly what the fail-closed path does — a
#: loop that captures nothing. So the check is a pre-flight against a number
#: the operator declares, and it costs no model call.
#:
#: The default matches Ollama's own default window. An operator who raises
#: ``OLLAMA_CONTEXT_LENGTH`` raises this to match.
DEFAULT_JUDGE_CONTEXT_TOKENS = 4096

#: Completion budget reserved out of the window (the ``max_tokens`` the judge
#: is called with). Prompt + completion must both fit, or the server drops
#: prompt tokens to make room.
_COMPLETION_RESERVE_TOKENS = 1200

#: Operator override for :data:`_MAX_SALIENT_CHARS`, for a deployment whose
#: judge endpoint has a larger window than the default assumes.
ENV_MAX_SALIENT_CHARS = "TRELLIS_CAPTURE_MAX_SALIENT_CHARS"

#: Operator declaration of the judge endpoint's context window.
ENV_JUDGE_CONTEXT_TOKENS = "TRELLIS_CAPTURE_JUDGE_CONTEXT_TOKENS"


def _positive_int_env(flag: str, default: int, env: Mapping[str, str] | None) -> int:
    """Read a positive-int env knob, falling back loudly rather than raising.

    A typo in one env var must not take out the nightly sweep.
    """
    source = os.environ if env is None else env
    raw = source.get(flag, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("capture_env_unparseable", flag=flag, value=raw)
        return default
    if value <= 0:
        logger.warning("capture_env_out_of_range", flag=flag, value=value)
        return default
    return value


def max_salient_chars(env: Mapping[str, str] | None = None) -> int:
    """Resolve the salient-text cap, honouring the operator override."""
    return _positive_int_env(ENV_MAX_SALIENT_CHARS, _MAX_SALIENT_CHARS, env)


def judge_context_tokens(env: Mapping[str, str] | None = None) -> int:
    """Resolve the declared judge context window."""
    return _positive_int_env(
        ENV_JUDGE_CONTEXT_TOKENS, DEFAULT_JUDGE_CONTEXT_TOKENS, env
    )


#: Per-session distillation timeout (seconds).
DEFAULT_TIMEOUT_S = 60.0


class DistillOutcome(StrEnum):
    """Session-level outcomes from the distillation judge."""

    CANDIDATES = "candidates"
    EMPTY = "empty"
    MALFORMED = "malformed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DistillResult:
    """Concrete judge outcome; no sentinel values or union return."""

    outcome: DistillOutcome
    candidates: tuple[CandidateMemory, ...] = ()
    parse_error: str | None = None
    unavailable_reason: str | None = None


_SYSTEM_PROMPT = (
    "You distill durable operator memories from an AI coding session. "
    "Return ONLY memories that pass ALL FOUR tests:\n"
    "- non_derivable: cannot be reconstructed from the repo, docs, or git.\n"
    "- durable: will still matter next month (not session-local state).\n"
    "- actionable: would change what a future agent DOES, not just knows.\n"
    "- attributed: carries concrete evidence (a path, a command, a date).\n"
    "Prefer instructive FAILURES and user CORRECTIONS over routine successes.\n"
    "Skip discipline — a session step is NOT a memory when it is only:\n"
    "- a status check that found nothing notable;\n"
    "- a dependency install or build that completed cleanly;\n"
    "- a bare file or directory listing;\n"
    "- a restatement of a finding the session says is already recorded;\n"
    "- research or a search that found nothing.\n"
    "Record what the session learned, built, or fixed — NEVER what you or the "
    'capture process are doing; "Analyzed the session and stored findings" is '
    "not a memory. A session whose SUBJECT is a capture or extraction pipeline "
    "is ordinary subject matter — distil it normally.\n"
    "NEVER copy raw tool output, secrets, tokens, credentials, or environment "
    "values into a memory — summarize in your own words. If nothing qualifies, "
    "return [] and nothing else — never explain the skip in prose. Output that "
    "is not the JSON array is discarded, so a prose explanation is a wasted "
    "response, not a record.\n"
    'Respond with ONLY a JSON array, each item: {"title": str, "memory": str, '
    '"memory_type": "semantic"|"procedural", "signal": "failure"|"correction"|'
    '"success", "evidence": str, "non_derivable": bool, "durable": bool, '
    '"actionable": bool, "confidence": 0.0-1.0}.'
)


def render_tool_summary(digest: SessionDigest, *, max_chars: int | None = None) -> str:
    """Render the tool stream as ``name xN (M errored)``, busiest first.

    Replaces the bare ``sorted(set(names))`` this line used to carry, which
    discarded three things the parse already held: **how often** each tool
    ran, **which** of them failed, and therefore any sense of scale. Measured
    on 451 real sessions, that set compressed a median 66 calls to 3 names
    (median 22x, mean 27x), and 17.7% of all sessions rendered the single
    word ``Bash`` as the complete tool record of the work they did. ``Bash``
    is **83.4% of all 56,030 calls** and appears in 447 of the 451 sessions,
    so as a *name* it is close to information-free; the count and the error
    attribution are the whole signal. (The corpus is live — it grew by three
    calls while being measured, because the measuring session is in it. Treat
    every figure here as a re-derivable order of magnitude, not a constant.)

    The error counts are the sharper half. 1,398 errored tool results across
    308 of those sessions reached the judge as one session-level
    ``has_error`` boolean — which the free-text backstop pushes to **True on
    426 of 451 sessions (94.5%)**, so the flag the prompt spends a line on is
    very close to a constant. Per-tool counts separate "one grep missed" from
    "Bash failed 73 times", and the distiller is asked for failure memories.

    Never renders an argument, a path, or any output: a
    :class:`~trellis_workers.session_capture.models.ToolUseRollup` carries
    counts and a tool name, so this inherits the digest's leak guarantee
    instead of reopening it.

    Truncation is marked, not silent — a dropped tail becomes ``(+N more)``
    so the judge cannot read a cut list as a complete one.

    **What this does not buy, measured.** A/B against the real judge
    (hermes3:8b, ``temperature=0``, 60 sessions sampled deterministically,
    both arms through the shipped ``parse_candidates``) found **no effect on
    judge output**: 148 candidates old against 152 new, 16 sessions yielding
    more and 14 fewer — a two-sided sign test at **p = 0.86** — with mean
    confidence 0.965 against 0.960 and *fewer* sessions producing any
    candidate at all (49 against 46). Failure-signal sessions moved 13 to 15,
    inside the same noise. So the justification for this line is **not** a
    demonstrated capture-quality gain. It is that the loss is real and
    one-directional (a median 66 calls rendered as 3 names; 1,398 errors as
    one boolean true on 94.5% of sessions), that closing it is deterministic
    and costs a *smaller* prompt, and that the alternative #306 proposed was
    a second local LLM to recover what the parse already held. A more capable
    judge may use the signal; this one does not, and saying otherwise would
    be the claim the measurement refuses.

    Args:
        digest: Parsed session. An empty tool stream renders ``"none"``.
        max_chars: Budget for the rendered list. Defaults to
            :data:`_MAX_TOOL_SUMMARY_CHARS`.

    Returns:
        One line's worth of text, never longer than *max_chars*.
    """
    budget = _MAX_TOOL_SUMMARY_CHARS if max_chars is None else max_chars
    rollup = digest.tool_rollup
    if not rollup:
        return "none"

    parts: list[str] = []
    for index, entry in enumerate(rollup):
        piece = f"{entry.name} x{entry.calls}"
        if entry.errors:
            piece += f" ({entry.errors} errored)"
        remaining = len(rollup) - index
        marker = f", (+{remaining} more)"
        candidate = ", ".join([*parts, piece])
        # Keep room for the marker only while a tail actually remains.
        needed = len(candidate) + (len(marker) if remaining > 1 else 0)
        if parts and needed > budget:
            return ", ".join(parts) + f", (+{remaining} more)"
        parts.append(piece)
    rendered = ", ".join(parts)
    return rendered[:budget] if len(rendered) > budget else rendered


def build_distill_messages(digest: SessionDigest) -> list[Message]:
    """Build the distillation prompt from the secret-free digest only.

    An oversize session is capped at :data:`_MAX_SALIENT_CHARS`, and the
    cut is marked with an explicit ``<elided … />`` tag (size + reason,
    #310) so the judge knows material was removed rather than treating
    the cut as the end of the session.

    The tool rollup is **charged against that same cap**, not added to it.
    :func:`_prompt_exceeds_window` sums every message and fails *closed* —
    an over-window prompt is not trimmed, the session is skipped and
    captured nowhere — so a line appended on top of a full budget buys tool
    density by dropping whole sessions, silently.

    What the charge buys is a **tighter bound**, not a smaller prompt in
    every case, and the distinction is worth stating because the measured
    numbers disagree with the tidier claim. The old line rendered
    ``sorted(set(names))`` *on top of* a full 8,000-char salient budget, so
    the prompt's ceiling was 8,000 + an unbounded tool list; here the tool
    line and the conversation share the 8,000 between them. On the
    reference corpus (451 sessions) the salient text is already at the cap
    on **372 of them (82.5%)**, where the line displaces conversation
    one-for-one; on the remaining **79** nothing is displaced, because
    neither text was near the cap, and the prompt grows by the difference
    between the two lines — median **+46** chars, max **+84**. Across the
    corpus the median session's prompt *shrinks* by **34** chars and the
    worst case falls from 10,618 to **9,835**.

    Neither arm puts a single session over the window: the largest prompt
    after the change is 9,835 of the 11,584 chars the default window allows
    (84.9%), and the replay counts **0** sessions that overflow only with
    the rollup. So the guard is not what makes this safe today — the
    headroom is. The charge is what keeps it safe when
    :data:`_MAX_SALIENT_CHARS` is next raised toward the window, which is
    the change that would otherwise turn a tool line into lost captures.

    What is charged is the whole line, label included. Charging only the
    rollup left the label's own 30-char growth uncharged, which put 267
    sessions over their old prompt size where the arithmetic predicted 79
    — small, but in the direction the guard exists to prevent, and found
    only by re-measuring a figure this docstring already asserted.
    """
    tool_line = _TOOL_LINE_LABEL + render_tool_summary(digest)
    salient_budget = max(0, max_salient_chars() - len(tool_line))
    salient = elide_text(digest.salient_text, salient_budget)
    signals = f"has_error={digest.has_error} has_correction={digest.has_correction}"
    user = (
        f"Session signals: {signals}\n"
        f"{tool_line}\n\n"
        f"Conversation (natural-language turns only):\n{salient}\n\n"
        "Return the JSON array of qualifying memories."
    )
    return [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=user),
    ]


def _coerce_candidate(item: Any, session_id: str) -> CandidateMemory | None:
    """Build a candidate from one model item; ``None`` if unusable."""
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    memory = item.get("memory")
    if not isinstance(title, str) or not isinstance(memory, str):
        return None
    if not title.strip() or not memory.strip():
        return None
    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    memory_type = item.get("memory_type")
    signal = item.get("signal")
    evidence = item.get("evidence")
    return CandidateMemory(
        title=title.strip(),
        memory=memory.strip(),
        memory_type=memory_type if isinstance(memory_type, str) else "semantic",
        signal=signal if isinstance(signal, str) else "unknown",
        evidence=evidence.strip() if isinstance(evidence, str) else "",
        non_derivable=bool(item.get("non_derivable")),
        durable=bool(item.get("durable")),
        actionable=bool(item.get("actionable")),
        confidence=max(0.0, min(1.0, confidence)),
        session_id=session_id,
    )


def parse_candidates(raw: str, session_id: str) -> DistillResult:
    """Parse the model's response into a concrete distillation outcome."""
    parsed = parse_json_response(raw)
    if parsed.outcome is JSONParseOutcome.MALFORMED:
        return DistillResult(
            outcome=DistillOutcome.MALFORMED,
            parse_error=parsed.error,
        )
    if parsed.outcome is JSONParseOutcome.EMPTY:
        return DistillResult(outcome=DistillOutcome.EMPTY)
    if not isinstance(parsed.value, list):
        return DistillResult(
            outcome=DistillOutcome.MALFORMED,
            parse_error="expected a JSON array of candidate memories",
        )
    candidates: list[CandidateMemory] = []
    for item in parsed.value:
        candidate = _coerce_candidate(item, session_id)
        if candidate is not None:
            candidates.append(candidate)
    return DistillResult(
        outcome=DistillOutcome.CANDIDATES,
        candidates=tuple(candidates),
    )


def distill_session(
    client: LLMClient | None,
    digest: SessionDigest,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> DistillResult:
    """Distil candidate memories from a session into a concrete outcome.

    The autonomous sweep never writes raw or guessed content when the judge is
    down — the opposite of #263's reconcile fail-open.
    """
    if client is None:
        logger.info("distill_skipped_no_client", session_id=digest.session_id)
        return DistillResult(
            outcome=DistillOutcome.UNAVAILABLE,
            unavailable_reason="no_client",
        )
    messages = build_distill_messages(digest)
    if _prompt_exceeds_window(messages, session_id=digest.session_id):
        return DistillResult(
            outcome=DistillOutcome.UNAVAILABLE,
            unavailable_reason="prompt_too_large",
        )
    try:
        response = asyncio.run(
            asyncio.wait_for(
                client.generate(messages=messages, temperature=0.0, max_tokens=1200),
                timeout=timeout,
            )
        )
    except TimeoutError:
        logger.warning("distill_timeout", session_id=digest.session_id)
        return DistillResult(
            outcome=DistillOutcome.UNAVAILABLE,
            unavailable_reason="timeout",
        )
    except Exception:
        logger.warning("distill_model_error", session_id=digest.session_id)
        return DistillResult(
            outcome=DistillOutcome.UNAVAILABLE,
            unavailable_reason="model_error",
        )
    return parse_candidates(response.content, digest.session_id)


def _prompt_exceeds_window(
    messages: list[Message],
    *,
    session_id: str,
) -> bool:
    """Whether the prompt cannot fit the judge's declared context window.

    A prompt over the window is not an error anywhere in the stack: Ollama
    truncates it server-side, returns 200, and hermes3:8b answers from the
    remnant rather than declining -- inventing memories that appear nowhere
    in the transcript ("Learning Python", "First Git Repository"). The
    worthiness gate cannot catch that, because a fabrication carries
    confident booleans and plausible-looking evidence. For an autonomous
    writer that is the worst available failure mode.

    Checked *before* the call, against a declared window, because the
    response cannot answer the question: Ollama's ``usage.prompt_tokens``
    counts tokens newly evaluated, so an identical prompt reports 1 on a
    cache hit. A post-hoc ratio test therefore fires hardest on a retry --
    the one path the fail-closed contract guarantees -- and would wedge the
    sweep into capturing nothing at all.

    The estimate is deliberately coarse (the same ~4:1 convention
    ``PackBuilder`` budgets with). It only has to be right enough to catch a
    prompt that is multiples over the window, which is the shape this
    guards; the ``TRELLIS_CAPTURE_MAX_SALIENT_CHARS`` default sits well
    inside the default window.
    """
    window = judge_context_tokens()
    estimated = sum(len(m.content) for m in messages) // _CHARS_PER_TOKEN
    if estimated + _COMPLETION_RESERVE_TOKENS <= window:
        return False
    logger.warning(
        "distill_prompt_exceeds_window",
        session_id=session_id,
        prompt_tokens_estimated=estimated,
        completion_reserve_tokens=_COMPLETION_RESERVE_TOKENS,
        judge_context_tokens=window,
        remedy=(
            "prompt does not fit the judge context window; raise the window "
            "server-side (OLLAMA_CONTEXT_LENGTH or a Modelfile num_ctx) and "
            "declare it via TRELLIS_CAPTURE_JUDGE_CONTEXT_TOKENS, or lower "
            "TRELLIS_CAPTURE_MAX_SALIENT_CHARS"
        ),
    )
    return True


def emit_distillation_judged(
    event_log: EventLog,
    *,
    candidate: CandidateMemory,
    decision: str,
    model_id: str,
) -> None:
    """Emit one leak-safe ``MEMORY_OP_JUDGED`` distillation training pair.

    The payload carries only a fingerprint of the session input (hash +
    length + the session id as an opaque ref), the verdict label, the model
    id, and the subject doc ref — never memory content or model prose.
    Best-effort: a telemetry failure never rolls back a committed capture.
    """
    emit_memory_op_judged(
        event_log,
        op_type=JudgedOpType.DISTILLATION,
        source="worker:session-capture.distill",
        model_id=model_id,
        input_digest=InputDigest(
            hash=candidate.input_hash,
            length=candidate.input_length,
            source_refs=[candidate.session_id],
        ),
        decision=decision,
        confidence=candidate.confidence,
        subject_ref=SubjectRef(ref_type=REF_TYPE_DOCUMENT, ref_id=candidate.doc_id),
        entity_id=candidate.doc_id or candidate.session_id,
        entity_type="document",
    )
