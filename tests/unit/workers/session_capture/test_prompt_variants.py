"""The eval-only ``verbatim_evidence`` prompt variant.

It exists so the judge eval's verbatim ``evidence_check`` measures something:
the production prompt asks for evidence as "a path, a command, a date", not a
quote. The variant must differ from production only in the two replaced
pieces, and adding it must not move a byte of the production prompt.
"""

from __future__ import annotations

import pytest

from trellis_workers.session_capture import distill

from .test_distill_parity import _GOLDEN_SHA, _golden_digest, _sha

_VARIANT = distill.PROMPT_VARIANTS[distill.VERBATIM_EVIDENCE_PROMPT_VARIANT]
_DEFAULT = distill.PROMPT_VARIANTS[distill.DEFAULT_PROMPT_VARIANT]


@pytest.fixture(autouse=True)
def _no_operator_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(distill.ENV_MAX_SALIENT_CHARS, raising=False)
    monkeypatch.delenv(distill.ENV_JUDGE_CONTEXT_TOKENS, raising=False)


def test_the_variant_is_registered_under_its_name() -> None:
    assert distill.VERBATIM_EVIDENCE_PROMPT_VARIANT == "verbatim_evidence"
    assert set(distill.PROMPT_VARIANTS) == {"default", "verbatim_evidence"}


def test_the_variant_differs_from_default() -> None:
    assert _VARIANT != _DEFAULT


def test_the_variant_requires_an_exact_quote() -> None:
    assert "exact, contiguous quote" in _VARIANT
    assert "character-for-character" in _VARIANT
    assert "20 to 300 characters" in _VARIANT
    assert "no paraphrase, no ellipsis" in _VARIANT
    assert "omit the candidate" in _VARIANT
    assert "(a path, a command, a date)" not in _VARIANT
    assert "(a path, a command, a date)" in _DEFAULT


def test_the_variant_changes_only_the_attributed_rule_and_evidence_field() -> None:
    variant_lines = _VARIANT.split("\n")
    default_lines = _DEFAULT.split("\n")
    assert len(variant_lines) == len(default_lines)
    changed = [
        (old, new)
        for old, new in zip(default_lines, variant_lines, strict=True)
        if old != new
    ]
    assert [old for old, _ in changed] == [
        "- attributed: carries concrete evidence (a path, a command, a date).",
        next(line for line in default_lines if '"evidence": str, ' in line),
    ]


def test_the_default_prompt_hash_is_unchanged() -> None:
    assert _sha(distill.build_distill_messages(_golden_digest())) == _GOLDEN_SHA
    assert distill.PROMPT_VARIANTS[distill.DEFAULT_PROMPT_VARIANT] is (
        distill._SYSTEM_PROMPT
    )


def test_the_variant_reaches_the_system_message() -> None:
    messages = distill.build_distill_messages(_golden_digest(), system_prompt=_VARIANT)
    assert messages[0].role == "system"
    assert messages[0].content == _VARIANT
    assert _sha(messages) != _GOLDEN_SHA


def test_a_missing_anchor_raises_instead_of_returning_the_input() -> None:
    with pytest.raises(ValueError, match="found 0 times"):
        distill._replace_once(_DEFAULT, "text the prompt does not hold", "x")
    with pytest.raises(ValueError, match="found 2 times"):
        distill._replace_once("ab ab", "ab", "x")
