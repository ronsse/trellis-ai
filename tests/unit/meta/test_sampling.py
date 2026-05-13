"""Tests for :func:`trellis.meta.sampling.reservoir_sample`."""

from __future__ import annotations

import pytest

from trellis.meta.sampling import (
    DEFAULT_FIRST,
    DEFAULT_LAST,
    DEFAULT_MIDDLE,
    reservoir_sample,
)


def test_returns_everything_when_input_fits_within_cap() -> None:
    """No sampling when ``len(items) <= first + last + middle``."""
    items = list(range(40))
    sampled = reservoir_sample(
        items,
        first=10,
        last=10,
        middle=30,
        seed=0,
    )
    assert sampled == items


def test_always_includes_first_ten() -> None:
    """First ``first`` items appear verbatim at the head of the output."""
    items = list(range(1000))
    sampled = reservoir_sample(items, first=10, last=10, middle=30, seed=42)
    assert sampled[:10] == list(range(10))


def test_always_includes_last_ten() -> None:
    """Last ``last`` items appear verbatim at the tail of the output."""
    items = list(range(1000))
    sampled = reservoir_sample(items, first=10, last=10, middle=30, seed=42)
    assert sampled[-10:] == list(range(990, 1000))


def test_total_count_capped_at_first_last_middle() -> None:
    """Output never exceeds the configured cap."""
    items = list(range(10_000))
    sampled = reservoir_sample(items, first=10, last=10, middle=30, seed=7)
    assert len(sampled) == 50


def test_middle_uses_exactly_middle_items_when_room() -> None:
    """Reservoir fills to exactly ``middle`` items for a long enough input."""
    items = list(range(1000))
    sampled = reservoir_sample(items, first=10, last=10, middle=30, seed=1)
    # 10 head + 30 middle + 10 tail = 50 total
    assert len(sampled) == 50
    head = sampled[:10]
    tail = sampled[-10:]
    middle = sampled[10:-10]
    assert len(middle) == 30
    # Middle entries must come from the interior of the stream
    # (item ids 10..989, exclusive of the tail bucket of 990..999).
    for item in middle:
        assert 10 <= item < 990
    # No duplicates across the three buckets.
    assert len(set(head) & set(middle)) == 0
    assert len(set(middle) & set(tail)) == 0


def test_determinism_same_seed_same_output() -> None:
    """Identical seed + input yields identical output."""
    items = list(range(500))
    a = reservoir_sample(items, first=10, last=10, middle=30, seed=99)
    b = reservoir_sample(items, first=10, last=10, middle=30, seed=99)
    assert a == b


def test_different_seed_different_output() -> None:
    """Distinct seeds usually produce distinct samples for a large input."""
    items = list(range(500))
    a = reservoir_sample(items, first=10, last=10, middle=30, seed=1)
    b = reservoir_sample(items, first=10, last=10, middle=30, seed=2)
    # With 480 middle candidates and 30 reservoir slots the probability
    # of two different seeds producing identical reservoirs is vanishingly
    # small — assert non-equality as a determinism witness.
    assert a != b
    # Head and tail still match because they're deterministic.
    assert a[:10] == b[:10]
    assert a[-10:] == b[-10:]


def test_empty_input_returns_empty_list() -> None:
    """Empty input is valid and yields an empty sample."""
    assert reservoir_sample([], first=10, last=10, middle=30, seed=0) == []


def test_input_shorter_than_first_only_fills_head() -> None:
    """When fewer than ``first`` items exist the result is the input."""
    items = [1, 2, 3]
    sampled = reservoir_sample(items, first=10, last=10, middle=30, seed=0)
    assert sampled == [1, 2, 3]


def test_negative_first_raises() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        reservoir_sample([1, 2, 3], first=-1, last=10, middle=30, seed=0)


def test_negative_last_raises() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        reservoir_sample([1, 2, 3], first=10, last=-1, middle=30, seed=0)


def test_negative_middle_raises() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        reservoir_sample([1, 2, 3], first=10, last=10, middle=-1, seed=0)


def test_zero_middle_keeps_only_head_and_tail() -> None:
    """Setting ``middle=0`` skips the reservoir entirely."""
    items = list(range(100))
    sampled = reservoir_sample(items, first=5, last=5, middle=0, seed=0)
    # 5 head + 0 middle + 5 tail = 10
    assert sampled == [*range(5), *range(95, 100)]


def test_module_defaults_match_adr() -> None:
    """The module-level defaults are the ADR-specified 10 / 10 / 30."""
    assert DEFAULT_FIRST == 10
    assert DEFAULT_LAST == 10
    assert DEFAULT_MIDDLE == 30


def test_works_with_generator_input() -> None:
    """Input is consumed once — generators are fine."""

    def gen():
        yield from range(200)

    sampled = reservoir_sample(gen(), first=10, last=10, middle=30, seed=3)
    assert len(sampled) == 50
    assert sampled[:10] == list(range(10))
    assert sampled[-10:] == list(range(190, 200))
