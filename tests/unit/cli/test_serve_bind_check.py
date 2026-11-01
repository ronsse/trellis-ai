"""The fail-closed REST bind check (#189) stops both serve commands.

``trellis serve`` and ``trellis admin serve`` both delegate to
``trellis_api.app.main``, which runs ``check_bind_is_safe`` before
``uvicorn.run``. These tests exercise that boundary at the CLI seam:
a refused bind must exit non-zero *without* ever reaching uvicorn, and
every other combination must reach it. ``uvicorn.run`` is patched out
everywhere so no test actually binds a socket.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.integration._live_server import repo_src_pythonpath
from trellis_cli.exit_codes import EXIT_STORE
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` parses config.yaml before ``uvicorn.run``; never the developer's."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)
    monkeypatch.delenv("TRELLIS_API_ALLOW_INSECURE_BIND", raising=False)


@pytest.fixture
def fake_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the real ``uvicorn`` module's ``run`` — ``app.main`` imports
    it lazily by name, so patching the attribute on the module itself
    (rather than any local alias) is what every caller actually sees."""
    fake = MagicMock()
    monkeypatch.setattr("uvicorn.run", fake)
    return fake


@pytest.mark.parametrize("command", [["serve"], ["admin", "serve"]])
class TestServeBindCheck:
    def test_unparseable_config_is_a_config_error_not_a_traceback(
        self,
        tmp_path: Path,
        fake_uvicorn_run: MagicMock,
        command: list[str],
    ) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("stores: [unclosed\n  bad: : :\n")

        result = runner.invoke(app, [*command, "--port", "18999"])

        assert result.exit_code == EXIT_STORE, result.output
        assert "CONFIG_ERROR" in result.output
        assert "Could not parse Trellis config" in " ".join(result.output.split())
        assert "Traceback" not in result.output
        fake_uvicorn_run.assert_not_called()

    def test_non_loopback_with_auth_off_refuses_without_starting(
        self, fake_uvicorn_run: MagicMock, command: list[str]
    ) -> None:
        result = runner.invoke(
            app,
            [*command, "--host", "0.0.0.0", "--port", "8420"],  # noqa: S104
        )
        assert result.exit_code == EXIT_STORE, result.output
        fake_uvicorn_run.assert_not_called()

    def test_non_loopback_with_auth_required_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_uvicorn_run: MagicMock,
        command: list[str],
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        result = runner.invoke(
            app,
            [*command, "--host", "0.0.0.0", "--port", "8420"],  # noqa: S104
        )
        assert result.exit_code == 0, result.output
        fake_uvicorn_run.assert_called_once()

    def test_loopback_with_auth_off_starts(
        self, fake_uvicorn_run: MagicMock, command: list[str]
    ) -> None:
        result = runner.invoke(app, [*command, "--host", "127.0.0.1", "--port", "8420"])
        assert result.exit_code == 0, result.output
        fake_uvicorn_run.assert_called_once()

    def test_override_starts_despite_auth_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_uvicorn_run: MagicMock,
        command: list[str],
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_ALLOW_INSECURE_BIND", "1")
        result = runner.invoke(
            app,
            [*command, "--host", "0.0.0.0", "--port", "8420"],  # noqa: S104
        )
        assert result.exit_code == 0, result.output
        fake_uvicorn_run.assert_called_once()


