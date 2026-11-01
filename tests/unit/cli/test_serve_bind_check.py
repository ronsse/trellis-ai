"""The fail-closed REST bind check (#189) stops both serve commands.

``trellis serve`` and ``trellis admin serve`` both delegate to
``trellis_api.app.main``, which runs ``check_bind_is_safe`` before
``uvicorn.run``. These tests exercise that boundary at the CLI seam:
a refused bind must exit non-zero *without* ever reaching uvicorn, and
every other combination must reach it. ``uvicorn.run`` is patched out
everywhere so no test actually binds a socket.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

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
    them. Only ``admin serve`` is covered here: ``trellis serve`` resolves
    its defaults when the module is imported, so a variable set inside a
    test never reaches it. Every case runs ``uvicorn.run`` as a mock, so
    nothing binds, and the bind check still has to run on the env value.
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
        assert "127.0.0.2:18731" in result.output

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

    def test_unparseable_env_port_is_a_usage_error_not_a_bind(
        self, monkeypatch: pytest.MonkeyPatch, fake_uvicorn_run: MagicMock
    ) -> None:
        monkeypatch.setenv("TRELLIS_API_PORT", "not-a-port")
        result = runner.invoke(app, ["admin", "serve"])
        assert result.exit_code == 2, result.output
        fake_uvicorn_run.assert_not_called()
