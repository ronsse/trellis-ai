"""Shared ``live_api_server`` fixture for outside-in surface tests.

Spawns ``uvicorn trellis_api.app:create_app --factory`` as a subprocess
against a tmp config dir wired to live Neon Postgres (operational
plane) + AuraDB Neo4j (knowledge plane). The fixture wipes persistent
state via ``eval._live_wipe.wipe_live_state`` before yielding so each
test starts from an empty graph + clean tables. SQLite stores under
``tmp_path`` naturally start clean per test.

Imported into both ``tests/integration/api/conftest.py`` and
``tests/integration/sdk/conftest.py`` so the API smoke matrix and the
SDK live round-trip suite share a single fixture definition. Each
conftest re-exports the symbol via ``from tests.integration._live_server
import live_api_server``; pytest registers any ``@pytest.fixture``
function it finds in a conftest module's namespace.

Skipped when ``TRELLIS_TEST_NEO4J_URI`` *or* ``TRELLIS_TEST_PG_DSN``
isn't set — the cloud-default deployment shape needs both. Mirrors the
gating in ``tests/integration/test_neo4j_e2e.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

NEO4J_URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
NEO4J_USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")
PG_DSN = os.environ.get("TRELLIS_TEST_PG_DSN", "")

# Use the production-default vector index name. AuraDB allows only one
# vector index per (label, property), so a `_test`-suffixed name would
# silently fail to provision when the production index is already there
# — and the production-default is what an outside-in test of the
# deployment shape should exercise anyway.
INTEGRATION_VECTOR_DIMS = 3

# AuraDB cold-start can take ~20s on the free tier; padded a little so
# transient slowdowns don't flake the suite. If a healthz never comes
# back inside this window, something is genuinely wrong with the spawn.
_HEALTHZ_TIMEOUT_SECONDS = 30.0
_HEALTHZ_POLL_INTERVAL_SECONDS = 0.25
_TEARDOWN_TIMEOUT_SECONDS = 5.0


def free_port() -> int:
    """Allocate an unused TCP port the OS doesn't immediately reuse.

    Closes the socket *before* returning so uvicorn can bind. There's a
    tiny TOCTOU window here, but for a single-process test suite on
    localhost the collision rate is effectively zero — the alternative
    (passing port 0 to uvicorn and parsing its stdout for the chosen
    port) trades a known small risk for noisy log scraping.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_cloud_config(config_dir: Path) -> None:
    """Write a config.yaml in the cloud-default deployment shape.

    Mirrors block #2 of ``docs/deployment/recommended-config.yaml``:
    Neo4j on the Knowledge Plane (graph + vector shape #2), Postgres
    on the Operational Plane. Neo4j credentials are baked into the
    YAML literally; Postgres DSNs come from
    ``TRELLIS_KNOWLEDGE_PG_DSN`` / ``TRELLIS_OPERATIONAL_PG_DSN`` set
    on the subprocess env (registry's plane-aware DSN resolver picks
    them up).
    """
    import yaml

    config = {
        "knowledge": {
            "graph": {
                "backend": "neo4j",
                "uri": NEO4J_URI,
                "user": NEO4J_USER,
                "password": NEO4J_PASSWORD,
                "database": NEO4J_DATABASE,
            },
            "vector": {
                "backend": "neo4j",
                "uri": NEO4J_URI,
                "user": NEO4J_USER,
                "password": NEO4J_PASSWORD,
                "database": NEO4J_DATABASE,
                "dimensions": INTEGRATION_VECTOR_DIMS,
            },
            "document": {"backend": "postgres"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "postgres"},
            "event_log": {"backend": "postgres"},
        },
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(config))


