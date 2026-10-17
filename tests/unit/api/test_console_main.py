"""``trellis-api`` console-script error surface (#189 follow-up).

``trellis_api.app.main`` is called directly by ``trellis serve`` /
``trellis admin serve``, which rely on a ``TrellisError`` propagating out
of it uncaught so ``trellis_cli``'s own Typer boundary can render and
exit it. The bare ``trellis-api`` console script (``pyproject.toml``'s
``trellis-api = "trellis_api.app:console_main"``) has no such boundary,
so without its own wrapper a refused bind surfaced as a raw traceback
instead of a one-line error and a scriptable exit code. These tests
exercise :func:`trellis_api.app.console_main` in isolation — no uvicorn,
no socket.
"""

from __future__ import annotations

import pytest

from trellis.errors import ConfigError, ValidationError
from trellis_api.app import console_main
from trellis_cli.exit_codes import EXIT_STORE, EXIT_VALIDATION


def test_refused_bind_prints_one_line_and_exits_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> None:
        msg = "Refusing to bind 0.0.0.0:8420"
        raise ConfigError(msg, setting="TRELLIS_AUTH_MODE")

    monkeypatch.setattr("trellis_api.app.main", _refuse)

    with pytest.raises(SystemExit) as exc_info:
        console_main()

    assert exc_info.value.code == EXIT_STORE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Refusing to bind 0.0.0.0:8420" in captured.err
    # No traceback — the wrapper caught it before Python's default handler did.
    assert "Traceback" not in captured.err


def test_other_trellis_errors_also_map_through_exit_code_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not hardcoded to ConfigError: any TrellisError exits consistently
    with the CLI boundary's own mapping."""

    def _raise_validation(*_args: object, **_kwargs: object) -> None:
        msg = "bad input"
        raise ValidationError(msg)

    monkeypatch.setattr("trellis_api.app.main", _raise_validation)

    with pytest.raises(SystemExit) as exc_info:
        console_main()

    assert exc_info.value.code == EXIT_VALIDATION
    assert "bad input" in capsys.readouterr().err


def test_successful_start_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("trellis_api.app.main", lambda: calls.append(True))

    console_main()  # must not raise

    assert calls == [True]
