"""``trellis-mcp`` prints a typed startup error as one line and exits 5.

A bad ``TRELLIS_MCP_TRANSPORT`` raised ``ConfigError`` out of ``main()`` and the
interpreter printed a traceback with exit 1, where the CLI prints
``CONFIG_ERROR`` and exits 5 for the same class. ``main`` is the console
script, so it is its own boundary: stdout belongs to the stdio protocol, so
the line goes to stderr.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trellis.mcp import server
from trellis_cli.exit_codes import EXIT_STORE


def test_exit_code_is_the_one_the_cli_gives_a_config_error() -> None:
    assert server._EXIT_TRELLIS_ERROR == EXIT_STORE


def test_a_bad_transport_is_one_stderr_line_and_exit_five(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRELLIS_MCP_TRANSPORT", "carrier-pigeon")
    run = MagicMock()
    monkeypatch.setattr(server.mcp, "run", run)

    with pytest.raises(SystemExit) as exc_info:
        server.main()

    assert exc_info.value.code == EXIT_STORE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "TRELLIS_MCP_TRANSPORT" in captured.err
    assert "Traceback" not in captured.err
    run.assert_not_called()


def test_an_untyped_failure_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the typed family is dressed as an operator error."""
    monkeypatch.delenv("TRELLIS_MCP_TRANSPORT", raising=False)
    monkeypatch.setattr(server.mcp, "run", MagicMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        server.main()