def wipe_live_state_for_config(config_dir: Path, env: dict[str, str]) -> None:
    """Truncate Neon + AuraDB state via the eval wipe orchestrator.

    Built using the same ``StoreRegistry.from_config_dir`` path the
    uvicorn lifespan will run, so any wiring bug surfaces here rather
    than after uvicorn boots. The registry is closed before yielding
    to release the connection — uvicorn opens its own.
    """
    from eval._live_wipe import wipe_live_state

    from trellis.stores.registry import StoreRegistry

    # Restore the env so plane-aware DSN resolution sees the test DSNs.
    saved = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        registry = StoreRegistry.from_config_dir(config_dir=config_dir)
        try:
            wipe_live_state(registry)
        finally:
            registry.close()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def spawn_uvicorn(
    env: dict[str, str],
    port: int,
    *,
    log_path: Path,
) -> subprocess.Popen[bytes]:
    """Spawn ``uvicorn trellis_api.app:create_app --factory`` on ``port``.

    Bound to 127.0.0.1 (not 0.0.0.0) so the test never accidentally
    accepts connections off the loopback. ``--factory`` tells uvicorn
    to call ``create_app()`` rather than import an app instance, which
    matches how production launches via ``trellis_api.app.main``.

    **Critical**: stdout + stderr go to ``log_path``, not
    ``subprocess.PIPE``. Using PIPE without a draining thread causes a
    Windows OS-pipe-buffer deadlock — once trellis's structlog writes
    fill ~8KB of buffer (about three /packs calls), the next
    ``print(..., flush=True)`` inside :class:`structlog.PrintLogger`
    blocks forever waiting for the parent to read. Writing to a file
    keeps the diagnostic available (``wait_for_healthz`` reads it on
    failure) without ever blocking the writer.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("wb")
    try:
        return subprocess.Popen(  # noqa: S603 — argv is built from sys.executable + literals
            [
                sys.executable,
                "-m",
                "uvicorn",
                "trellis_api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=env,
            stdout=log_handle,
            stderr=log_handle,
        )
    finally:
        # The OS-level fd is duped into the child; closing the Python
        # handle here is fine and avoids a leaked file object.
        log_handle.close()


def _read_log(log_path: Path) -> str:
    """Best-effort read of a uvicorn log file for error reporting."""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(could not read {log_path}: {exc})"


def wait_for_healthz(
    proc: subprocess.Popen[bytes],
    base_url: str,
    *,
    log_path: Path,
) -> None:
    """Block until ``/healthz`` returns 200 or the process exits.

    Fail-loud on early process exit (validation crashes, import errors)
    so the operator sees the captured uvicorn output. Falls back to a
    timeout error if the process is alive but unresponsive — both
    failure modes are real bugs we want surfaced, not flakes to retry.
    """
    deadline = time.monotonic() + _HEALTHZ_TIMEOUT_SECONDS
    last_err: BaseException | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            msg = (
                f"uvicorn exited with code {proc.returncode} before "
                f"serving /healthz:\nlog: {_read_log(log_path)}"
            )
            raise RuntimeError(msg)
        try:
            resp = httpx.get(f"{base_url}/healthz", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_err = exc
        time.sleep(_HEALTHZ_POLL_INTERVAL_SECONDS)

    # Timed out — terminate the hung process and surface its captured
    # output so the operator can see why startup stalled (validate()
    # crash, port collision, import error, etc.).
    proc.terminate()
    try:
        proc.wait(timeout=_TEARDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_TEARDOWN_TIMEOUT_SECONDS)
    msg = (
        f"uvicorn never responded to /healthz within "
        f"{_HEALTHZ_TIMEOUT_SECONDS}s (last error: {last_err}):\n"
        f"log: {_read_log(log_path)}"
    )
    raise RuntimeError(msg)


def terminate_subprocess(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM uvicorn, escalate to SIGKILL if it hangs.

    Windows ``Popen.terminate`` calls TerminateProcess which is closer
    to SIGKILL semantics than POSIX SIGTERM, but uvicorn's signal
    handlers run identically on both platforms.
    """
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=_TEARDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_TEARDOWN_TIMEOUT_SECONDS)


def find_console_script(name: str, *, install_hint: str) -> str:
    """Resolve an installed console script by name.

    Resolution order: sibling of ``sys.executable`` (the venv's
    ``Scripts/`` or ``bin/`` directory) first, then ``shutil.which``.
    Tests using this prefer the venv's binary over a system-wide
    install, so a stale shim on ``PATH`` can't shadow the wheel
    under test. ``pytest.skip`` if neither path turns up the binary.
    """
    py_dir = Path(sys.executable).parent
    for candidate in (py_dir / f"{name}.exe", py_dir / name):
        if candidate.is_file():
            return str(candidate)

    fallback = shutil.which(name)
    if fallback is not None:
        return fallback

    pytest.skip(
        f"{name} console script not found next to the test runner's "
        f"python or on PATH — {install_hint}"
    )


