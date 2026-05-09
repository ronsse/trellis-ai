"""Shared fixtures for MCP server tests.

Both ``test_server.py`` and ``test_execute_mutation.py`` need to:

* Unwrap FastMCP ``FunctionTool`` objects to their underlying callables.
* Suppress structlog output (these tools log a lot).
* Patch a tmp ``StoreRegistry`` into ``trellis.mcp.server._registry``.

Hoisted here so the two test modules don't drift on the patch shape.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

import trellis.mcp.server as server_mod
from trellis.stores.registry import StoreRegistry


def unwrap_tool(tool_or_fn: Any) -> Any:
    """Unwrap a FastMCP ``FunctionTool`` to its underlying callable.

    Re-exported as a free function so individual test modules can keep
    their existing module-level ``execute_mutation = unwrap_tool(...)``
    pattern.
    """
    return getattr(tool_or_fn, "fn", tool_or_fn)


@pytest.fixture(autouse=True)
def _suppress_structlog() -> Iterator[None]:
    """Filter structlog below CRITICAL for the duration of the test.

    Captures the prior config and restores it in teardown so the filter
    doesn't leak into later tests in the same session — capture-and-restore
    matters because structlog config is process-global.
    """
    prior = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
    )
    try:
        yield
    finally:
        structlog.configure(**prior)


@pytest.fixture(autouse=True)
def temp_registry(tmp_path: Path) -> Iterator[StoreRegistry]:
    """Create a tmp ``StoreRegistry`` and patch it into the MCP server module.

    Autouse so tests that don't take the fixture explicitly still get
    the global ``server_mod._registry`` patched — several MCP tests
    exercise the implicit-registry path.
    """
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir(parents=True)
    registry = StoreRegistry(stores_dir=stores_dir)
    server_mod._registry = registry
    try:
        yield registry
    finally:
        server_mod._registry = None
