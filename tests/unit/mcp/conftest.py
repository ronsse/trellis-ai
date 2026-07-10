"""Shared fixtures for MCP server tests.

``test_server.py``, ``test_execute_mutation.py`` and ``test_auth.py`` need to:

* Unwrap FastMCP ``FunctionTool`` objects to their underlying callables.
* Suppress structlog output (these tools log a lot).
* Patch a tmp ``StoreRegistry`` into ``trellis.mcp.server._registry``.
* Mint scoped API keys, and reset the process-global auth state that
  ``trellis.mcp.auth`` keeps between tests.

Hoisted here so the test modules don't drift on the patch shape.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

import trellis.mcp.auth as auth_mod
import trellis.mcp.server as server_mod
from trellis.auth import generate_api_key
from trellis.stores.registry import StoreRegistry

#: Every env var ``trellis.mcp.auth`` reads. Cleared before each test so a
#: developer's shell can't change what the suite asserts.
_MCP_ENV_VARS = (
    "TRELLIS_MCP_TRANSPORT",
    "TRELLIS_MCP_HOST",
    "TRELLIS_MCP_PORT",
    "TRELLIS_MCP_PATH",
    "TRELLIS_MCP_AUTH_MODE",
    "TRELLIS_MCP_ALLOW_INSECURE_BIND",
)


def unwrap_tool(tool_or_fn: Any) -> Any:
    """Unwrap a FastMCP ``FunctionTool`` to its underlying callable.

    Re-exported as a free function so individual test modules can keep
    their existing module-level ``execute_mutation = unwrap_tool(...)``
    pattern.
    """
    return getattr(tool_or_fn, "fn", tool_or_fn)


def mint(registry: StoreRegistry, scopes: list[str], name: str = "test-key") -> str:
    """Mint and persist a scoped key; return the bearer token."""
    token, record = generate_api_key(name, scopes)
    registry.operational.api_key_store.create(record)
    return token


@pytest.fixture(autouse=True)
def _reset_mcp_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate the process-global auth state ``trellis.mcp.auth`` holds.

    ``_auth_state`` and ``mcp.auth`` are module-level singletons set by
    ``main()``. Without this, one test's ``auth_mode=off`` leaks into the
    next and turns an enforcement assertion into a false pass.
    """
    for var in _MCP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    prior_verifier = server_mod.mcp.auth
    server_mod.mcp.auth = None
    auth_mod.set_auth_enforced(enforced=True)
    try:
        yield
    finally:
        server_mod.mcp.auth = prior_verifier
        auth_mod.set_auth_enforced(enforced=True)


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
    # The MinHash fuzzy-dedup index is a separate module-level cache built
    # from whichever registry was live when it was first used — left in
    # place it dedups memories against a *previous test's* stores. Reset
    # alongside the registry on both sides of the test.
    server_mod._minhash_index = None
    try:
        yield registry
    finally:
        server_mod._registry = None
        server_mod._minhash_index = None
