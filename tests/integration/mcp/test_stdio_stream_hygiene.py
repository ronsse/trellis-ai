"""The MCP stdio server's stdout carries JSON-RPC and nothing else.

``trellis.logging.configure_stderr_logging`` exists for exactly one reason:
under stdio transport, stdout is the JSON-RPC channel, so a log line written
there corrupts the protocol rather than merely looking untidy.

#377 made that guarantee load-bearing on a *lazy* stream lookup — the config
now stores a rule ("whatever stderr is now") instead of a handle. A lazy rule
that ever resolved to stdout would be worse than the bug it replaced, and two
concrete ways to write one exist: handing structlog ``file=None`` (its
``PrintLogger`` defaults that to stdout), or adding stdout as a fallback for
when stderr is missing. This asserts the outcome end to end rather than
trusting the implementation, because the unit tests in
``tests/unit/test_logging.py`` can only check the seam, not the real server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import trellis

# One initialize request, the smallest exchange that makes a stdio server
# write a real frame to stdout. Stdin reaches EOF immediately afterwards,
# which is how a stdio server is told to shut down — so the subprocess
# exits on its own and needs no timeout-and-kill dance.
_INITIALIZE_REQUEST = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "stream-hygiene-test", "version": "1"},
            },
        }
    )
    + "\n"
)

_SPAWN_TIMEOUT_SECONDS = 60.0


def _server_env(base: dict[str, str]) -> dict[str, str]:
    """Env that makes the subprocess run *this* checkout's code, and log.

    Two deliberate choices.

    ``PYTHONPATH`` is pinned to the ``src`` directory the imported
    :mod:`trellis` actually came from. Without it a subprocess resolves
    ``trellis`` through the venv's editable install, which points at the
    *primary* checkout — so in a git worktree this test would assert against
    whatever is on another branch and report it as this branch's result. The
    in-process suite does not have that problem because ``pyproject.toml``
    sets ``pythonpath = ["src", "."]`` relative to the rootdir; a subprocess
    inherits none of that.

    ``TRELLIS_LOG_LEVEL=INFO`` guarantees the server logs at all. The server
    emits ``mcp_write_provenance`` at INFO before it picks a transport, and
    a stdout-cleanliness assertion over a process that never logged would be
    vacuously true — so the test also asserts that line landed on stderr.
    """
    src_dir = Path(trellis.__file__).resolve().parent.parent
    return {
        **base,
        "PYTHONPATH": str(src_dir),
        "TRELLIS_LOG_LEVEL": "INFO",
    }


def test_stdio_stdout_carries_only_jsonrpc_frames(
    mcp_subprocess_env: dict[str, str],
) -> None:
    """Every stdout line parses as a JSON-RPC frame; logs are on stderr."""
    completed = subprocess.run(
        [sys.executable, "-c", "from trellis.mcp.server import main; main()"],
        input=_INITIALIZE_REQUEST.encode(),
        capture_output=True,
        env=_server_env(mcp_subprocess_env),
        timeout=_SPAWN_TIMEOUT_SECONDS,
        check=False,
    )
    stdout = completed.stdout.decode(errors="replace")
    stderr = completed.stderr.decode(errors="replace")

    # Guards the vacuous pass: a clean stdout proves nothing if the server
    # never wrote a log line in the first place.
    assert "mcp_write_provenance" in stderr, (
        f"server did not log at all, so the stdout assertion below would be "
        f"vacuous; stderr={stderr[:2000]!r}"
    )

    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"no JSON-RPC response on stdout; stderr={stderr[:2000]!r}"
    for line in lines:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - failure path
            pytest_msg = (
                f"non-JSON line on the stdio protocol channel: {line[:400]!r} ({exc})"
            )
            raise AssertionError(pytest_msg) from exc
        assert frame.get("jsonrpc") == "2.0", (
            f"stdout line is JSON but not a JSON-RPC frame: {line[:400]!r}"
        )


def test_stdio_stdout_stays_clean_when_the_process_has_no_stderr(
    mcp_subprocess_env: dict[str, str],
) -> None:
    """A host with no stderr must not push log lines onto the RPC channel.

    ``sys.stderr`` is ``None`` under ``pythonw.exe`` and some frozen bundles.
    Before #377 this was a live corruption path, not a theoretical one:
    ``PrintLoggerFactory(file=sys.stderr)`` with ``sys.stderr is None``
    resolves to ``file=None``, and structlog's ``PrintLogger`` defaults that
    to ``sys.stdout`` — so on such a host the function whose entire job is
    keeping logs off stdout put every line on it.

    Simulated by detaching stderr in the child before the server starts,
    which is as close to that host as a Linux test can get.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.stderr = None; sys.__stderr__ = None; "
                "from trellis.mcp.server import main; main()"
            ),
        ],
        input=_INITIALIZE_REQUEST.encode(),
        capture_output=True,
        env=_server_env(mcp_subprocess_env),
        timeout=_SPAWN_TIMEOUT_SECONDS,
        check=False,
    )
    stdout = completed.stdout.decode(errors="replace")

    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, "no JSON-RPC response on stdout"
    for line in lines:
        frame = json.loads(line)
        assert frame.get("jsonrpc") == "2.0", (
            f"log line leaked onto the JSON-RPC channel: {line[:400]!r}"
        )


def test_the_subprocess_under_test_is_this_checkout(
    mcp_subprocess_env: dict[str, str],
) -> None:
    """Pins the ``PYTHONPATH`` guard in :func:`_server_env`.

    Without it the two tests above spawn the venv's editable install, which
    in a git worktree is a *different* checkout — they would pass or fail on
    code this branch does not contain, silently. This asserts the child
    imports the same ``trellis`` the test session did.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import trellis, os; print(os.path.dirname(trellis.__file__))",
        ],
        capture_output=True,
        env=_server_env(mcp_subprocess_env),
        timeout=_SPAWN_TIMEOUT_SECONDS,
        check=True,
    )
    child_pkg = Path(completed.stdout.decode().strip()).resolve()
    assert child_pkg == Path(trellis.__file__).resolve().parent, (
        f"subprocess imported {child_pkg}, but this session is running "
        f"{Path(trellis.__file__).resolve().parent}"
    )


def test_env_pins_pythonpath_to_the_imported_package() -> None:
    """Cheap unit-level guard on the same invariant, no subprocess needed."""
    env = _server_env(dict(os.environ))
    assert Path(env["PYTHONPATH"]).resolve() == (
        Path(trellis.__file__).resolve().parent.parent
    )
