"""Deterministic trigger and worthiness-gate coverage."""

from __future__ import annotations

from trellis_workers.session_capture import gating
from trellis_workers.session_capture.models import CandidateMemory, SessionDigest

from .conftest import good_candidate


def _digest(**kwargs: object) -> SessionDigest:
    digest = SessionDigest(session_id="sess-fake-0001", source_path="x")
    digest.user_texts.append("did something")
    for key, value in kwargs.items():
        setattr(digest, key, value)
    return digest


def test_error_session_is_capture_mandatory() -> None:
    assert gating.is_capture_mandatory(_digest(has_error=True))


def test_correction_session_is_capture_mandatory() -> None:
    assert gating.is_capture_mandatory(_digest(has_correction=True))


def test_clean_session_not_mandatory() -> None:
    assert not gating.is_capture_mandatory(_digest())


def test_empty_session_never_distilled() -> None:
    empty = SessionDigest(session_id="s", source_path="x")
    assert not gating.should_distill(empty, sample_denominator=1)


def test_mandatory_session_always_distilled_regardless_of_sampling() -> None:
    # A huge denominator would sample almost everything out, but a mandatory
    # session is captured anyway.
    assert gating.should_distill(_digest(has_error=True), sample_denominator=10_000)


def test_sampling_denominator_one_captures_all_clean_sessions() -> None:
    assert gating.should_distill(_digest(), sample_denominator=1)


def test_sampling_is_deterministic_per_session() -> None:
    first = gating.is_sampled("sess-fake-0001", 5)
    again = gating.is_sampled("sess-fake-0001", 5)
    assert first == again


def test_worthiness_accepts_good_candidate() -> None:
    cand = CandidateMemory(**good_candidate())
    assert gating.passes_worthiness(cand)


def test_worthiness_rejects_unattributed() -> None:
    cand = CandidateMemory(**good_candidate(evidence="   "))
    assert not gating.passes_worthiness(cand)


def test_worthiness_rejects_derivable() -> None:
    cand = CandidateMemory(**good_candidate(non_derivable=False))
    assert not gating.passes_worthiness(cand)


def test_worthiness_rejects_non_actionable() -> None:
    cand = CandidateMemory(**good_candidate(actionable=False))
    assert not gating.passes_worthiness(cand)


def test_worthiness_rejects_trivial_short_memory() -> None:
    cand = CandidateMemory(**good_candidate(memory="too short"))
    assert not gating.passes_worthiness(cand)
