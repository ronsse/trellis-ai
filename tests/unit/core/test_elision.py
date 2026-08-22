"""Tests for explicit elision markers on capped text (#310)."""

from __future__ import annotations

import pytest

from trellis.core.elision import (
    ELISION_REASON_OVERSIZE,
    elide_text,
    format_char_count,
)


class TestFormatCharCount:
    def test_under_a_thousand_is_exact(self) -> None:
        assert format_char_count(0) == "0"
        assert format_char_count(734) == "734"
        assert format_char_count(999) == "999"

    def test_thousands_get_one_decimal(self) -> None:
        assert format_char_count(2_345) == "2.3k"

    def test_trailing_zero_decimal_is_dropped(self) -> None:
        assert format_char_count(2_000) == "2k"
        assert format_char_count(1_000) == "1k"

    def test_millions(self) -> None:
        assert format_char_count(1_200_000) == "1.2M"

    def test_rounds_down_so_the_note_never_overstates(self) -> None:
        """A "+2.4k chars" note over a 2,350-char drop would be a lie."""
        assert format_char_count(2_350) == "2.3k"
        assert format_char_count(1_999) == "1.9k"

    def test_a_value_just_under_a_boundary_stays_in_its_unit(self) -> None:
        """Rounding to nearest rendered this as the nonsense "1000k"."""
        assert format_char_count(999_999) == "999.9k"
        assert format_char_count(999_999_999) == "999.9M"


class TestElideText:
    def test_under_cap_returned_verbatim(self) -> None:
        assert elide_text("short", 100) == "short"

    def test_exactly_at_cap_untouched(self) -> None:
        text = "a" * 100
        assert elide_text(text, 100) == text

    def test_cut_is_marked_with_size_and_reason(self) -> None:
        result = elide_text("a" * 130, 100)
        assert result.startswith("a" * 100)
        assert result.endswith(
            '\n<elided chars="30" original_size_chars="130" reason="oversize" />'
        )

    def test_kept_head_is_the_raw_slice(self) -> None:
        """The cap bounds the payload; the marker rides on top of it."""
        text = "word " * 50
        result = elide_text(text, 40)
        head, sep, _ = result.partition("\n<elided ")
        assert sep, "the cut was not marked"
        assert head == text[:40]

    def test_custom_reason(self) -> None:
        assert 'reason="budget"' in elide_text("a" * 10, 4, reason="budget")

    def test_default_reason_is_oversize(self) -> None:
        assert f'reason="{ELISION_REASON_OVERSIZE}"' in elide_text("a" * 10, 4)

    def test_negative_cap_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_chars"):
            elide_text("abc", -1)

    def test_zero_cap_keeps_nothing_but_still_marks(self) -> None:
        result = elide_text("abc", 0)
        assert result.startswith("\n<elided ")
        assert 'chars="3"' in result
