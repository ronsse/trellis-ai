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
