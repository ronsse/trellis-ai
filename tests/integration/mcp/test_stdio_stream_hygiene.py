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
import subprocess
import sys

from tests.integration._live_server import (
    assert_env_pins_this_checkout,
    assert_subprocess_imports_this_checkout,
)

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
    """``mcp_subprocess_env`` plus a log level that guarantees output.

    The server emits ``mcp_write_provenance`` at INFO before it picks a
    transport, and a stdout-cleanliness assertion over a process that never
    logged would be vacuously true — so the tests below also assert that
    line landed on stderr.

    ``PYTHONPATH`` comes from ``mcp_subprocess_env``, which pins it to this
    checkout via ``repo_src_pythonpath``; without that a spawned server runs
    the editable install's checkout, which in a git worktree is a different
    branch. :func:`test_the_subprocess_under_test_is_this_checkout` guards it.
    """
    return {**base, "TRELLIS_LOG_LEVEL": "INFO"}


def _run_stdio_server(preamble: str, env: dict[str, str]) -> tuple[str, str]:
    """Spawn the stdio server with *preamble*, feed one frame, return streams."""
    assert_env_pins_this_checkout(env, what="_run_stdio_server")
    completed = subprocess.run(  # noqa: S603 — argv is this interpreter + a literal
        [sys.executable, "-c", preamble],
        input=_INITIALIZE_REQUEST.encode(),
        capture_output=True,
        env=env,
        timeout=_SPAWN_TIMEOUT_SECONDS,
        check=False,
    )
    return (
        completed.stdout.decode(errors="replace"),
        completed.stderr.decode(errors="replace"),
    )


def _assert_only_jsonrpc(stdout: str, stderr: str) -> None:
    """Every non-blank stdout line must be a JSON-RPC frame."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"no JSON-RPC response on stdout; stderr={stderr[:2000]!r}"
    for line in lines:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"non-JSON line on the stdio protocol channel: {line[:400]!r} ({exc})"
            raise AssertionError(msg) from exc
        assert frame.get("jsonrpc") == "2.0", (
            f"stdout line is JSON but not a JSON-RPC frame: {line[:400]!r}"
        )


_SERVER_MAIN = "from trellis.mcp.server import main; main()"


def test_stdio_stdout_carries_only_jsonrpc_frames(
    mcp_subprocess_env: dict[str, str],
) -> None:
    """Every stdout line parses as a JSON-RPC frame; logs are on stderr."""
    stdout, stderr = _run_stdio_server(_SERVER_MAIN, _server_env(mcp_subprocess_env))

    # Guards the vacuous pass: a clean stdout proves nothing if the server
    # never wrote a log line in the first place.
    assert "mcp_write_provenance" in stderr, (
        f"server did not log at all, so the stdout assertion below would be "
        f"vacuous; stderr={stderr[:2000]!r}"
    )
    _assert_only_jsonrpc(stdout, stderr)


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
    stdout, stderr = _run_stdio_server(
        f"import sys; sys.stderr = None; sys.__stderr__ = None; {_SERVER_MAIN}",
        _server_env(mcp_subprocess_env),
    )
    _assert_only_jsonrpc(stdout, stderr)


def test_the_subprocess_under_test_is_this_checkout(
    mcp_subprocess_env: dict[str, str],
) -> None:
    """Pins the ``PYTHONPATH`` guard in :func:`_server_env`.

    Without it the two tests above spawn the venv's editable install, which
    in a git worktree is a *different* checkout — they would pass or fail on
    code this branch does not contain, silently. This asserts the child
    imports the same ``trellis`` the test session did.
    """
    assert_subprocess_imports_this_checkout(_server_env(mcp_subprocess_env))
