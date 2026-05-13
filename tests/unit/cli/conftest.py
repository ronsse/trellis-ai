"""Shared fixtures for CLI tests."""

from __future__ import annotations

import gc

import pytest
import structlog
from structlog._config import BoundLoggerLazyProxy

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
    """
    monkeypatch.setenv("TRELLIS_LOG_LEVEL", "CRITICAL")


@pytest.fixture(scope="package", autouse=True)
def _evict_structlog_proxy_cache_after_cli_suite():
    """Evict cached lazy-proxy binds after the CLI test package finishes.

    The package-scoped finalizer keeps the in-suite CRITICAL caching that
    every CLI test relies on (CliRunner merges stderr into ``result.output``
    — uncached + unfiltered logs would leak INFO lines into the captured
    output and break JSON-parsing assertions in
    ``test_migrate_graph_cli`` et al.). Evicting only at *package* teardown
    means downstream test packages (``extract``, ``feedback``, ``classify``,
    ``api``) get the structlog state they expect: ``capture_logs`` works
    again because no cached CRITICAL-filtering bind is in the way.
    """
    yield
    _clear_cached_logger_proxies()
    structlog.reset_defaults()
