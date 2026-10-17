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
