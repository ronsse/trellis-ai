"""``tests.cli_output`` is itself a guard, so its own failure is pinned here.

:func:`~tests.cli_output.assert_coloured` is the load-bearing half of the
four markup-survival tests: stripping escapes proves nothing on a build
where Rich never coloured, so the guard is what makes those tests
statements about the *coloured* renderer rather than a second run of the
plain one.

Nothing in ``tests/unit/cli/`` can notice it breaking. ``force_colour``
runs first in every one of those tests, so the guard's subject is always
present and a guard that never fires is indistinguishable from one that
works — measured, before this file existed: rewriting ``assert_coloured``
to ``return plain(text)`` left all 580 tests in ``tests/unit/cli/`` green.
That is the shape #457, #464, #466, #488 and #495 itself keep producing —
a check whose *failure* mode is untested — and the repo's own convention
already answers it: ``tests/ast_rules.py`` has
``tests/unit/test_ast_rules.py`` and ``tests/unreadable_paths.py`` has
``tests/unit/test_unreadable_path_shapes.py``.

The mechanism tests below use a synthetic module rather than a real
``trellis_cli`` one, so that a failure here is about the helper and not
about whichever command happened to be borrowed. The real modules are
covered where it matters: ``monkeypatch.setattr`` raises on a module with
no ``console``, which is verified directly.
"""

from __future__ import annotations

import io
import os
from types import ModuleType

import pytest
from rich.console import Console

from tests.cli_output import assert_coloured, force_colour, plain


class _BodyFailedError(RuntimeError):
    """Raised by a test body that is meant to fail, to prove teardown ran."""


def _plain_console_output(text: str) -> str:
    """Render ``text`` through a Console that has decided not to colour."""
    buffer = io.StringIO()
    Console(file=buffer, width=200, color_system=None).print(text)
    return buffer.getvalue()


def _coloured_console_output(text: str) -> str:
    buffer = io.StringIO()
    Console(file=buffer, width=200, force_terminal=True).print(text)
    return buffer.getvalue()


def _stub_cli_module(name: str = "_stub_cli") -> ModuleType:
    """A module shaped like a ``trellis_cli`` one: a module-level console.

    Built with ``force_terminal=False`` for the same reason the real ones
    end up that way under a piped test run — ``_detect_color_system`` is
    called once in ``__init__`` and caches ``None``.
    """
    module = ModuleType(name)
    module.console = Console(file=io.StringIO(), force_terminal=False)  # type: ignore[attr-defined]
    return module


class TestPlain:
    def test_strips_sgr_escapes_from_real_rich_output(self) -> None:
        rendered = _coloured_console_output("count: 5000")
        assert "\x1b[" in rendered, "precondition: Rich coloured this"
        assert "count: 5000" in plain(rendered)

    def test_is_identity_on_undecorated_text(self) -> None:
        """The other half: stripping must not *remove* content.

        An over-eager strip would satisfy every positive assertion in the
        suite by deleting the escapes and a little either side of them,
        and the failures would land far from here.
        """
        text = "  reconcile: 0 noop  0 supersede\nrepaired 1 of 1 scanned\n"
        assert plain(text) == text

    def test_rejoins_a_token_split_across_escape_runs(self) -> None:
        """The exact defect #495 is about, and the reason negatives strip too.

        Rich styles *parts* of a token, so the raw output does not contain
        the option name it plainly displays — which makes ``in`` fail and,
        worse, makes ``not in`` *pass* for a reason that has nothing to do
        with the property under test.
        """
        split = "\x1b[1;36m-\x1b[0m\x1b[1;36m-include\x1b[0m\x1b[1;36m-chunks\x1b[0m"
        assert "--include-chunks" not in split
        assert "--include-chunks" in plain(split)


class TestAssertColoured:
    def test_raises_when_rich_emitted_no_escapes(self) -> None:
        """The assertion this whole helper exists to make.

        Against output from a console that declined to colour — which is
        what a piped ``make test`` produces and what every ``trellis_cli``
        module caches at import — the guard must fail rather than hand
        back a string that pins nothing.
        """
        rendered = _plain_console_output("To reset: mv /tmp/a /tmp/a.corrupt")
        assert "\x1b[" not in rendered, "precondition: this run is not coloured"
        with pytest.raises(AssertionError):
            assert_coloured(rendered)

    def test_returns_the_stripped_text_when_colour_happened(self) -> None:
        rendered = _coloured_console_output("To reset: mv /tmp/a /tmp/a.corrupt")
        assert assert_coloured(rendered) == plain(rendered)
        assert "\x1b[" not in assert_coloured(rendered)

    def test_the_failure_names_the_remedy(self) -> None:
        """A guard whose message does not say what to do gets deleted.

        The failure is not "the code under test is wrong" — it is "this
        test forgot ``force_colour``", and the two send a reader in
        opposite directions.
        """
        with pytest.raises(AssertionError, match="force_colour"):
            assert_coloured("no escapes here")


