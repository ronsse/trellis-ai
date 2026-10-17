"""A lone surrogate in CLI argv exits 2, not a traceback.

On Linux, Python decodes an argv byte that is not valid UTF-8 with
``surrogateescape`` into exactly one unpaired UTF-16 surrogate, so a
shell-passed id or value can legitimately contain one. Every CLI command
that submits a mutation builds a
:class:`~trellis.mutate.commands.Command` straight from that string, and
pydantic refuses a lone surrogate in a plain ``str`` field by raising its
*own* ``ValidationError`` — not :class:`~trellis.errors.ValidationError` —
before a ``CommandResult`` exists for the command's own rendering to
catch. Left uncaught that is a Typer traceback, exit ``1``, and nothing
parseable on stdout under ``--format json`` either.

``trellis_cli.main._BoundaryGroup.invoke`` is the one seam every command
in the tree dispatches through, so the fix sits there
(``_render_command_validation_failure``) rather than as a per-command
pre-check. Two commands are exercised below, each building its
``Command`` from a different module (``curate.py``, ``admin_settings.py``)
and a different field (``target_id``, ``name``), to show the seam catches
both without either naming the other.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from tests.cli_output import plain
from trellis_cli.exit_codes import EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()

#: Index 1 is an unpaired low surrogate — exactly what ``surrogateescape``
#: produces from a non-UTF-8 argv byte, and what pydantic's ``str``
#: validator refuses.
_SURROGATE_ID = "x\udcffy"

#: ``{case name: argv}``. Each builds a ``Command`` straight from
#: ``_SURROGATE_ID`` before any registry or store is touched, so neither
#: case needs stores configured.
_SITES: dict[str, list[str]] = {
    "curate redact": ["curate", "redact", _SURROGATE_ID, "--reason", "r", "--yes"],
    "admin settings set": ["admin", "settings", "set", _SURROGATE_ID, "v"],
}


def _assert_the_boundary_swallowed_it(result: object) -> None:
    """No exception escaped ``_BoundaryGroup.invoke``.

    ``typer.Exit`` leaves ``CliRunner`` a ``SystemExit``; anything else is
    the traceback this seam exists to prevent.
    """
    exc = result.exception  # type: ignore[attr-defined]
    assert exc is None or isinstance(exc, SystemExit), (
        f"the boundary let an exception escape: {exc!r}"
    )


def test_text_arm_exits_validation_with_no_traceback() -> None:
    for name, argv in _SITES.items():
        result = runner.invoke(app, argv)
        assert result.exit_code == EXIT_VALIDATION, f"{name}: {result.output}"
        _assert_the_boundary_swallowed_it(result)
        out = plain(result.output)
        assert "Traceback" not in out, f"{name}: {out}"
        assert "VALIDATION_ERROR" in out, f"{name}: {out}"


def test_json_arm_exits_the_same_code_and_stdout_parses() -> None:
    for name, argv in _SITES.items():
        result = runner.invoke(app, [*argv, "--format", "json"])
        assert result.exit_code == EXIT_VALIDATION, f"{name}: {result.output}"
        _assert_the_boundary_swallowed_it(result)
        payload = json.loads(result.stdout)
        assert payload["status"] == "error", f"{name}: {payload}"
        assert payload["error_type"] == "ValidationError", f"{name}: {payload}"
        # Never the raw value: pydantic's own str(exc) composes
        # ``input_value=<the caller's value>`` into every line, which is
        # the hazard this seam exists to keep out of the payload.
        assert "\udcff" not in result.stdout, f"{name}: {result.stdout!r}"


def test_the_two_format_arms_agree_about_the_exit_code() -> None:
    """Stated as its own case because it is the property, not a side effect."""
    for argv in _SITES.values():
        text = runner.invoke(app, argv)
        machine = runner.invoke(app, [*argv, "--format", "json"])
        assert text.exit_code == machine.exit_code == EXIT_VALIDATION
