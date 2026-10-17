"""Tests for trellis.core.text_safety."""

from __future__ import annotations

import pytest

from trellis.core.text_safety import (
    REPLACEMENT_CHAR,
    contains_nul_or_lone_surrogate,
    replace_nul_and_lone_surrogates,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", False),
        ("", False),
        ("café \U0001f600", False),
        ("a\x00b", True),
        ("a\ud800b", True),
        ("\udfff", True),
        ("\n\t\x01", False),  # other control characters are not hazards
        ("\ud7ff\ue000", False),  # the code points either side of the range
    ],
)
def test_contains_nul_or_lone_surrogate(value: str, expected: bool) -> None:
    assert contains_nul_or_lone_surrogate(value) is expected


def test_replace_keeps_length_and_positions() -> None:
    out = replace_nul_and_lone_surrogates("a\x00b\ud800c")
    assert out == f"a{REPLACEMENT_CHAR}b{REPLACEMENT_CHAR}c"
    assert not contains_nul_or_lone_surrogate(out)


def test_replace_returns_a_safe_string_unchanged() -> None:
    value = "café \U0001f600"
    assert replace_nul_and_lone_surrogates(value) is value


def _old_is_unsafe_char(ch: str) -> bool:
    """The pre-regex per-character predicate, inlined as the oracle below
    so a change to the production fast path can't make the comparison
    vacuous by drifting both sides together."""
    return ch == "\x00" or 0xD800 <= ord(ch) <= 0xDFFF


def _old_contains_nul_or_lone_surrogate(value: str) -> bool:
    return any(_old_is_unsafe_char(ch) for ch in value)


def _old_replace_nul_and_lone_surrogates(value: str) -> str:
    if not _old_contains_nul_or_lone_surrogate(value):
        return value
    return "".join(REPLACEMENT_CHAR if _old_is_unsafe_char(ch) else ch for ch in value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain ascii",
        "café \U0001f600",  # clean non-BMP (emoji)
        "a\x00b",  # NUL
        "a\ud800b",  # lone high surrogate
        # mixed: NUL, high surrogate, low surrogate, emoji
        "mixed \x00 text \ud800 and \udfff more \U0001f600 end",
        "line\n\ttab\x01ctl",  # other control characters stay as they are
        "\ud7ff\ue000",  # the code points either side of the range
    ],
)
def test_fast_path_matches_old_character_walk_implementation(value: str) -> None:
    """The regex fast path (production) and the per-character walk it
    replaced (the oracle, inlined above) must agree on every case,
    including the "no hazard" early-return that makes the fast path
    fast."""
    assert contains_nul_or_lone_surrogate(value) == _old_contains_nul_or_lone_surrogate(
        value
    )
    assert replace_nul_and_lone_surrogates(
        value
    ) == _old_replace_nul_and_lone_surrogates(value)