class TestForceColour:
    def test_makes_the_module_console_emit_escapes(self) -> None:
        module = _stub_cli_module()
        buffer = io.StringIO()
        before = io.StringIO()
        module.console.file = before  # type: ignore[attr-defined]
        module.console.print("[bold]mv[/bold] /tmp/a")  # type: ignore[attr-defined]
        assert "\x1b[" not in before.getvalue(), "precondition: plain to start"

        with pytest.MonkeyPatch.context() as monkeypatch:
            force_colour(monkeypatch, module)
            module.console.file = buffer  # type: ignore[attr-defined]
            module.console.print("[bold]mv[/bold] /tmp/a")  # type: ignore[attr-defined]
        assert "\x1b[" in buffer.getvalue()

    def test_setting_the_env_var_alone_would_not_have_worked(self) -> None:
        """Pins the mechanism the helper's docstring claims.

        ``Console.is_terminal`` reads the environment live, but
        ``Console.__init__`` calls ``_detect_color_system()`` once and
        caches the answer — so a test-scoped ``FORCE_COLOR`` reaches a
        console that has already decided not to colour. If a future
        ``rich`` made the colour system live too, this fails and the
        docstring's reasoning (and the rebuild it justifies) is stale.
        """
        buffer = io.StringIO()
        with pytest.MonkeyPatch.context() as monkeypatch:
            # The ambient run may itself be coloured (the CI leg #398 is
            # about, and the FORCE_COLOR=1 arm of this fix's own report),
            # so the "piped" starting state is constructed, not assumed.
            monkeypatch.delenv("FORCE_COLOR", raising=False)
            monkeypatch.delenv("TTY_COMPATIBLE", raising=False)
            console = Console(file=buffer, width=200)
            assert console._color_system is None, "precondition: piped run"
            monkeypatch.setenv("FORCE_COLOR", "1")
            assert console.is_terminal is True
            console.print("[bold]mv[/bold] /tmp/a")
        assert "\x1b[" not in buffer.getvalue()

    def test_restores_the_original_console_on_teardown(self) -> None:
        module = _stub_cli_module()
        original = module.console  # type: ignore[attr-defined]
        with pytest.MonkeyPatch.context() as monkeypatch:
            force_colour(monkeypatch, module)
            assert module.console is not original  # type: ignore[attr-defined]
        assert module.console is original  # type: ignore[attr-defined]

    def test_restores_the_original_console_when_the_test_body_fails(self) -> None:
        """Teardown, not the happy path — a leaked coloured console would
        contaminate every later test in the process, and the four callers
        are all tests that exist to fail loudly."""
        module = _stub_cli_module()
        original = module.console  # type: ignore[attr-defined]

        def _failing_body() -> None:
            with pytest.MonkeyPatch.context() as monkeypatch:
                force_colour(monkeypatch, module)
                raise _BodyFailedError

        with pytest.raises(_BodyFailedError):
            _failing_body()
        assert module.console is original  # type: ignore[attr-defined]

    def test_restores_the_environment_too(self) -> None:
        before = {
            name: os.environ.get(name)
            for name in ("FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE")
        }
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("NO_COLOR", "1")
            monkeypatch.setenv("TTY_COMPATIBLE", "0")
            force_colour(monkeypatch, _stub_cli_module())
            assert os.environ["FORCE_COLOR"] == "1"
            assert "NO_COLOR" not in os.environ
            assert "TTY_COMPATIBLE" not in os.environ
        assert {
            name: os.environ.get(name)
            for name in ("FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE")
        } == before

    def test_a_renamed_module_console_is_loud_not_silent(self) -> None:
        """The failure direction that matters.

        A module that renamed its ``console`` must break the test that
        forces colour on it, not quietly leave the test running against
        the plain renderer under a name that says otherwise.
        """
        module = ModuleType("_stub_cli_without_console")
        with pytest.MonkeyPatch.context() as monkeypatch, pytest.raises(AttributeError):
            force_colour(monkeypatch, module)

    def test_every_module_the_survival_tests_force_still_has_one(self) -> None:
        """``setattr`` is only loud if these keep the attribute it names."""
        from trellis_cli import analyze, curate, policy, worker

        for module in (analyze, curate, policy, worker):
            assert isinstance(module.console, Console), module.__name__
