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
from typing import Any

import structlog
from structlog._config import BoundLoggerLazyProxy
from typer.testing import CliRunner

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


def reset_structlog_global_state() -> None:
    """Restore structlog to a stream-agnostic default configuration.

    ``clear_cached_logger_proxies`` alone is not enough after code that
    calls :func:`trellis.logging.configure_stderr_logging`. That function
    pins the *current* ``sys.stderr`` into the logger factory::

        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr)

    so the stream is captured at configure time and lives in structlog's
    global ``_CONFIG`` afterwards. If ``sys.stderr`` was a temporary buffer
    at that moment, every later log call in the process writes to it — and
    once the buffer is closed, raises ``ValueError: I/O operation on closed
    file``.

    ``reset_defaults`` restores structlog's own default factory, which
    resolves ``sys.stdout`` per logger construction rather than baking one
    in, so a subsequent re-bind picks up whatever stream is current.
    """
    clear_cached_logger_proxies()
    structlog.reset_defaults()


class IsolatedCliRunner(CliRunner):
    """A :class:`typer.testing.CliRunner` that cannot leak structlog state.

    Subclasses Typer's runner rather than Click's so it accepts both a
    ``Typer`` app and a plain Click command, matching every existing call
    site in this suite.

    ``CliRunner.invoke`` redirects ``sys.stdout`` / ``sys.stderr`` to
    temporary buffers and closes them on return. Any Trellis CLI command
    calls ``configure_stderr_logging()`` on the way in, which pins the
    then-current (temporary) stderr into structlog's global config — so the
    poisoned config outlives the ``invoke`` call that created it, and the
    next log statement *anywhere in the process* raises.

    ``tests/unit/cli/`` handles this with a package-scoped fixture, which
    protects that package only. Any test invoking the CLI from elsewhere
    poisons every test that runs after it and logs. That is not
    hypothetical: it took out 109 tests across 23 directories in CI when a
    single ``CliRunner`` call was added under ``tests/unit/mutate/``, while
    passing locally against older ``click`` / ``structlog`` that did not
    close the buffer.

    Resetting inside ``invoke`` — rather than at fixture teardown — matters:
    the test body typically asserts *after* the invocation, and those
    assertions may themselves log.
    """

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().invoke(*args, **kwargs)
        finally:
            reset_structlog_global_state()
