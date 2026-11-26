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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.elision import elide_text
from trellis.core.memory_op_judged import emit_memory_op_judged
from trellis.errors import ConfigError
from trellis.llm import Message
from trellis.llm.json_response import (
    JSONParseOutcome,
    coerce_finite_float,
    parse_json_response,
)
from trellis.schemas.memory_op import (
    REF_TYPE_DOCUMENT,
    REF_TYPE_SESSION,
    InputDigest,
    JudgedOpType,
    SubjectRef,
)
from trellis_workers.session_capture.models import CandidateMemory, SessionDigest

if TYPE_CHECKING:
    from trellis.llm import LLMClient
    from trellis.llm.types import TokenUsage
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
DEFAULT_MAX_SALIENT_CHARS = 8000
_MAX_SALIENT_CHARS = DEFAULT_MAX_SALIENT_CHARS

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
DEFAULT_COMPLETION_TOKENS = 1200
_COMPLETION_RESERVE_TOKENS = DEFAULT_COMPLETION_TOKENS

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


#: Operator choice of the sweep's judge system prompt, by name among
#: :data:`PROMPT_VARIANTS`. Unset means the production default prompt.
ENV_PROMPT_VARIANT = "TRELLIS_CAPTURE_PROMPT_VARIANT"

#: Operator override for the sweep's completion budget (``max_tokens``, and
#: the completion reserve of the window pre-flight). Unset means
#: :data:`DEFAULT_COMPLETION_TOKENS`.
ENV_COMPLETION_TOKENS = "TRELLIS_CAPTURE_COMPLETION_TOKENS"


def completion_tokens(env: Mapping[str, str] | None = None) -> int:
    """Resolve the sweep's completion budget, honouring the operator override."""
    return _positive_int_env(ENV_COMPLETION_TOKENS, DEFAULT_COMPLETION_TOKENS, env)


def capture_system_prompt(env: Mapping[str, str] | None = None) -> str | None:
    """Resolve the sweep's judge system prompt from :data:`ENV_PROMPT_VARIANT`.

    ``None`` when the variable is unset, which :func:`distill_session` reads
    as the production prompt, so an unset deployment sends exactly what it
    always sent. Unlike the numeric knobs, an unknown name raises rather than
    falling back: an operator who asked for a specific prompt and silently got
    another would read the sweep's output as that prompt's.

    Raises:
        ConfigError: the variable names no member of :data:`PROMPT_VARIANTS`.
    """
    source = os.environ if env is None else env
    name = source.get(ENV_PROMPT_VARIANT, "").strip()
    if not name:
        return None
    if name not in PROMPT_VARIANTS:
        known = ", ".join(sorted(PROMPT_VARIANTS))
        msg = (
            f"{ENV_PROMPT_VARIANT}={name!r} is not a known prompt variant;"
            f" known: {known}"
        )
        raise ConfigError(msg, setting=ENV_PROMPT_VARIANT)
    return PROMPT_VARIANTS[name]


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
    #: Token usage the endpoint reported for the call, when it reported any.
    #: Read by the offline judge eval only; the sweep ignores it.
    usage: TokenUsage | None = None
    #: Whether the reply had no content (empty or whitespace); ``None`` when no
    #: reply was parsed. Read by the offline judge eval only, and left out of
    #: equality so the outcome alone decides whether two results match.
    content_empty: bool | None = field(default=None, compare=False)


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

#: Name of the production system prompt among :data:`PROMPT_VARIANTS`.
DEFAULT_PROMPT_VARIANT = "default"

#: Eval-only variant whose ``evidence`` is an exact quote of the session text,
#: so the eval's verbatim ``evidence_check`` measures something. The default
#: prompt asks for "a path, a command, a date", not a quote.
VERBATIM_EVIDENCE_PROMPT_VARIANT = "verbatim_evidence"


def _replace_once(text: str, old: str, new: str) -> str:
    """Replace the single occurrence of ``old``; raise when it is not exactly one.

    Runs at import, so an edit to the production prompt that removes the
    replaced text fails loudly instead of leaving a variant equal to default.
    """
    count = text.count(old)
    if count != 1:
        msg = f"prompt variant anchor found {count} times, expected 1: {old!r}"
        raise ValueError(msg)
    return text.replace(old, new)