CLI_SUBCMD_TIMEOUT_SECONDS = 60.0


def run_cli(
    bin_path: str,
    args: list[str],
    env: dict[str, str],
    *,
    timeout_seconds: float = CLI_SUBCMD_TIMEOUT_SECONDS,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, Any]]:
    """Run ``trellis <args>`` as a subprocess and parse its JSON stdout.

    Asserts exit 0 and JSON-decodes stdout. Failures dump both streams.
    """
    completed = subprocess.run(  # noqa: S603 — argv is a resolved console-script + caller args
        [bin_path, *args],
        env=env,
        capture_output=True,
        timeout=timeout_seconds,
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


def initialize_trellis_stores(
    env: dict[str, str],
    trellis_bin: str,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Run ``trellis admin init --format json`` to bootstrap stores.

    The CLI registry exits non-zero if ``stores_dir`` doesn't exist,
    so any subprocess that touches stores needs this first. Asserts
    exit 0 with both streams in the failure message so a broken init
    surfaces immediately rather than as a downstream NoneType error.
    """
    result = subprocess.run(  # noqa: S603 — argv is a known console-script + literals
        [trellis_bin, "admin", "init", "--format", "json"],
        env=env,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    assert result.returncode == 0, (
        f"`trellis admin init` failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout.decode(errors='replace')}\n"
        f"stderr: {result.stderr.decode(errors='replace')}"
    )


def build_subprocess_env(config_dir: Path, data_dir: Path) -> dict[str, str]:
    """Build the env dict used for any subprocess that imports trellis.

    Includes ``TRELLIS_CONFIG_DIR`` / ``TRELLIS_DATA_DIR`` so the
    subprocess's ``StoreRegistry.from_config_dir`` finds the right
    YAML, plane-aware ``TRELLIS_KNOWLEDGE_PG_DSN`` /
    ``TRELLIS_OPERATIONAL_PG_DSN`` for Postgres-backed stores, and
    ``PYTHONPATH`` to expose ``src/`` because pytest's
    ``pythonpath = ["src", "."]`` only affects the test-driver
    process — subprocesses inherit the parent shell's env, not
    pytest's path manipulation.
    """
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = repo_root / "src"
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath_entries = [str(src_dir), str(repo_root)]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)

    env = os.environ.copy()
    env.update(
        {
            "TRELLIS_CONFIG_DIR": str(config_dir),
            "TRELLIS_DATA_DIR": str(data_dir),
            "TRELLIS_KNOWLEDGE_PG_DSN": PG_DSN,
            "TRELLIS_OPERATIONAL_PG_DSN": PG_DSN,
            "PYTHONPATH": os.pathsep.join(pythonpath_entries),
        }
    )
    return env


@pytest.fixture
def live_api_server(tmp_path: Path) -> Iterator[str]:
    """Spawn uvicorn against live Neon + AuraDB; yield the base URL.

    Skipped when the live-infra env vars aren't set. Wipes persistent
    state in Neon + AuraDB before yielding so each test starts from a
    known-empty graph + tables — SQLite tmp_path stores naturally
    start clean. Yields only the base URL: the tests are pure HTTP
    black-box, no in-process registry handle.
    """
    if not NEO4J_URI or not PG_DSN:
        pytest.skip(
            "TRELLIS_TEST_NEO4J_URI and TRELLIS_TEST_PG_DSN must be set "
            "for live API tests"
        )

    config_dir = tmp_path / ".trellis"
    data_dir = tmp_path / "data"
    write_cloud_config(config_dir)
    subprocess_env = build_subprocess_env(config_dir, data_dir)

    wipe_live_state_for_config(
        config_dir,
        env={
            "TRELLIS_CONFIG_DIR": str(config_dir),
            "TRELLIS_DATA_DIR": str(data_dir),
            "TRELLIS_KNOWLEDGE_PG_DSN": PG_DSN,
            "TRELLIS_OPERATIONAL_PG_DSN": PG_DSN,
        },
    )

    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "uvicorn.log"
    proc = spawn_uvicorn(subprocess_env, port, log_path=log_path)
    try:
        wait_for_healthz(proc, base_url, log_path=log_path)
        yield base_url
    finally:
        terminate_subprocess(proc)
