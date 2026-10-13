"""The fail-closed REST bind check (#189) stops both serve commands.

``trellis serve`` and ``trellis admin serve`` both delegate to
``trellis_api.app.main``, which runs ``check_bind_is_safe`` before
``uvicorn.run``. These tests exercise that boundary at the CLI seam:
a refused bind must exit non-zero *without* ever reaching uvicorn, and
every other combination must reach it. ``uvicorn.run`` is patched out
everywhere so no test actually binds a socket.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from trellis_cli.exit_codes import EXIT_STORE
from trellis_cli.main import app

runner = CliRunner()


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
