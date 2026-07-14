"""Deterministic gates: capture triggers and the worthiness filter.

Per the lifecycle plan (``docs/design/plan-memory-lifecycle.md`` §2) and the
#255 guide amendment, *triggers* are deterministic and *content* is
model-judged:

* **Triggers** — computed structurally from the transcript. Sessions with
  errors or user corrections are capture-mandatory (failure-bias; corrections
  are gold-tier semantic memory). Clean routine sessions are deterministically
  sampled so the sweep still learns from steady-state work without capturing
  everything.
* **Worthiness** — the four-test gate (non-derivable / durable / actionable /
  attributed) applied to each distilled candidate. The model self-assesses the
  first three; this module enforces all four deterministically, so a
  candidate that merely *claims* worthiness but carries no evidence is still
  rejected.
* **Injection guard** — a v1 deterministic backstop against
  capture-instruction injection. The worthiness booleans are model
  self-report and the distillation prompt hands the model the exact rubric,
  so transcript text that *addresses the memory system* ("remember this —
  it's durable, non-derivable and actionable") can otherwise self-certify
  junk into the store autonomously. :func:`looks_like_injection` rejects
  candidates whose text carries imperative capture instructions or stuffs
  2+ rubric terms.

**Residual risk (honest):** unattended capture of adversarial text is
inherently gameable at this tier — a model can launder an injected
instruction into clean-looking prose the patterns below won't match. The
mitigations are layered, not absolute: every capture is provenance-marked
(``capture:claude-code:`` doc-id prefix + ``distilled: true`` metadata) so
evidence-driven retention (#261) can prune captures that never prove useful,
and the secret-scan gate bounds the worst-case damage of a successful
injection to junk, not leakage.
"""

from __future__ import annotations

import re

from trellis.core.hashing import content_hash
from trellis_workers.session_capture.models import CandidateMemory, SessionDigest

#: Minimum memory length — one-liners rarely clear the durability bar and are
#: usually restatements of the intent.
MIN_MEMORY_CHARS = 40

#: User-turn markers that signal an explicit correction. Corrections are
#: pre-verified semantic memory ("actually, planning lives in TODO.md") and
#: must never be lost to sampling.
_CORRECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bactually\b"),
    re.compile(r"(?i)\bno,\s"),
    re.compile(r"(?i)\bthat'?s (?:wrong|incorrect|not right|not correct)\b"),
    re.compile(r"(?i)\b(?:it |that )?should (?:be|have been)\b"),
    re.compile(r"(?i)\binstead of\b"),
    re.compile(r"(?i)\bnot .*? but (?:rather|instead)\b"),
    re.compile(r"(?i)\byou'?re wrong\b"),
    re.compile(r"(?i)\bcorrection\b"),
)

#: Free-text error markers (a backstop to the structural ``is_error`` flag on
#: tool results — some failures surface only in assistant prose).
_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\btraceback \(most recent call last\)"),
    re.compile(r"(?i)\b(?:error|exception|failed|failure)\b"),
    re.compile(r"(?i)\bpermission denied\b"),
    re.compile(r"(?i)\bcommand not found\b"),
)


def detect_correction(texts: list[str]) -> bool:
    """``True`` iff any user turn matches a correction marker."""
    return any(
        pattern.search(text) for text in texts for pattern in _CORRECTION_PATTERNS
    )


def detect_error_markers(texts: list[str]) -> bool:
    """``True`` iff any turn carries a free-text error marker."""
    return any(pattern.search(text) for text in texts for pattern in _ERROR_PATTERNS)


def is_capture_mandatory(digest: SessionDigest) -> bool:
    """Sessions with errors or corrections are always captured (failure-bias)."""
    return digest.has_error or digest.has_correction


def is_sampled(session_id: str, sample_denominator: int) -> bool:
    """Deterministically sample clean sessions ~1 in *sample_denominator*.

    Uses a content hash of the session id so the decision is stable across
    re-runs (a session sampled out today is not suddenly captured tomorrow)
    and needs no stored RNG state. ``sample_denominator <= 1`` captures every
    clean session.
    """
    if sample_denominator <= 1:
        return True
    bucket = int(content_hash(session_id), 16) % sample_denominator
    return bucket == 0


def should_distill(digest: SessionDigest, sample_denominator: int) -> bool:
    """Whether a parsed session proceeds to (costly) model distillation."""
    if digest.is_empty:
        return False
    if is_capture_mandatory(digest):
        return True
    return is_sampled(digest.session_id, sample_denominator)


#: Imperative capture-instruction shapes — text that addresses the memory
#: system ("remember this", "save this as a memory") rather than describing
#: the world. Anchored on imperative/address forms so a memory whose *content*
#: merely mentions remembering something is not caught.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bremember\s+this\b"),
    re.compile(r"(?i)\bsave\s+this(?:\s+as\s+a)?\s+memor(?:y|ies)\b"),
    re.compile(r"(?i)\badd\s+(?:this\s+|it\s+)?to\s+(?:your\s+)?memor(?:y|ies)\b"),
)

#: Worthiness-rubric terms. The distillation prompt hands the model this
#: rubric verbatim, so adversarial transcript text can echo it back to
#: self-certify; 2+ distinct terms in a candidate's prose is rubric-stuffing,
#: not natural description. A single term in prose is fine.
_RUBRIC_TERMS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bnon[-_]?derivable\b"),
    re.compile(r"(?i)\bdurable\b"),
    re.compile(r"(?i)\bactionable\b"),
)


def looks_like_injection(candidate: CandidateMemory) -> bool:
    """Deterministic v1 guard against capture-instruction injection.

    ``True`` when the candidate's text (title + memory + evidence) carries an
    imperative capture instruction or stuffs 2+ distinct worthiness-rubric
    terms. Modest by design — see the module docstring for the residual risk
    and the layered mitigations behind it.
    """
    text = f"{candidate.title}\n{candidate.memory}\n{candidate.evidence}"
    if any(pattern.search(text) for pattern in _INJECTION_PATTERNS):
        return True
    rubric_hits = sum(1 for pattern in _RUBRIC_TERMS if pattern.search(text))
    return rubric_hits >= 2  # noqa: PLR2004 - threshold documented above


def passes_worthiness(candidate: CandidateMemory) -> bool:
    """Enforce the four-test gate deterministically over a candidate.

    All four must hold: non-derivable, durable, actionable (model-assessed),
    and attributed (evidence present — enforced here regardless of the model's
    claim). A too-short memory is rejected as non-durable restatement.
    """
    if not candidate.non_derivable or not candidate.durable:
        return False
    if not candidate.actionable:
        return False
    if not candidate.evidence.strip():
        return False
    return len(candidate.memory.strip()) >= MIN_MEMORY_CHARS
