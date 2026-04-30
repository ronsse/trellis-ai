"""Live MCP-protocol fixtures for outside-in tool tests.

Spawns the ``trellis-mcp`` console script as a subprocess and connects
to it through ``fastmcp.Client`` over stdio. Each test invokes a tool
by name through the protocol and asserts on the markdown returned —
proving the tool surface works end-to-end the way an MCP-compatible
agent (Claude Desktop, etc.) would talk to it.

Skipped only when ``trellis-mcp`` isn't on ``PATH`` or next to
``sys.executable``. Runs against ``tmp_path`` SQLite, so no live infra
is needed — the goal is to validate the **protocol layer**, not the
storage backends (those are covered by the API and SDK suites).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from tests.integration._live_server import (
    find_console_script,
    initialize_trellis_stores,
)


@pytest.fixture(scope="session")
def trellis_mcp_bin() -> str:
    return find_console_script(
        "trellis-mcp", install_hint="install with `pip install -e .`"
    )


@pytest.fixture(scope="session")
def trellis_bin_for_init() -> str:
    """Resolve the ``trellis`` CLI for the per-test admin-init step."""
    return find_console_script(
        "trellis", install_hint="install with `pip install -e .`"
    )


@pytest.fixture
def mcp_subprocess_env(tmp_path: Path) -> dict[str, str]:
    """Environment for the spawned MCP subprocess.

    The MCP server resolves stores via
    :func:`trellis.stores.registry.StoreRegistry.from_config_dir`, so
    pointing ``TRELLIS_CONFIG_DIR`` + ``TRELLIS_DATA_DIR`` at a fresh
    ``tmp_path`` gives every test a clean SQLite-backed registry. The
    Postgres DSN env vars are explicitly cleared so a stray ``.env``
    in the parent shell can't drift the subprocess off SQLite.
    """
    config_dir = tmp_path / ".trellis"
    data_dir = tmp_path / "data"
    config_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "TRELLIS_CONFIG_DIR": str(config_dir),
            "TRELLIS_DATA_DIR": str(data_dir),
            "TRELLIS_KNOWLEDGE_PG_DSN": "",
            "TRELLIS_OPERATIONAL_PG_DSN": "",
        }
    )
    return env


@pytest_asyncio.fixture
async def mcp_session(
    trellis_mcp_bin: str,
    trellis_bin_for_init: str,
    mcp_subprocess_env: dict[str, str],
) -> AsyncIterator[Client]:
    """Connect a ``fastmcp.Client`` to a fresh ``trellis-mcp`` subprocess.

    Calls ``trellis admin init`` first so the registry has a clean
    stores dir, then opens the MCP stdio transport. The client's
    async context manager handles initialise/teardown and SIGTERMs the
    subprocess on exit.
    """
    initialize_trellis_stores(mcp_subprocess_env, trellis_bin_for_init)

    transport = StdioTransport(
        command=trellis_mcp_bin,
        args=[],
        env=mcp_subprocess_env,
    )
    async with Client(transport, timeout=30.0) as client:
        yield client