_VERBATIM_EVIDENCE_SYSTEM_PROMPT = _replace_once(
    _replace_once(
        _SYSTEM_PROMPT,
        "- attributed: carries concrete evidence (a path, a command, a date).\n",
        "- attributed: carries evidence that is an exact, contiguous quote copied "
        "character-for-character from the session text, 20 to 300 characters "
        "long: no paraphrase, no ellipsis, no added citation or label. This "
        "quote is the one thing you copy rather than summarize; never quote a "
        "secret, token, credential, or environment value. If no such quote "
        "exists, omit the candidate.\n",
    ),
    '"evidence": str, ',
    '"evidence": str (the exact quote, copied verbatim from the session), ',
)

#: System prompts an offline judge eval may select by name. The sweep uses
#: :data:`DEFAULT_PROMPT_VARIANT` unless the operator names another in
#: :data:`ENV_PROMPT_VARIANT`; a candidate prompt is added here, under a new
#: name, so an eval arm can compare it against production on the same
#: transcripts before an operator points the sweep at it.
PROMPT_VARIANTS: Mapping[str, str] = MappingProxyType(
    {
        DEFAULT_PROMPT_VARIANT: _SYSTEM_PROMPT,
        VERBATIM_EVIDENCE_PROMPT_VARIANT: _VERBATIM_EVIDENCE_SYSTEM_PROMPT,
    }
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


def build_distill_messages(
    digest: SessionDigest,
    *,
    salient_chars: int | None = None,
    system_prompt: str | None = None,
) -> list[Message]:
    """Build the distillation prompt from the secret-free digest only.

    ``salient_chars`` and ``system_prompt`` exist for the offline judge eval
    (:mod:`~trellis_workers.session_capture.judge_eval`). Left ``None``, as
    the sweep leaves them, the cap is :func:`max_salient_chars` (operator
    override included) and the prompt is the production one, byte for byte
    (``tests/unit/workers/session_capture/test_distill_parity.py``).

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
    cap = max_salient_chars() if salient_chars is None else salient_chars
    salient_budget = max(0, cap - len(tool_line))
    salient = elide_text(digest.salient_text, salient_budget)
    signals = f"has_error={digest.has_error} has_correction={digest.has_correction}"
    user = (
        f"Session signals: {signals}\n"
        f"{tool_line}\n\n"
        f"Conversation (natural-language turns only):\n{salient}\n\n"
        "Return the JSON array of qualifying memories."
    )
    return [
        Message(
            role="system",
            content=_SYSTEM_PROMPT if system_prompt is None else system_prompt,
        ),
        Message(role="user", content=user),
    ]


def _without_nul(value: Any) -> Any:
    """A reply string minus its NUL characters; any other value untouched.

    A JSON ``\\u0000`` escape decodes to a real NUL, which SQLite stores and
    Postgres refuses in a TEXT column. Stripped where the reply is parsed so
    no later stage, the store write included, has to know about it.
    """
    return value.replace("\x00", "") if isinstance(value, str) else value


def _coerce_candidate(item: Any, session_id: str) -> CandidateMemory | None:
    """Build a candidate from one model item; ``None`` if unusable."""
    if not isinstance(item, dict):
        return None
    title = _without_nul(item.get("title"))
    memory = _without_nul(item.get("memory"))
    if not isinstance(title, str) or not isinstance(memory, str):
        return None
    if not title.strip() or not memory.strip():
        return None
    confidence = coerce_finite_float(item.get("confidence", 0.5))
    if confidence is None:
        confidence = 0.5
    memory_type = _without_nul(item.get("memory_type"))
    signal = _without_nul(item.get("signal"))
    evidence = _without_nul(item.get("evidence"))
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
    salient_chars: int | None = None,
    context_tokens: int | None = None,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
) -> DistillResult:
    """Distil candidate memories from a session into a concrete outcome.

    The autonomous sweep never writes raw or guessed content when the judge is
    down — the opposite of #263's reconcile fail-open.

    The sweep passes neither ``salient_chars`` nor ``context_tokens``, and
    passes ``system_prompt`` / ``max_tokens`` as resolved from
    :data:`ENV_PROMPT_VARIANT` / :data:`ENV_COMPLETION_TOKENS` (``None`` and
    ``1200`` when those are unset). Each ``None`` resolves exactly as before
    these parameters existed (env-honouring cap, env-declared window,
    production prompt, a :data:`DEFAULT_COMPLETION_TOKENS` completion cap).
    The offline judge eval sets them per arm. ``max_tokens`` is both the
    call's completion cap and the completion reserve of the window pre-flight.
    """
    completion_tokens = DEFAULT_COMPLETION_TOKENS if max_tokens is None else max_tokens
    if client is None:
        logger.info("distill_skipped_no_client", session_id=digest.session_id)
        return DistillResult(
            outcome=DistillOutcome.UNAVAILABLE,
            unavailable_reason="no_client",
        )
    messages = build_distill_messages(
        digest, salient_chars=salient_chars, system_prompt=system_prompt
    )
    if _prompt_exceeds_window(
        messages,
        session_id=digest.session_id,
        context_tokens=context_tokens,
        completion_reserve_tokens=completion_tokens,
    ):
        return DistillResult(
            outcome=DistillOutcome.UNAVAILABLE,
            unavailable_reason="prompt_too_large",
        )
    try:
        response = asyncio.run(
            asyncio.wait_for(
                client.generate(
                    messages=messages, temperature=0.0, max_tokens=completion_tokens
                ),
                timeout=timeout,
            )
        )
    except TimeoutError:
        logger.warning("distill_timeout", session_id=digest.session_id)
        return DistillResult(
            outcome=DistillOutcome.UNAVAILABLE,
            unavailable_reason="timeout",
        )
    except Exception as exc:
        # The class name only: a provider's message can quote the prompt.
        logger.warning(
            "distill_model_error",
            session_id=digest.session_id,
            error=type(exc).__name__,
        )
        return DistillResult(
            outcome=DistillOutcome.UNAVAILABLE,
            unavailable_reason="model_error",
        )
    return replace(
        parse_candidates(response.content, digest.session_id),
        usage=response.usage,
        content_empty=not response.content.strip(),
    )


def _prompt_exceeds_window(
    messages: list[Message],
    *,
    session_id: str,
    context_tokens: int | None = None,
    completion_reserve_tokens: int = _COMPLETION_RESERVE_TOKENS,
) -> bool:
    """Whether the prompt cannot fit the judge's declared context window.

    ``context_tokens`` overrides the declared window for one call (the
    offline judge eval's per-arm window); ``None`` reads
    :func:`judge_context_tokens` as the sweep always has.
    ``completion_reserve_tokens`` is the call's completion cap.

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
    window = judge_context_tokens() if context_tokens is None else context_tokens
    estimated = sum(len(m.content) for m in messages) // _CHARS_PER_TOKEN
    if estimated + completion_reserve_tokens <= window:
        return False
    logger.warning(
        "distill_prompt_exceeds_window",
        session_id=session_id,
        prompt_tokens_estimated=estimated,
        completion_reserve_tokens=completion_reserve_tokens,
        judge_context_tokens=window,
        remedy=(
            "prompt does not fit the judge context window; raise the window "
            "server-side (OLLAMA_CONTEXT_LENGTH or a Modelfile num_ctx) and "
            "declare it via TRELLIS_CAPTURE_JUDGE_CONTEXT_TOKENS, or lower "
            "TRELLIS_CAPTURE_MAX_SALIENT_CHARS"
        ),
    )
    return True


#: The distillation arm's whole decision vocabulary — the judge either kept
#: the candidate or refused it. Two labels, named once: a second vocabulary
#: for the same facts is the ``content_type`` / ``document_form`` drift of
#: #325/#326, and sub-slugs ("discard_short", "discard_unattributed") would
#: reintroduce it inside one op_type.
DECISION_KEEP = "keep"
DECISION_DISCARD = "discard"


def _judged_subject(candidate: CandidateMemory) -> tuple[SubjectRef, str]:
    """Point the verdict at what it is actually about, and name its kind.

    A kept memory is a document. A **discard has no document**:
    ``CandidateMemory.doc_id`` is populated by the writer, after the gate
    that rejected the candidate, so it is empty here and a ``doc`` ref would
    carry a dangling id in half the feedback-attribution join key. The
    discard's stable subject is the session the judgement was made about —
    which ``input_digest.source_refs`` already names, so the two halves of
    the pair agree.

    Returns the payload's ``subject_ref`` and the event row's
    ``entity_type``. They are deliberately not the same string: ``ref_type``
    is the join-key vocabulary (``doc`` / ``entity`` / ``session``) and
    ``entity_type`` is the event log's own column, where this worker's
    sibling emitter already writes ``capture_sweep``.
    """
    if candidate.doc_id:
        return SubjectRef(ref_type=REF_TYPE_DOCUMENT, ref_id=candidate.doc_id), (
            "document"
        )
    return SubjectRef(
        ref_type=REF_TYPE_SESSION, ref_id=candidate.session_id
    ), "capture_session"


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
    id, and the subject ref — never memory content or model prose.
    Best-effort: a telemetry failure never rolls back a committed capture.
    """
    subject_ref, entity_type = _judged_subject(candidate)
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
        subject_ref=subject_ref,
        entity_id=subject_ref.ref_id,
        entity_type=entity_type,
    )
