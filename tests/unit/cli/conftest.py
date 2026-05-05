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
    """Silence structlog during CliRunner tests via ``TRELLIS_LOG_LEVEL=CRITICAL``.

    CliRunner merges stderr into ``result.output`` regardless of where
    the logger writes, so the env-var tuning knob is the cleanest mute.
    """
    monkeypatch.setenv("TRELLIS_LOG_LEVEL", "CRITICAL")
