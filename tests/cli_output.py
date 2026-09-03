"""Reading a Rich-rendered CLI surface as the operator's eye reads it.

Rich emits SGR escapes whenever it believes the stream is colour-capable,
and its highlighter styles *parts* of a token: a rendered option name, a
count or a path arrives as several separately-wrapped runs, so
``"--include-chunks" in result.output`` is ``False`` against output that
plainly displays ``--include-chunks``. A CLI test that reads
``result.output`` directly is therefore reading decorated output as if it
were plain text, and its outcome depends on whether the run happened to
be coloured — which CI's is and a local ``make test`` is not (#495, and
the environment-drift axis #398 tracks).

Stripping is :func:`click.utils.strip_ansi` rather than a local regex:
click owns ``CliRunner``, already strips these sequences in
``click.echo`` (pinned by
``tests/unit/test_machine_output_rule.py::test_emit_machine_text_preserves_what_typer_echo_would_strip``),
so it is the library's own solution rather than a fourth in-repo
spelling of a solved problem.

This module is deliberately **not** a ``conftest`` fixture that turns
colour on for the whole CLI package. Whether ``FORCE_COLOR=1`` becomes a
standing default or a CI matrix leg is a #398 decision; what lives here
is the vocabulary a test needs to be correct under *either* answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from click.utils import strip_ansi
from rich.console import Console

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

__all__ = ["assert_coloured", "force_colour", "plain"]


def plain(text: str) -> str:
    """``text`` with SGR escapes removed — the sentence, not the styling.

    Use this for every substring assertion against a Rich-rendered
    surface, including negative ones: ``"TRUNCATED" not in output`` is
    *weaker* on coloured text, because a highlighted token can be split
    across escape runs and satisfy the negative for the wrong reason.
    """
    return strip_ansi(text)


def force_colour(monkeypatch: pytest.MonkeyPatch, *cli_modules: ModuleType) -> None:
    """Make Rich colour ``cli_modules``' output whatever the ambient run does.

    A test asserting that something *survives* colouring has to guarantee
    the colouring, or it silently degrades into a test of the plain path
    on any run that is not coloured — which is the whole defect #495
    describes. ``CliRunner(color=True)`` does not do it: Rich writes to
    ``sys.stdout`` itself and consults the environment, not click's flag.

    Setting ``FORCE_COLOR`` alone is not enough either, and the reason is
    worth stating because it is not the obvious one.
    ``Console.is_terminal`` *does* read the environment live — but
    ``Console.__init__`` calls ``_detect_color_system()`` once and caches
    the answer, and every ``trellis_cli`` module builds its ``Console()``
    at **import** time. Under a plain run that cache is already ``None``
    by the time any test body executes, so a test-scoped env var reaches
    a console that has decided not to colour. Each module under test
    therefore gets a freshly-built console; ``monkeypatch`` restores the
    original on teardown, and ``setattr`` raises if a module does not
    have one, so a rename cannot silently turn colour off again.

    The env vars are still set, for the consoles a command constructs
    *during* the call (``trellis_cli.stores`` builds one per warning).
    ``TTY_COMPATIBLE`` is checked *before* ``FORCE_COLOR`` and
    ``NO_COLOR`` suppresses styling downstream of both, so neither is
    trusted to be unset.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TTY_COMPATIBLE", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    for module in cli_modules:
        monkeypatch.setattr(module, "console", Console(force_terminal=True))


def assert_coloured(text: str) -> str:
    """Assert Rich really coloured ``text``, then return it stripped.

    The guard that makes a markup-survival test mean something. Stripping
    alone passes on a build where Rich declined to colour at all, which
    pins nothing — the assertion would hold against output that never went
    near the code path it claims to police.
    """
    stripped = plain(text)
    assert stripped != text, (
        "Rich emitted no SGR escapes, so this run exercised the *plain* "
        "renderer and the stripping below pins nothing about the coloured "
        "one. Call force_colour(monkeypatch, <the cli module that prints "
        "this>) before invoking the command."
    )
    return stripped
