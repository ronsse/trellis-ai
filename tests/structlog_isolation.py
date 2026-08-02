"""Shared structlog lazy-proxy cache eviction for tests.

``trellis.logging.configure_stderr_logging`` sets
``cache_logger_on_first_use=True``. That cache lives on each
``BoundLoggerLazyProxy`` instance — not in structlog's ``_CONFIG`` — so
neither ``structlog.configure`` nor ``structlog.reset_defaults`` evicts it.
Any test that runs a CLI command or an entry point therefore leaves every
module-level ``logger = structlog.get_logger(__name__)`` holding a memoised
bind pinned to that invocation's level and stream. Later tests using
``structlog.testing.capture_logs`` see nothing, and tests that assert on
captured stdout see leaked log lines from a stream that may already be closed.

Walking the GC for live proxies and trimming their non-baseline attributes is
the cleanest available eviction — structlog exposes no public API for it.
Both ``tests/unit/cli/conftest.py`` and the session-capture entry-point tests
need it; it lives here so there is one copy of the rationale and the attribute
baseline.
"""

from __future__ import annotations

import gc

from structlog._config import BoundLoggerLazyProxy

#: Attributes a freshly-constructed ``BoundLoggerLazyProxy`` carries. Anything
#: outside this set is a memoised bind/log method stuck on after first use.
PROXY_BASELINE_ATTRS = frozenset(
    {
        "_logger",
        "_wrapper_class",
        "_processors",
        "_context_class",
        "_cache_logger_on_first_use",
        "_initial_values",
        "_logger_factory_args",
    }
)


def clear_cached_logger_proxies() -> None:
    """Drop memoised bind/log methods from every live ``BoundLoggerLazyProxy``."""
    for obj in gc.get_objects():
        if isinstance(obj, BoundLoggerLazyProxy):
            for attr in [k for k in obj.__dict__ if k not in PROXY_BASELINE_ATTRS]:
                delattr(obj, attr)
