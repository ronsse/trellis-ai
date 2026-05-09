"""Top-level pytest configuration shared by unit + integration suites.

Currently scoped to registering a fast ``hypothesis`` profile so property
tests run in a few seconds — not minutes — during ``make test``.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings

# A short, deterministic-feeling profile for in-tree property tests. Property
# tests in this repo are invariant checks, not soak/fuzz tests — 50 examples
# is enough to catch regressions without slowing the unit suite.
settings.register_profile(
    "fast",
    max_examples=50,
    # mock-only paths are fast; explicit None avoids flakes on cold imports
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("fast")
