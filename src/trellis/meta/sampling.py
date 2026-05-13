"""Reservoir sampling for bounding meta-Activity edge fan-out.

A single ``record_meta_analysis()`` invocation might consume tens of
thousands of operational events. Stamping that many ``wasInformedBy``
edges per Activity would balloon the graph and make retrieval over
Activities useless — so each Activity caps its provenance edges by
deterministically sampling the consumed inputs.

The shape is "first ``first`` + last ``last`` + reservoir ``middle`` from
the rest" — both endpoints are always present (operators want to see
the beginning and end of the window), and the interior is uniformly
sampled with Algorithm R so any single middle item has equal probability
of inclusion.

Determinism: the function takes a ``seed`` parameter and constructs a
fresh :class:`random.Random` instance — never touches the global RNG —
so a test that pins the seed gets the same sample every run. This is
the same pattern used by every other deterministic helper in the
codebase (e.g., ``trellis.testing.fixtures``).

Per ``docs/design/plan-dogfooding-meta-traces.md`` §2: the sampling
helpers must raise on misuse rather than silently truncate. Negative
``first`` / ``last`` / ``middle`` are caller bugs and surface as
:class:`ValueError`.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from typing import TypeVar

T = TypeVar("T")

#: Default head / tail / middle splits per the ADR. Always include the
#: first 10 + last 10 of the window so operators can spot-check both
#: edges; the middle ``DEFAULT_MIDDLE`` (= 30) are reservoir-sampled
#: from whatever sits between. 10 + 10 + 30 = 50 edge cap.
DEFAULT_FIRST: int = 10
DEFAULT_LAST: int = 10
DEFAULT_MIDDLE: int = 30


def reservoir_sample(
    items: Iterable[T],
    *,
    first: int = DEFAULT_FIRST,
    last: int = DEFAULT_LAST,
    middle: int = DEFAULT_MIDDLE,
    seed: int,
) -> list[T]:
    """Sample ``items`` to at most ``first + last + middle`` entries.

    Strategy:

    * Always include the first ``first`` items of the stream (FIFO).
    * Always include the last ``last`` items of the stream (LIFO).
    * Uniformly sample ``middle`` items from whatever sits between the
      head and the tail using Algorithm R, seeded by ``seed``.

    The returned list preserves the original ordering of the items it
    contains (head chunk, then sampled middle in stream order, then
    tail chunk). It is never longer than ``first + last + middle`` and
    may be shorter if the input has fewer items than the cap.

    The implementation iterates the input exactly once so callers can
    pass generators of unknown length without materialising them.

    Args:
        items: Source iterable. Consumed once.
        first: Size of the always-included head. Must be ``>= 0``.
        last: Size of the always-included tail. Must be ``>= 0``.
        middle: Reservoir size for the interior. Must be ``>= 0``.
        seed: Seed for the local :class:`random.Random` instance. Two
            calls with the same seed and identical input produce
            identical output.

    Raises:
        ValueError: If any of ``first`` / ``last`` / ``middle`` is
            negative.

    Returns:
        Sampled list in stream order.
    """
    if first < 0 or last < 0 or middle < 0:
        msg = (
            "reservoir_sample: first / last / middle must each be >= 0, "
            f"got first={first}, last={last}, middle={middle}"
        )
        raise ValueError(msg)

    rng = random.Random(seed)  # noqa: S311 — sampling is not security-sensitive

    head: list[T] = []
    tail: list[T] = []  # bounded ring of last `last` items not in head
    reservoir: list[T] = []
    # ``middle_index`` is the count of items that have *passed through*
    # the candidate-for-reservoir slot — i.e., items that left the tail
    # ring because a newer one displaced them.
    middle_index = 0

    for item in items:
        if len(head) < first:
            head.append(item)
            continue

        # Ride along in the tail ring until something newer pushes us
        # out. The displaced item is the new candidate for the middle
        # reservoir.
        if last > 0:
            tail.append(item)
            if len(tail) <= last:
                # Tail not yet full — nothing displaced into the middle.
                continue
            candidate = tail.pop(0)
        else:
            # No tail — every post-head item is a middle candidate.
            candidate = item

        # Algorithm R: the i-th candidate (i starts at 0) replaces a
        # random slot with probability middle / (i + 1).
        if middle == 0:
            middle_index += 1
            continue
        if len(reservoir) < middle:
            reservoir.append(candidate)
        else:
            j = rng.randint(0, middle_index)
            if j < middle:
                reservoir[j] = candidate
        middle_index += 1

    return [*head, *reservoir, *tail]


__all__ = [
    "DEFAULT_FIRST",
    "DEFAULT_LAST",
    "DEFAULT_MIDDLE",
    "reservoir_sample",
]
