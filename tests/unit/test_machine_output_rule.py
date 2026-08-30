"""Enforcement for the machine-output rule (#403).

``CLAUDE.md`` states it as a hard rule:

    Use ``--format json`` for machine output. All CLI commands support it.
    Parse JSON output, not human-readable text.

That was false for several commands, and silently. ``rich.console.Console``
does two things to a printed string:

* **Emoji substitution.** ``:name:`` shortcodes are replaced, so the seeded
  id ``corpus:notes:doc0`` came back as ``corpus`` + a musical-notes emoji +
  ``doc0``. The JSON still parses, so no consumer errors — it just holds a
  document id that exists in no store. Trellis ids are colon-delimited by
  construction, and ``notes``, ``book``, ``art``, ``key``, ``link``,
  ``memo``, ``zap``, ``warning`` and ``x`` are all live emoji names.
* **Line wrapping at the console width.** A newline folded into a JSON
  string literal is an invalid control character. Because the width comes
  from ``COLUMNS`` or the tty, the same command parses in one terminal and
  fails in another, and neither result is reproducible from the other.

The fix is one door — :func:`trellis_cli.output.emit_json` /
:func:`~trellis_cli.output.emit_machine_text`, both of which write straight
to ``sys.stdout``. ``emit_json`` already existed and was used on two paths of
``retrieve.py`` while three ``console.print`` calls sat beside them, which
is the tell that a narrow fix does not hold: the rule needs an enforcer,
not a convention. Same reasoning as ``test_chunk_visibility_rule.py``.

**What is enforced, and what is not.** The scan rejects a Rich ``print``
whose argument is a serialized payload — a direct ``json.dumps(...)`` call,
or a bare name bound from ``json.dumps`` / ``format_output`` in the same
module. It does *not* try to prove that every ``--format json`` branch
reaches an emitter; that is a judgement at the call site. Interpolating
``json.dumps`` into human prose (``console.print(f"  proposed: {...}")`` in
``metrics.py``) is deliberately allowed: it is the text branch, wrapping is
correct there, and forbidding it would buy nothing.
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from trellis_cli.output import emit_json, emit_machine_text, format_output

#: Names whose return value is a serialized machine payload. A bare
#: ``console.print(payload)`` where ``payload`` came from one of these is
#: the exact shape of the reported defect (``retrieve.py`` had three).
_SERIALIZERS = frozenset({"dumps", "format_output"})

#: Values that survive JSON but not Rich. ``:notes:`` is the one that
#: actually bit — it is a real corpus ``--source-system`` and a real emoji
#: name at the same time. ``:100:`` and ``:x:`` are included so the test
#: does not pass merely because one shortcode left Rich's table.
_EMOJI_TRAP_PAYLOAD = {
    "doc_id": "corpus:notes:b8b2ecbef8b88feb0a4a74092fd6fd30dedf1768",
    "trace_id": "trace:book:0001",
    "note": "a :100: b :x: c",
    "long": "x" * 400,
}


def _cli_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src" / "trellis_cli"
    assert root.is_dir(), f"trellis_cli not found at {root}"
    return root


def _is_rich_print(node: ast.Call) -> bool:
    """Does *node* look like ``<something>.print(...)``?

    Attribute name only, and deliberately broad: the CLI reaches Rich
    through ``console``, ``err_console``, ``out`` (the injected target in
    ``analyze._render_advisory_degradation``) and bare ``Console().print``.
    Matching on the receiver's name would miss the injected ones, which is
    where a machine payload is most likely to end up by accident.
    """
    return isinstance(node.func, ast.Attribute) and node.func.attr == "print"


def _serialized_names(tree: ast.Module) -> set[str]:
    """Names bound anywhere in the module from a serializer call."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, (ast.Attribute, ast.Name))
            and (
                getattr(value.func, "attr", None) in _SERIALIZERS
                or getattr(value.func, "id", None) in _SERIALIZERS
            )
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _violations() -> list[str]:
    """Rich ``print`` calls whose argument is a serialized payload."""
    found: list[str] = []
    for py_file in sorted(_cli_root().rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        serialized = _serialized_names(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_rich_print(node)):
                continue
            for arg in node.args:
                direct = (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "dumps"
                )
                indirect = isinstance(arg, ast.Name) and arg.id in serialized
                if direct or indirect:
                    found.append(
                        f"{py_file.relative_to(_cli_root().parent.parent)}"
                        f":{node.lineno}: {ast.unparse(node)[:90]}"
                    )
    return found


def test_no_rich_print_carries_a_serialized_payload() -> None:
    """The rule. Route it through ``emit_json`` / ``emit_machine_text``."""
    violations = _violations()
    assert not violations, (
        "Rich corrupts machine output (#403): it substitutes ``:name:`` emoji "
        "shortcodes and wraps at the console width. Use "
        "``trellis_cli.output.emit_json`` for an object, or "
        "``emit_machine_text`` for an already-serialized payload.\n  "
        + "\n  ".join(violations)
    )


def test_the_scan_finds_the_print_calls_it_is_meant_to_police() -> None:
    """Guard against the enforcement quietly matching nothing.

    A structural test that stops finding call sites — because ``console``
    was renamed, the package moved, or Rich was swapped out — would keep
    passing while enforcing nothing. This asserts the scan still sees a
    substantial population of Rich ``print`` calls to reason about.
    """
    total = 0
    for py_file in sorted(_cli_root().rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        total += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_rich_print(node)
        )
    assert total > 100, (
        f"only {total} Rich print calls found in trellis_cli; the scan has "
        f"probably drifted and is no longer policing anything"
    )


def test_the_scan_would_catch_the_defect_it_was_written_for() -> None:
    """Mutation guard: the reported code shape must be reported.

    Without this, a scan that silently matched nothing would pass both
    tests above — ``test_no_rich_print...`` vacuously, and the population
    guard on unrelated calls.
    """
    tree = ast.parse(
        "import json\n"
        "console.print(json.dumps({'a': 1}))\n"
        "payload = json.dumps({'b': 2})\n"
        "console.print(payload)\n"
        "console.print(f'prose {json.dumps(x)}')\n"
    )
    serialized = _serialized_names(tree)
    assert serialized == {"payload"}

    flagged = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_rich_print(node)
        and any(
            (
                isinstance(a, ast.Call)
                and isinstance(a.func, ast.Attribute)
                and a.func.attr == "dumps"
            )
            or (isinstance(a, ast.Name) and a.id in serialized)
            for a in node.args
        )
    ]
    # Lines 2 and 4 are violations; line 5 (prose f-string) is allowed.
    assert flagged == [2, 4]


