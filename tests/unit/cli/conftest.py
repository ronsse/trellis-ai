"""Shared fixtures for CLI tests."""

from __future__ import annotations

import gc

import pytest
import structlog
from structlog._config import BoundLoggerLazyProxy

from trellis.logging import configure_stderr_logging
from trellis_cli.stores import _reset_registry


@pytest.fixture(autouse=True)
def _reset_cli_registry() -> None:
    """Reset the cached StoreRegistry between tests to avoid stale connections."""
    _reset_registry()


# Baseline attributes that a freshly-constructed ``BoundLoggerLazyProxy``
# carries. Anything *outside* this set is a memoised bind/log method that
# ``cache_logger_on_first_use=True`` (see ``trellis.logging``) has stuck
# onto the proxy after first use — those caches survive future
# ``structlog.configure`` calls and break ``capture_logs`` in tests that
# run after a CLI test. We clear them at session teardown.
_PROXY_BASELINE_ATTRS = frozenset(
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


def _clear_cached_logger_proxies() -> None:
    """Drop memoised bind/log methods from every live ``BoundLoggerLazyProxy``.

    ``trellis_cli.main`` runs ``configure_stderr_logging`` on every CLI
    invocation, which sets ``cache_logger_on_first_use=True``. That cache
    lives on each lazy-proxy instance — not in ``_CONFIG`` — so neither
    ``structlog.configure`` nor ``structlog.reset_defaults`` evicts it.
    When subsequent tests use ``structlog.testing.capture_logs`` to install
    a capturing processor, the cached bind on every module-level
    ``logger = structlog.get_logger(__name__)`` ignores the new processors
    and short-circuits at whatever level the CLI invocation pinned
    (``CRITICAL`` here, via the env var below). Walking the GC for every
    live proxy and trimming non-baseline attributes is the cleanest
    available eviction — structlog exposes no public API for it.
    """
    for obj in gc.get_objects():
        if isinstance(obj, BoundLoggerLazyProxy):
            extras = [k for k in obj.__dict__ if k not in _PROXY_BASELINE_ATTRS]
            for attr in extras:
                delattr(obj, attr)


@pytest.fixture(autouse=True)
def _suppress_structlog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence structlog during CliRunner tests via ``TRELLIS_LOG_LEVEL=CRITICAL``.

    CliRunner merges stderr into ``result.output`` regardless of where
    the logger writes, so the env-var tuning knob is the cleanest mute.

    The explicit ``configure_stderr_logging()`` call makes the mute
    order-independent: tests that invoke a sub-Typer directly (e.g.
    ``runner.invoke(admin_app, ...)``) bypass the root callback that
    normally applies the env var, and structlog's unconfigured default
    prints INFO to stdout — which pollutes ``result.output`` and breaks
    JSON-parsing assertions when such a test runs first.
    """
    monkeypatch.setenv("TRELLIS_LOG_LEVEL", "CRITICAL")
    configure_stderr_logging()


@pytest.fixture(scope="package", autouse=True)
def _evict_structlog_proxy_cache_around_cli_suite():
    """Evict cached lazy-proxy binds entering AND leaving the CLI package.

    **Entry eviction:** tests that run before this package (e.g. the
    integration loop suites under ``pytest tests/``) instantiate stores
    without ``configure_stderr_logging``, so module-level proxies like
    ``trellis.stores.registry``'s cache an unconfigured INFO/stdout
    bind. That cache survives the CLI suite's CRITICAL reconfiguration
    and leaks ``store_instantiated`` lines into ``result.output``,
    breaking JSON-parsing assertions in ``test_migrate_graph_cli`` et
    al. Clearing on entry lets every proxy re-bind under the CLI
    suite's ``TRELLIS_LOG_LEVEL=CRITICAL``.

    **Exit eviction:** keeps the in-suite CRITICAL caching from leaking
    the other way — downstream packages (``extract``, ``feedback``,
    ``classify``, ``api``) get the structlog state they expect, and
    ``capture_logs`` works because no cached CRITICAL-filtering bind is
    in the way.
    """
    _clear_cached_logger_proxies()
    structlog.reset_defaults()
    yield
    _clear_cached_logger_proxies()
    structlog.reset_defaults()
