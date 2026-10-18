"""Shared unit hint for an out-of-range feedback rating.

``rating`` is bounded to ``[0.0, 1.0]`` on every feedback surface (MCP,
REST, SDK), and the one mistake worth naming is a caller writing a score
on a ``/10`` or ``/100`` scale instead. This module is pure — no
dependency on ``trellis.*`` core — so every surface, including the
HTTP-only SDK, can share one implementation rather than three that drift.
"""

from __future__ import annotations

#: Bucket bounds for the unit-mismatch guess: a rating just above 1.0 and
#: at or below 10 reads as a /10 score; above that and at or below 100
#: reads as a /100 score. Anything else (negative, NaN, far out of range)
#: gets no guess — a wrong guess is worse than none.
_TEN_SCALE_MAX = 10.0
_HUNDRED_SCALE_MAX = 100.0


def rating_out_of_range_hint(rating: float) -> str | None:
    """A unit-mismatch guess for an out-of-range ``rating``, or ``None``.

    ``rating`` must already be known out of ``[0.0, 1.0]`` — this does not
    re-check that bound, only guesses *why*. The returned clause is a
    fragment ("did you mean ...?"), not a full sentence, so a caller
    appends it to whatever already states the valid range in that
    surface's own words rather than restating it twice.

    Every comparison below is false for ``NaN``/``inf``/``-inf``, so a
    non-finite rating returns ``None`` rather than a nonsensical guess.
    """
    if 1.0 < rating <= _TEN_SCALE_MAX:
        return f"did you mean {round(rating / 10, 6)}?"
    if _TEN_SCALE_MAX < rating <= _HUNDRED_SCALE_MAX:
        return f"did you mean {round(rating / 100, 6)}?"
    return None
