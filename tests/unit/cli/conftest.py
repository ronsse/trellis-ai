"""Shared fixtures for CLI tests."""

from __future__ import annotations

import pytest

from trellis_cli.stores import _reset_registry


@pytest.fixture(autouse=True)
def _reset_cli_registry() -> None:
    """Reset the cached StoreRegistry between tests to avoid stale connections."""
    _reset_registry()


@pytest.fixture(autouse=True)
def _suppress_structlog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suppress structlog output during CLI tests.

    CliRunner merges stderr into ``result.output``, so even though
    ``trellis_cli.main._configure_cli_logging`` routes structlog to
    stderr in production, in-process unit tests still see log lines
    interleaved with JSON payloads. Pinning ``TRELLIS_LOG_LEVEL`` to
    CRITICAL makes the callback configure a no-op filter, silencing
    everything below CRITICAL — the env var is the supported tuning
    knob for the same purpose.
    """
    monkeypatch.setenv("TRELLIS_LOG_LEVEL", "CRITICAL")
