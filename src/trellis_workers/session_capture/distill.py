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
from typing import TYPE_CHECKING, Any

import structlog

from trellis.llm import Message
from trellis.schemas.memory_op import (
    InputDigest,
    JudgedOpType,
    MemoryOpJudgedPayload,
    SubjectRef,
)
from trellis.stores.base.event_log import EventType
from trellis_workers.session_capture.models import CandidateMemory, SessionDigest

if TYPE_CHECKING:
    from trellis.llm import LLMClient
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)

#: Default local judge model id (label only; the endpoint is machine config).
DEFAULT_DISTILL_MODEL = "hermes3:8b"

#: Cap on salient text sent to the judge — bounds prompt size on long
#: sessions; the tail is where corrections and error resolutions cluster.
_MAX_SALIENT_CHARS = 8000

#: Per-session distillation timeout (seconds).
DEFAULT_TIMEOUT_S = 60.0

_SYSTEM_PROMPT = (
    "You distill durable operator memories from an AI coding session. "
    "Return ONLY memories that pass ALL FOUR tests:\n"
    "- non_derivable: cannot be reconstructed from the repo, docs, or git.\n"
    "- durable: will still matter next month (not session-local state).\n"
    "- actionable: would change what a future agent DOES, not just knows.\n"
    "- attributed: carries concrete evidence (a path, a command, a date).\n"
    "Prefer instructive FAILURES and user CORRECTIONS over routine successes. "
    "NEVER copy raw tool output, secrets, tokens, credentials, or environment "
    "values into a memory — summarize in your own words. If nothing qualifies, "
    "return an empty list.\n"
    'Respond with ONLY a JSON array, each item: {"title": str, "memory": str, '
    '"memory_type": "semantic"|"procedural", "signal": "failure"|"correction"|'
    '"success", "evidence": str, "non_derivable": bool, "durable": bool, '
    '"actionable": bool, "confidence": 0.0-1.0}.'
)


def build_distill_messages(digest: SessionDigest) -> list[Message]:
    """Build the distillation prompt from the secret-free digest only."""
    salient = digest.salient_text[:_MAX_SALIENT_CHARS]
    tool_names = sorted({call.name for call in digest.tool_calls})
    signals = (
        f"has_error={digest.has_error} has_correction={digest.has_correction}"
    )
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
    payload = MemoryOpJudgedPayload(
        op_type=JudgedOpType.DISTILLATION,
        model_id=model_id,
        input_digest=InputDigest(
            hash=candidate.input_hash,
            length=candidate.input_length,
            source_refs=[candidate.session_id],
        ),
        decision=decision,
        confidence=candidate.confidence,
        subject_ref=SubjectRef(ref_type="document", ref_id=candidate.doc_id),
    )
    try:
        event_log.emit(
            EventType.MEMORY_OP_JUDGED,
            source="worker:session-capture.distill",
            entity_id=candidate.doc_id or candidate.session_id,
            entity_type="document",
            payload=payload.model_dump(mode="json"),
        )
    except Exception:
        logger.exception(
            "distill_judged_emit_failed", session_id=candidate.session_id
        )