class TestAdminServeEnvironment:
    """``admin serve`` reads ``TRELLIS_API_HOST`` / ``TRELLIS_API_PORT``.

    Its own ``--host`` help and the running-Trellis guide both say the
    variables apply, and ``trellis serve`` / ``trellis-api`` already honour
    them. ``TestServeEnvironment`` below covers the port for ``trellis
    serve``. Every case runs ``uvicorn.run`` as a mock, so nothing binds,
    and the bind check still has to run on the env value.
    """

    @pytest.fixture(autouse=True)
    def _clean_bind_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_API_HOST", raising=False)
        monkeypatch.delenv("TRELLIS_API_PORT", raising=False)

    @staticmethod
    def _bound(fake_uvicorn_run: MagicMock) -> tuple[str, int]:
        fake_uvicorn_run.assert_called_once()
        kwargs = fake_uvicorn_run.call_args.kwargs
        return kwargs["host"], kwargs["port"]

    def test_defaults_without_env_are_loopback_8420(
        self, fake_uvicorn_run: MagicMock
    ) -> None:
        result = runner.invoke(app, ["admin", "serve"])
        assert result.exit_code == 0, result.output
        assert self._bound(fake_uvicorn_run) == ("127.0.0.1", 8420)

    def test_env_host_and_port_reach_uvicorn(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_HOST", "127.0.0.2")
        monkeypatch.setenv("TRELLIS_API_PORT", "18731")
        result = runner.invoke(app, ["admin", "serve"])
        assert result.exit_code == 0, result.output
        assert self._bound(fake_uvicorn_run) == ("127.0.0.2", 18731)
        assert "127.0.0.2:18731" in plain(result.output)

    def test_flag_beats_env(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_HOST", "127.0.0.2")
        monkeypatch.setenv("TRELLIS_API_PORT", "18731")
        result = runner.invoke(
            app, ["admin", "serve", "--host", "127.0.0.1", "--port", "18732"]
        )
        assert result.exit_code == 0, result.output
        assert self._bound(fake_uvicorn_run) == ("127.0.0.1", 18732)

    def test_empty_env_is_the_loopback_default_never_a_wildcard(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_HOST", "")
        monkeypatch.setenv("TRELLIS_API_PORT", "")
        result = runner.invoke(app, ["admin", "serve"])
        assert result.exit_code == 0, result.output
        assert self._bound(fake_uvicorn_run) == ("127.0.0.1", 8420)

    def test_env_non_loopback_host_with_auth_off_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_HOST", "0.0.0.0")  # noqa: S104
        result = runner.invoke(app, ["admin", "serve"])
        assert result.exit_code == EXIT_STORE, result.output
        assert "Starting Trellis API server" not in result.output
        fake_uvicorn_run.assert_not_called()

    def test_env_non_loopback_host_with_auth_required_binds_it(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv("TRELLIS_API_HOST", "0.0.0.0")  # noqa: S104
        monkeypatch.setenv("TRELLIS_API_PORT", "18731")
        result = runner.invoke(app, ["admin", "serve"])
        assert result.exit_code == 0, result.output
        assert self._bound(fake_uvicorn_run) == ("0.0.0.0", 18731)  # noqa: S104

    @pytest.mark.parametrize("value", ["not-a-port", "80.5", "70000", "-1"])
    def test_a_bad_env_port_is_a_config_error_not_a_bind(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_uvicorn_run: MagicMock,
        value: str,
    ) -> None:
        """The same ``ConfigError`` and exit 5 as ``trellis serve``.

        Typer's ``envvar=`` parsed the variable as an ``int`` and exited 2 with
        a usage error, so the three server entry points disagreed about one
        bad value. ``admin serve`` now reads it through ``resolve_api_port``.
        """
        monkeypatch.setenv("TRELLIS_API_PORT", value)
        result = runner.invoke(app, ["admin", "serve"])
        assert result.exit_code == EXIT_STORE, result.output
        text = " ".join(plain(result.output).split())
        assert "CONFIG_ERROR" in text
        assert f"TRELLIS_API_PORT='{value}' is not a port number" in text
        assert "Starting Trellis API server" not in text
        assert "Traceback" not in result.output
        fake_uvicorn_run.assert_not_called()

    def test_flag_beats_a_bad_env_port(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_PORT", "not-a-port")
        result = runner.invoke(app, ["admin", "serve", "--port", "18732"])
        assert result.exit_code == 0, result.output
        assert self._bound(fake_uvicorn_run) == ("127.0.0.1", 18732)


class TestServeEnvironment:
    """``trellis serve`` and ``trellis-api`` read ``TRELLIS_API_PORT`` when run.

    The variable used to be an ``int()`` at import of ``trellis_api.app``,
    which ``trellis_cli.main`` imports for every command, so a value that was
    not a number crashed the whole CLI with a traceback and exit ``1``. Now it
    is resolved when a server starts, and a bad value is a ``ConfigError``:
    ``EXIT_STORE`` and one legible line. ``serve`` has no ``--format``, so
    the formats are covered on a command that does, below.
    """

    @pytest.fixture(autouse=True)
    def _clean_port_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_API_PORT", raising=False)

    def test_env_port_reaches_uvicorn(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_PORT", "18731")
        result = runner.invoke(app, ["serve"])
        assert result.exit_code == 0, result.output
        assert fake_uvicorn_run.call_args.kwargs["port"] == 18731

    def test_flag_beats_env_even_when_the_env_is_bad(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_PORT", "not-a-port")
        result = runner.invoke(app, ["serve", "--port", "18732"])
        assert result.exit_code == 0, result.output
        assert fake_uvicorn_run.call_args.kwargs["port"] == 18732

    def test_empty_env_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_PORT", "")
        result = runner.invoke(app, ["serve"])
        assert result.exit_code == 0, result.output
        assert fake_uvicorn_run.call_args.kwargs["port"] == 8420

    @pytest.mark.parametrize("value", ["not-a-port", "80.5", "70000", "-1"])
    def test_a_bad_env_port_is_a_config_error_not_a_bind(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_uvicorn_run: MagicMock,
        value: str,
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_PORT", value)
        result = runner.invoke(app, ["serve"])
        assert result.exit_code == EXIT_STORE, result.output
        text = " ".join(plain(result.output).split())
        assert "CONFIG_ERROR" in text
        assert f"TRELLIS_API_PORT='{value}' is not a port number" in text
        assert "Traceback" not in result.output
        fake_uvicorn_run.assert_not_called()

    def test_the_standalone_server_exits_the_same_way(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_uvicorn_run: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from trellis_api.app import console_main

        monkeypatch.setenv("TRELLIS_API_PORT", "not-a-port")
        with pytest.raises(SystemExit) as exited:
            console_main()
        assert exited.value.code == EXIT_STORE
        assert "is not a port number" in capsys.readouterr().err
        fake_uvicorn_run.assert_not_called()


class _Ran(NamedTuple):
    """What a ``trellis`` child process did, without the process object."""

    code: int
    out: str
    err: str


def _cli(tmp_path: Path, *args: str) -> _Ran:
    """Run ``trellis`` in a fresh interpreter with a bad ``TRELLIS_API_PORT``.

    A subprocess, because the defect was at import: an in-process run has
    long since imported the module with a good value. The result is a plain
    record, not the ``CompletedProcess``, so this helper is not mistaken for
    a builder of pinned environments by ``test_subprocess_pythonpath_rule``.
    """
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "from trellis_cli.main import app; app()", *args],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        env={
            **os.environ,
            "PYTHONPATH": repo_src_pythonpath(),
            "TRELLIS_API_PORT": "not-a-port",
            "TRELLIS_CONFIG_DIR": str(tmp_path / "config"),
            "TRELLIS_DATA_DIR": str(tmp_path / "data"),
        },
        check=False,
    )
    return _Ran(done.returncode, done.stdout, done.stderr)


def test_a_bad_env_port_ends_trellis_serve_at_exit_five(tmp_path: Path) -> None:
    done = _cli(tmp_path, "serve")

    assert done.code == EXIT_STORE, done.out + done.err
    assert "TRELLIS_API_PORT='not-a-port' is not a port number" in " ".join(
        plain(done.out).split()
    )
    assert "Traceback" not in done.out + done.err


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_a_bad_env_port_no_longer_breaks_commands_that_do_not_bind(
    tmp_path: Path, output_format: str
) -> None:
    """Both formats, one exit: the command does not read the port at all."""
    done = _cli(tmp_path, "admin", "write-config", "--format", output_format)

    assert done.code == 0, done.out + done.err
    assert "Traceback" not in done.err
