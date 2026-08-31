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

    The still-current reason: ``configure_stderr_logging`` also installs a
    ``wrapper_class`` carrying a **level**, and ``cache_logger_on_first_use``
    pins that into every live proxy. A suite that ran under
    ``TRELLIS_LOG_LEVEL=CRITICAL`` leaves later tests unable to see their own
    log events, and ``structlog.testing.capture_logs`` blind. Only clearing
    the caches *and* resetting the global config restores both.

    **Historical, and no longer true:** this function was written because
    ``configure_stderr_logging`` pinned the *current* ``sys.stderr`` into the
    logger factory::

        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr)

    so a stream captured at configure time lived on in structlog's global
    ``_CONFIG``, and once click closed that buffer every later log call in
    the process raised ``ValueError: I/O operation on closed file``. #377
    replaced it with a proxy that resolves ``sys.stderr`` per write, so the
    global config no longer holds a stream at all. The stream half of this
    function is now belt-and-braces; the level half is not.
    """
    clear_cached_logger_proxies()
    structlog.reset_defaults()


class IsolatedCliRunner(CliRunner):
    """A :class:`typer.testing.CliRunner` that cannot leak structlog state.

    Subclasses Typer's runner rather than Click's so it accepts both a
    ``Typer`` app and a plain Click command, matching every existing call
    site in this suite.

    ``CliRunner.invoke`` redirects ``sys.stdout`` / ``sys.stderr`` to
    temporary buffers and closes them on return, and any Trellis CLI command
    calls ``configure_stderr_logging()`` on the way in.

    **That used to poison the process.** The configure call pinned the
    then-current (temporary) stderr into structlog's global config, so the
    dead handle outlived the ``invoke`` that created it and the next log
    statement *anywhere* raised — 109 tests across 23 directories in CI when
    one ``CliRunner`` call was added under ``tests/unit/mutate/``, while
    passing locally against older ``click`` / ``structlog`` that did not
    close the buffer. #377 fixed that at the root: the stream is now
    resolved per write, so a bare ``CliRunner`` is survivable and this class
    is no longer load-bearing for it.

    **What it still does**, and why it is kept: ``invoke`` leaves the global
    config carrying whatever *level* the command resolved, memoised into
    every live lazy proxy. A later test using ``capture_logs``, or asserting
    on its own log output, sees nothing. Resetting inside ``invoke`` rather
    than at fixture teardown still matters — the test body asserts *after*
    the invocation, and those assertions may themselves log.
    """

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().invoke(*args, **kwargs)
        finally:
            reset_structlog_global_state()
