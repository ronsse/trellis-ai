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
import json
import os
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.elision import elide_text
from trellis.core.memory_op_judged import emit_memory_op_judged
from trellis.llm import Message
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


def build_distill_messages(digest: SessionDigest) -> list[Message]:
    """Build the distillation prompt from the secret-free digest only.

    An oversize session is capped at :data:`_MAX_SALIENT_CHARS`, and the
    cut is marked with an explicit ``<elided … />`` tag (size + reason,
    #310) so the judge knows material was removed rather than treating
    the cut as the end of the session.
    """
    salient = elide_text(digest.salient_text, max_salient_chars())
    tool_names = sorted({call.name for call in digest.tool_calls})
    signals = f"has_error={digest.has_error} has_correction={digest.has_correction}"
    user = (
        f"Session signals: {signals}\n"
        f"Tools used: {', '.join(tool_names) or 'none'}\n\n"
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


def parse_candidates(raw: str, session_id: str) -> list[CandidateMemory]:
    """Parse the model's JSON array into candidates; ``[]`` if malformed.

    Tolerant of a fenced code block; a non-array or non-JSON response yields
    an empty list (fail-closed), never an exception.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    candidates: list[CandidateMemory] = []
    for item in parsed:
        candidate = _coerce_candidate(item, session_id)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def distill_session(
    client: LLMClient | None,
    digest: SessionDigest,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> list[CandidateMemory] | None:
    """Distil candidate memories from a session. Fail-closed on any problem.

    Returns:
        * ``None`` — the judge could not be reached (missing client, transport
          error, timeout). The caller writes nothing **and** leaves the
          session un-watermarked so a later run retries it: a model outage
          must never silently lose a session's memories.
        * ``list`` (possibly empty) — the judge responded. An empty list means
          "judged, nothing worthy"; the caller safely advances the watermark.

    The autonomous sweep never writes raw or guessed content when the judge is
    down — the opposite of #263's reconcile fail-open.
    """
    if client is None:
        logger.info("distill_skipped_no_client", session_id=digest.session_id)
        return None
    messages = build_distill_messages(digest)
    if _prompt_exceeds_window(messages, session_id=digest.session_id):
        return None
    try:
        response = asyncio.run(
            asyncio.wait_for(
                client.generate(messages=messages, temperature=0.0, max_tokens=1200),
                timeout=timeout,
            )
        )
    except TimeoutError:
        logger.warning("distill_timeout", session_id=digest.session_id)
        return None
    except Exception:
        logger.warning("distill_model_error", session_id=digest.session_id)
        return None
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
