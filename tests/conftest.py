"""Top-level pytest configuration shared across the test tree.

Currently registers a fast ``hypothesis`` profile so property-based tests
stay snappy in unit runs — these are invariant checks, not soak tests, and
the default deadline trips on cold imports under tox / CI.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile(
    "fast",
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("fast")
