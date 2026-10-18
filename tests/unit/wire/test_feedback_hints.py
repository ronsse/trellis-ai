"""The shared out-of-range-rating unit hint, used by MCP, REST and SDK.

``rating_out_of_range_hint`` is pure and lives in ``trellis_wire`` so every
feedback surface -- including the HTTP-only SDK -- can share one
implementation instead of three that drift (:mod:`trellis_wire` carries no
``trellis.*`` dependency; see ``tests/unit/sdk/test_isolation.py``).
"""

from __future__ import annotations

import math

import pytest

from trellis_wire.feedback_hints import rating_out_of_range_hint


class TestRatingOutOfRangeHint:
    @pytest.mark.parametrize(
        ("rating", "expected"), [(7, 0.7), (7.0, 0.7), (8.5, 0.85)]
    )
    def test_ten_scale_guess(self, rating: float, expected: float) -> None:
        assert rating_out_of_range_hint(rating) == f"did you mean {expected}?"

    @pytest.mark.parametrize(("rating", "expected"), [(85, 0.85), (42, 0.42)])
    def test_hundred_scale_guess(self, rating: float, expected: float) -> None:
        assert rating_out_of_range_hint(rating) == f"did you mean {expected}?"

    @pytest.mark.parametrize("rating", [-1.0, 0.0, 1.0, 101.0, 1000.0])
    def test_no_guess_outside_either_bucket(self, rating: float) -> None:
        assert rating_out_of_range_hint(rating) is None

    @pytest.mark.parametrize("rating", [math.nan, math.inf, -math.inf])
    def test_non_finite_gets_no_guess(self, rating: float) -> None:
        """Every bucket comparison is false against a non-finite value."""
        assert rating_out_of_range_hint(rating) is None
