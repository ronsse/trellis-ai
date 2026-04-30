"""``trellis serve`` boot test — proves the wheel can launch the API.

The unit suite never spawns ``trellis serve`` end-to-end; this is the
first test that validates the entry point boots, listens on the
requested port, and serves ``/healthz`` with a 200. Running the CLI
binary (rather than ``python -m uvicorn``) catches breakage in the
console-script entry that the API smoke matrix can't see — entry
points, structlog config wiring at startup, ``--config-dir`` plumbing.

Skipped when ``trellis`` isn't on ``PATH``. Runs against the same
SQLite tmp_path layout as the rest of the CLI suite, so no live infra
needed.
"""

from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

import httpx

_HEALTHZ_TIMEOUT_SECONDS = 30.0
_HEALTHZ_POLL_INTERVAL_SECONDS = 0.25
_TEARDOWN_TIMEOUT_SECONDS = 5.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_serve_boots_and_responds_to_healthz(
    trellis_bin: str,
    initialized_cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """``trellis serve --port <free>`` boots and serves ``/healthz``.

    The fixture has already run ``admin init`` against the same
    config dir, so the registry has its SQLite store files in place
    when the API lifespan validates them. This is the first test that
    proves the wheel's ``trellis serve`` entry point works end-to-end
    — every other test goes through ``python -m uvicorn``.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [
            trellis_bin,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--config-dir",
            initialized_cli_env["TRELLIS_CONFIG_DIR"],
        ],
        env=initialized_cli_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        deadline = time.monotonic() + _HEALTHZ_TIMEOUT_SECONDS
        last_err: BaseException | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                msg = (
                    f"`trellis serve` exited with {proc.returncode} "
                    f"before serving:\n"
                    f"stdout: {stdout.decode(errors='replace')}\n"
                    f"stderr: {stderr.decode(errors='replace')}"
                )
                raise AssertionError(msg)
            try:
                resp = httpx.get(f"{base_url}/healthz", timeout=2.0)
                if resp.status_code == 200:
                    body = resp.json()
                    assert body.get("status") == "ok"
                    return
            except httpx.HTTPError as exc:
                last_err = exc
            time.sleep(_HEALTHZ_POLL_INTERVAL_SECONDS)

        # Hung — surface whatever startup output we captured.
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(
                timeout=_TEARDOWN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(
                timeout=_TEARDOWN_TIMEOUT_SECONDS,
            )
        msg = (
            f"`trellis serve` never responded to /healthz within "
            f"{_HEALTHZ_TIMEOUT_SECONDS}s (last error: {last_err}):\n"
            f"stdout: {stdout.decode(errors='replace')}\n"
            f"stderr: {stderr.decode(errors='replace')}"
        )
        raise AssertionError(msg)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_TEARDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_TEARDOWN_TIMEOUT_SECONDS)
