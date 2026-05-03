"""Subprocess fixtures for outside-in CLI tests.

These tests sit one layer above the Typer ``CliRunner`` suites under
``tests/unit/cli/``. ``CliRunner`` invokes the command function in
the same Python process, skipping the wheel's console-script entry
point, the import-path resolution that happens in a fresh subprocess,
and any environment-variable wiring that the entry-script does. This
module spawns the installed ``trellis`` binary as a real subprocess
against a per-test ``tmp_path`` config dir using the default
SQLite-only backends (the CLI doesn't yet honour the cloud-default
plane-split YAML; see ``src/trellis_cli/stores.py:_get_registry``).

Skipped only when the ``trellis`` console script can't be located on
``PATH``. No live-infra gating — the CLI runs against tmp_path SQLite
so contributors without ``.env`` can run the full suite.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration._live_server import (
    find_console_script,
    initialize_trellis_stores,
)

_SUBCMD_TIMEOUT_SECONDS = 60.0


@pytest.fixture(scope="session")
def trellis_bin() -> str:
    return find_console_script(
        "trellis", install_hint="install with `pip install -e .`"
    )


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    """Subprocess env that points the CLI at a private tmp config dir.

    SQLite stores live under ``tmp_path/data/stores``; the registry
    falls back to its built-in defaults (SQLite for everything except
    blob, which uses ``local``) because the CLI's ``_get_registry``
    constructs ``StoreRegistry(stores_dir=...)`` without a config dict.
    Any data the CLI writes is isolated to ``tmp_path``.
    """
    config_dir = tmp_path / ".trellis"
    data_dir = tmp_path / "data"
    config_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "TRELLIS_CONFIG_DIR": str(config_dir),
            "TRELLIS_DATA_DIR": str(data_dir),
            # Belt-and-braces — strip any live-infra DSNs the parent
            # shell loaded so a stray TRELLIS_KNOWLEDGE_PG_DSN can't
            # silently steer the CLI off SQLite.
            "TRELLIS_KNOWLEDGE_PG_DSN": "",
            "TRELLIS_OPERATIONAL_PG_DSN": "",
        }
    )
    return env


@pytest.fixture
def initialized_cli_env(
    trellis_bin: str,
    cli_env: dict[str, str],
) -> dict[str, str]:
    """Run ``trellis admin init`` once so subsequent commands have stores."""
    initialize_trellis_stores(
        cli_env, trellis_bin, timeout_seconds=_SUBCMD_TIMEOUT_SECONDS
    )
    return cli_env


def run_cli(
    bin_path: str,
    args: list[str],
    env: dict[str, str],
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object]]:
    """Run a CLI subcommand and parse stdout as JSON.

    Returns ``(completed, parsed_json)``. Asserts exit 0 and that
    stdout is decodable JSON — these are the contract every
    ``--format json`` subcommand must honour. structlog logs are
    routed to stderr by ``trellis.logging.configure_stderr_logging``,
    so stdout is the payload alone. Failures dump both streams.
    """
    completed = subprocess.run(  # noqa: S603 — argv is the resolved console-script + caller-supplied args
        [bin_path, *args],
        env=env,
        capture_output=True,
        timeout=_SUBCMD_TIMEOUT_SECONDS,
        check=False,
    )
    stdout = completed.stdout.decode(errors="replace")
    stderr = completed.stderr.decode(errors="replace")
    assert completed.returncode == 0, (
        f"CLI exited {completed.returncode} for {args}:\n"
        f"stdout: {stdout}\nstderr: {stderr}"
    )
    try:
        parsed = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        msg = (
            f"CLI {args} did not emit valid JSON on stdout:\n"
            f"stdout: {stdout!r}\nstderr: {stderr!r}\nerror: {exc}"
        )
        raise AssertionError(msg) from exc
    return completed, parsed


@pytest.fixture
def cli_runner(trellis_bin: str) -> Iterator[object,]:
    """Bind ``run_cli`` to the resolved ``trellis_bin`` for ergonomics.

    Tests can call ``cli_runner(args, env=...)`` instead of repeating
    the binary path on every invocation.
    """

    def _runner(
        args: list[str],
        env: dict[str, str],
    ) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object]]:
        return run_cli(trellis_bin, args, env)

    return _runner