class TestEmitterFidelity:
    """The emitters must be byte-transparent where Rich is not."""

    def test_emit_json_round_trips_emoji_shortcodes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        emit_json(_EMOJI_TRAP_PAYLOAD)
        assert json.loads(capsys.readouterr().out) == _EMOJI_TRAP_PAYLOAD

    def test_emit_machine_text_is_byte_identical(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        text = json.dumps(_EMOJI_TRAP_PAYLOAD)
        emit_machine_text(text)
        assert capsys.readouterr().out == text + "\n"

    def test_emit_machine_text_preserves_what_typer_echo_would_strip(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The reason ``emit_machine_text`` is not ``typer.echo``.

        ``click.echo`` strips ANSI escape sequences when the destination is
        not a tty. JSON never carries a raw escape byte — ``json.dumps``
        renders it ``\\u001b`` — but ``format_output``'s ``tsv`` branch
        stringifies cells unescaped, so on that path the stripping is a
        silent edit to the operator's data. Asserting both halves keeps the
        docstring's claim checkable rather than folklore.
        """
        import typer

        text = "c1\tc2\nval\x1b[31mred\x1b[0m\tv2"

        emit_machine_text(text)
        assert capsys.readouterr().out == text + "\n"

        typer.echo(text)
        assert capsys.readouterr().out != text + "\n", (
            "click.echo no longer strips ANSI; emit_machine_text could "
            "revert to typer.echo"
        )

    def test_format_output_results_survive_the_emitter(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``format_output`` feeds ``emit_machine_text`` on three commands."""
        emit_machine_text(
            format_output([_EMOJI_TRAP_PAYLOAD], "json", wrapper={"status": "ok"})
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["items"] == [_EMOJI_TRAP_PAYLOAD]


class TestErrorPathsAreAlsoMachineReadable:
    """``--format json`` must hold on the failure branch too.

    Found by sweeping every ``--format json`` surface against a real store
    rather than by reading code: four commands answered a missing input
    with Rich prose on **stdout**, so a consumer that did the documented
    thing — parse stdout — got a ``JSONDecodeError`` instead of a
    structured error it could act on. Each had a correct sibling a few
    lines away (``policy remove`` does it right, and ``ingest trace``
    already emitted JSON for a *parse* failure but not a *missing file*),
    which is the same "narrow fix, unenforced rule" shape as #403 itself.

    Exit codes are asserted alongside, because the fix must not turn a
    failure into a silent success.
    """

    @staticmethod
    def _json_stdout(result: object) -> dict:
        stdout = result.stdout  # type: ignore[attr-defined]
        assert stdout.strip(), "command produced no stdout at all"
        return json.loads(stdout)

    def test_ingest_trace_missing_file(self, cli_runner, tmp_path: Path) -> None:
        from trellis_cli.main import app

        result = cli_runner.invoke(
            app,
            ["ingest", "trace", str(tmp_path / "nope.json"), "--format", "json"],
        )
        assert result.exit_code != 0
        assert self._json_stdout(result)["status"] == "error"

    def test_ingest_evidence_missing_file(self, cli_runner, tmp_path: Path) -> None:
        from trellis_cli.main import app

        result = cli_runner.invoke(
            app,
            ["ingest", "evidence", str(tmp_path / "nope.json"), "--format", "json"],
        )
        assert result.exit_code != 0
        assert self._json_stdout(result)["status"] == "error"

    def test_ingest_dbt_manifest_missing_path(self, cli_runner, tmp_path: Path) -> None:
        from trellis_cli.main import app

        result = cli_runner.invoke(
            app,
            ["ingest", "dbt-manifest", str(tmp_path / "nope"), "--format", "json"],
        )
        assert result.exit_code != 0
        assert self._json_stdout(result)["status"] == "error"

    def test_ingest_openlineage_missing_file(self, cli_runner, tmp_path: Path) -> None:
        from trellis_cli.main import app

        result = cli_runner.invoke(
            app,
            ["ingest", "openlineage", str(tmp_path / "nope.json"), "--format", "json"],
        )
        assert result.exit_code != 0
        assert self._json_stdout(result)["status"] == "error"

    def test_policy_show_missing_policy(
        self, cli_runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis_cli.main import app

        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        (tmp_path / "data" / "stores").mkdir(parents=True)

        result = cli_runner.invoke(
            app, ["policy", "show", "no-such-policy", "--format", "json"]
        )
        assert result.exit_code != 0
        assert self._json_stdout(result)["status"] == "error"


class TestRichCorruptionIsReal:
    """Pins the behaviour the whole rule is a response to."""

    @pytest.mark.parametrize("width", [40, 80, 100, 200])
    def test_rich_would_have_corrupted_the_same_payload(self, width: int) -> None:
        """Pins the defect, so the tests above are not asserting a tautology.

        If Rich ever stops substituting emoji and wrapping, this fails and
        the rule can be revisited on evidence rather than assumed. Both
        failure modes are asserted because they are independent: at some
        widths the payload parses with wrong values, at others it does not
        parse at all.
        """
        buf = io.StringIO()
        Console(file=buf, force_terminal=False, width=width).print(
            json.dumps(_EMOJI_TRAP_PAYLOAD)
        )
        rendered = buf.getvalue()

        assert "corpus:notes:" not in rendered, (
            "Rich no longer substitutes :notes:; re-evaluate the rule"
        )
        try:
            decoded = json.loads(rendered)
        except json.JSONDecodeError:
            return  # wrapped into unparseable JSON — the loud failure mode
        assert decoded != _EMOJI_TRAP_PAYLOAD, (
            "Rich output parsed AND matched the payload; the corruption this "
            "rule exists for did not occur at this width"
        )
