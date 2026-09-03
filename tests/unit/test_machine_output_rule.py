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

from tests.ast_rules import (
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    construction_names,
    name_of,
)
from trellis_cli.output import emit_json, emit_machine_text, format_output

#: Callables whose return value is a serialized machine payload. Matched on
#: the bare name, so ``json.dumps`` and ``from json import dumps`` both land.
#:
#: ``model_dump_json`` is here because leaving it out cost a real miss: in a
#: Pydantic codebase it is the obvious sibling of ``json.dumps``, and
#: ``retrieve trace --format json`` shipped a live #403 defect through it —
#: six lines below a call the same commit had fixed — because the first
#: version of this set named only ``dumps``. The rule's value is entirely in
#: how wide this set is, so add to it on sight rather than on evidence.
_SERIALIZERS = frozenset(
    {"dumps", "format_output", "model_dump_json", "json", "to_json"}
)

#: Rich methods that render markup. ``print_json`` is included even though no
#: call site uses it today: it is the single most plausible "fix" a future
#: reader reaches for, and it still routes through Rich's renderer.
_RICH_RENDER_METHODS = frozenset({"print", "print_json", "out", "log"})

#: Values that survive JSON but not Rich, one per corruption mode.
#:
#: ``:notes:`` is the one that actually bit — a real corpus
#: ``--source-system`` and a real emoji name at once. ``:100:`` and ``:x:``
#: are here so the test does not pass merely because one shortcode left
#: Rich's table.
#:
#: ``markup`` covers the mode the issue did not name and this branch nearly
#: shipped without: Rich reads ``[...]`` as a style tag and **deletes** it.
#: ``SELECT [col] FROM t`` renders as ``SELECT  FROM t``. That is the worst
#: of the three — it needs no shortcode collision, fires on any bracketed
#: token (SQL identifiers, markdown links, glob patterns), and the result
#: parses cleanly, so nothing anywhere raises. The knowledge was already in
#: this repo: ``analyze._render_advisory_degradation`` escapes every
#: interpolation for exactly this reason. It just had not reached here.
#:
#: ``long`` forces wrapping at any realistic console width.
_TRAP_PAYLOAD = {
    "doc_id": "corpus:notes:b8b2ecbef8b88feb0a4a74092fd6fd30dedf1768",
    "trace_id": "trace:book:0001",
    "note": "a :100: b :x: c",
    "markup": "SELECT [col] FROM t -- see [docs](http://x)",
    "long": "x" * 400,
}


class _RecordingStream:
    """Write-only stream that remembers its calls; ``capsys`` cannot see flushes."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, data: str) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        self.flushes += 1


def _render_through_rich(payload: dict, *, width: int) -> str:
    """What ``console.print(json.dumps(payload))`` would have emitted."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=width).print(json.dumps(payload))
    return buf.getvalue()


def _cli_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src" / "trellis_cli"
    assert root.is_dir(), f"trellis_cli not found at {root}"
    return root


def _is_rich_render(node: ast.Call) -> bool:
    """Does *node* look like ``<something>.print(...)`` or a Rich sibling?

    Attribute name only, and deliberately broad: the CLI reaches Rich
    through ``console``, ``err_console``, ``out`` (the injected target in
    ``analyze._render_advisory_degradation``) and bare ``Console().print``.
    Matching on the receiver's name would miss the injected ones, which is
    where a machine payload is most likely to end up by accident.
    """
    return (
        isinstance(node.func, ast.Attribute) and node.func.attr in _RICH_RENDER_METHODS
    )


def _serializer_names(tree: ast.Module) -> frozenset[str]:
    """:data:`_SERIALIZERS` plus every local name in *tree* that reaches one.

    ``from json import dumps as _dump``, ``_render = format_output`` and a
    ``BaseModel`` subclass all produce a serializer under a name a literal
    match never sees, and each is resolvable within the module — see
    :func:`tests.ast_rules.construction_names`, lifted from #488's own
    rule. The set is small and the resolution is per module, so this is
    cheap; the alternative is a blind spot that no test would have shown,
    because the corpus below never rendered one.
    """
    names: set[str] = set()
    for serializer in _SERIALIZERS:
        names |= construction_names(serializer, tree)
    return frozenset(names)


def _is_serializer_call(node: ast.AST, names: frozenset[str] = _SERIALIZERS) -> bool:
    """Is *node* a call to something that returns a serialized payload?

    Reads the bare name off either an attribute (``json.dumps``,
    ``result.model_dump_json``) or a plain call (``dumps`` imported
    directly, ``format_output``). Both forms matter — the attribute-only
    version of this predicate is what let ``model_dump_json`` through.

    The reading is :func:`tests.ast_rules.name_of`, shared with every other
    AST rule here. It used to be a pair of ``getattr`` lookups: correct,
    but a fourth independent spelling of the same sub-problem, and #488 was
    the site that needed it and got it wrong.
    ``test_ast_rules.TestNameOf.test_agrees_with_the_getattr_spelling_across_src``
    checks the two against every call in ``src/`` rather than reasoning
    about their equivalence.

    *names* defaults to the literal set so the predicate stays callable on
    a lone node; :func:`_violations` passes the resolved set.
    """
    return isinstance(node, ast.Call) and name_of(node.func) in names


def _serialized_names(tree: ast.Module, names: frozenset[str]) -> set[str]:
    """Names bound anywhere in the module from a serializer call.

    Covers plain assignment, annotated assignment (``payload: str = ...``)
    and the walrus, because all three produce the ``console.print(payload)``
    shape the rule exists to reject.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_serializer_call(node.value, names):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, (ast.AnnAssign, ast.NamedExpr))
            and node.value is not None
            and _is_serializer_call(node.value, names)
            and isinstance(node.target, ast.Name)
        ):
            bound.add(node.target.id)
    return bound


def _carries_payload(
    arg: ast.expr, serialized: set[str], names: frozenset[str] = _SERIALIZERS
) -> bool:
    """Is *arg* a serialized payload, directly or by name?

    Unwraps two shapes that would otherwise walk straight past the rule:

    * ``print(*[payload])`` — the star and a literal list/tuple around it.
    * ``print(f"{payload}")`` — an f-string whose *entire* content is one
      interpolation is the payload wearing a costume. One with any literal
      text around it is prose (``metrics.py`` interpolates ``json.dumps``
      into a human line in the text branch) and stays allowed, which is the
      distinction that keeps the rule from forbidding something harmless.
    """
    # ``print(name := json.dumps(x))`` — the binding is registered by
    # ``_serialized_names``, but the argument node here is the NamedExpr
    # itself, so it has to be unwrapped to be seen at all.
    if isinstance(arg, ast.NamedExpr):
        return _carries_payload(arg.value, serialized, names)

    if isinstance(arg, ast.Starred):
        inner = arg.value
        if isinstance(inner, (ast.List, ast.Tuple)):
            return any(_carries_payload(e, serialized, names) for e in inner.elts)
        return _carries_payload(inner, serialized, names)

    if isinstance(arg, ast.JoinedStr):
        parts = [
            v for v in arg.values if not (isinstance(v, ast.Constant) and v.value == "")
        ]
        if len(parts) == 1 and isinstance(parts[0], ast.FormattedValue):
            return _carries_payload(parts[0].value, serialized, names)
        return False

    return _is_serializer_call(arg, names) or (
        isinstance(arg, ast.Name) and arg.id in serialized
    )


def _violations(root: Path | None = None) -> list[str]:
    """Rich render calls whose argument is a serialized payload.

    *root* is injectable so :func:`test_the_scan_catches_every_known_evasion`
    can run **this** function over a synthetic tree. An earlier version of
    that guard re-implemented the predicate inline, which meant the scan
    could be stubbed out entirely and the suite stayed green — the guard was
    guarding a copy of itself.
    """
    cli_root = root if root is not None else _cli_root()
    found: list[str] = []
    for py_file in sorted(cli_root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        names = _serializer_names(tree)
        serialized = _serialized_names(tree, names)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_rich_render(node)):
                continue
            for arg in node.args:
                if _carries_payload(arg, serialized, names):
                    found.append(
                        f"{py_file.name}:{node.lineno}: {ast.unparse(node)[:90]}"
                    )
                    break
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


def _rich_render_population() -> int:
    total = 0
    for py_file in sorted(_cli_root().rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        total += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_rich_render(node)
        )
    return total


#: Hand-read off ``src/trellis_cli`` on 2026-09-03: 594 Rich render calls.
#: Floored well below that because the CLI's prose output is edited
#: constantly and a floor that tracks the tree is a floor nobody re-reads —
#: what it has to catch is the scan going to near-zero, not a refactor
#: trimming a hundred prints.
_RICH_RENDER_FLOOR = 100


def test_the_scan_finds_the_print_calls_it_is_meant_to_police() -> None:
    """Guard against the enforcement quietly matching nothing.

    A structural test that stops finding call sites — because ``console``
    was renamed, the package moved, or Rich was swapped out — would keep
    passing while enforcing nothing. This asserts the scan still sees a
    substantial population of Rich ``print`` calls to reason about.
    """
    assert_hand_read_floor(
        _rich_render_population(),
        _RICH_RENDER_FLOOR,
        subject="Rich render call in trellis_cli",
        hint="console/err_console .print, .print_json, .out and .log.",
    )


#: Every shape a reviewer found that walked past the first version of this
#: rule, plus the control. The comment on each line is what made it invisible.
_EVASIONS = """
import json
from json import dumps
from trellis_cli.output import format_output

console.print(json.dumps(a))                  # 5  the control
payload = json.dumps(b)
console.print(payload)                        # 7  bound name
console.print(result.model_dump_json())       # 8  pydantic sibling -> was LIVE
console.print(format_output(items, "json"))   # 9  serializer, called directly
console.print(dumps(c))                       # 10 imported bare, no attribute
console.print_json(json.dumps(d))             # 11 rich's own json printer
annotated: str = json.dumps(e)
console.print(annotated)                      # 13 annotated assignment
console.print(walrus := json.dumps(f))        # 14 walrus
console.print(*[payload])                     # 15 starred literal
console.print(f"{payload}")                   # 16 f-string that IS the payload
console.print(f"proposed: {json.dumps(g)}")   # 17 ALLOWED: prose, text branch
console.print("plain text")                   # 18 ALLOWED
"""

#: Line numbers in :data:`_EVASIONS` the rule must report. 17 and 18 are
#: deliberately absent — ``metrics.py`` interpolates a payload into a human
#: line in its text branch, and forbidding that would buy nothing.
_EXPECTED_EVASION_LINES = [5, 7, 8, 9, 10, 11, 13, 14, 15, 16]


def test_the_scan_catches_every_known_evasion(tmp_path: Path) -> None:
    """Mutation guard, run through the shipped scanner rather than a copy.

    The first version of this test re-implemented the predicate inline. That
    made it worthless in the exact scenario it named: stubbing ``_violations``
    to return ``[]`` left the whole file green, so the rule could stop
    enforcing anything without turning the suite red. It now calls
    :func:`_violations` against a synthetic package, so the guard fails if
    the shipped scanner regresses.

    Every listed shape was verified to walk past the first version, and one
    of them — ``model_dump_json`` — was not hypothetical: it was a live #403
    defect in ``retrieve trace --format json``, six lines below a call the
    same commit had fixed.
    """
    # ``lstrip`` so the line numbers in _EVASIONS' comments are the real ones.
    (tmp_path / "evasions.py").write_text(_EVASIONS.lstrip("\n"))

    reported = sorted(int(v.split(":")[1]) for v in _violations(root=tmp_path))
    assert reported == _EXPECTED_EVASION_LINES, (
        f"scanner reported {reported}, expected {_EXPECTED_EVASION_LINES}; "
        f"missing={sorted(set(_EXPECTED_EVASION_LINES) - set(reported))} "
        f"spurious={sorted(set(reported) - set(_EXPECTED_EVASION_LINES))}"
    )


def test_the_scan_sees_every_shape_in_the_shared_evasion_roster(
    tmp_path: Path,
) -> None:
    """The cross-rule half of the guard, run through ``_violations``.

    The corpus above is this rule's own and stays: it encodes the
    *judgement* — prose interpolation is allowed, a bare payload is not —
    against hand-read line numbers, which no shared roster can supply.
    What it cannot do is know about a spelling nobody thought of here, and
    that is exactly how #488 shipped: its synthetic tree carried only the
    shapes its scan already handled.

    ``tests.ast_rules.EVASIONS`` supplies the spelling, binding and
    placement axes; ``wrap`` supplies this rule's subject, which is a
    *composite* — a Rich render carrying a serializer call — rather than a
    bare construction. The serializer call is the part the roster models,
    and the shapes it adds here are real: two levels of attribute access,
    a call in an ``except`` block, in a nested closure, in a lambda body,
    at module and class scope, in an ``async def``, in a decorator, and in
    a second file. None of those were pinned before.

    The binding axis is what forced :func:`_serializer_names` to exist. An
    earlier version of this test exempted the alias, rebinding and
    subclass shapes on the claim that a single-file walk cannot resolve
    them; that claim was false, and the guard now refuses a non-residue
    exemption outright. The two that remain are residue and the harness
    demonstrates them by running every scanner it has.
    """
    assert_scan_is_not_vacuous(
        lambda root: [int(v.split(":")[1]) for v in _violations(root=root)],
        subject="dumps",
        kwarg="indent",
        wrap="console.print({call})",
        tmp_path=tmp_path,
        live_population=_rich_render_population(),
        floor=_RICH_RENDER_FLOOR,
        exempt={
            "partial_binding": (
                "residue: the binding's value is a call, so there is no "
                "name for construction_names to resolve. The standing "
                "mitigation is the width of _SERIALIZERS, which is why "
                "that set says to add to it on sight rather than on "
                "evidence"
            ),
            "cross_module_subclass": (
                "residue: a model class defined in another module and "
                "imported here is not in this tree, so its "
                "model_dump_json is unreachable under its own name. The "
                "attribute spelling — result.model_dump_json() — is the "
                "one the CLI actually writes, and that is caught"
            ),
        },
    )


class TestEmitterFidelity:
    """The emitters must be byte-transparent where Rich is not."""

    def test_emit_json_round_trips_emoji_shortcodes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        emit_json(_TRAP_PAYLOAD)
        assert json.loads(capsys.readouterr().out) == _TRAP_PAYLOAD

    def test_emit_machine_text_is_byte_identical(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        text = json.dumps(_TRAP_PAYLOAD)
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

    def test_emit_machine_text_flushes_what_it_wrote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flush is load-bearing and was previously unobservable.

        ``sys.stdout.write`` does not flush, so on a block-buffered pipe a
        payload could sit unwritten until process exit. ``capsys`` cannot
        see the difference — it does not buffer — so removing the flush
        changed no test. A recording stream can.
        """
        import sys as _sys

        stream = _RecordingStream()
        monkeypatch.setattr(_sys, "stdout", stream)

        emit_machine_text("payload")

        assert stream.writes == ["payload\n"]
        assert stream.flushes == 1, "emit_machine_text did not flush"

    def test_format_output_results_survive_the_emitter(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``format_output`` feeds ``emit_machine_text`` on three commands."""
        emit_machine_text(
            format_output([_TRAP_PAYLOAD], "json", wrapper={"status": "ok"})
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["items"] == [_TRAP_PAYLOAD]


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

    @pytest.fixture
    def isolated_cli(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point the CLI at a private config/data dir and pin the log level.

        Both matter and neither is decoration. Without the dir overrides
        these tests would resolve the *operator's* real ``~/.trellis`` —
        harmless only because ``_fail`` happens to fire before any store is
        constructed, which is a property of today's code rather than
        something the test states. And ``trellis_cli.main._root`` writes
        ``os.environ["TRELLIS_LOG_LEVEL"]`` with no restore, so an
        un-monkeypatched invocation leaks that level into every later test
        in the session.
        """
        monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("TRELLIS_LOG_LEVEL", "CRITICAL")
        (tmp_path / "data" / "stores").mkdir(parents=True)
        return tmp_path

    @staticmethod
    def _assert_json_error(result: object, *, expected_type: str) -> None:
        """stdout parses, names the failure, and the exit code still says so."""
        assert result.exit_code != 0, "a missing input must not exit 0"  # type: ignore[attr-defined]
        stdout = result.stdout  # type: ignore[attr-defined]
        assert stdout.strip(), "command produced no stdout at all"
        payload = json.loads(stdout)
        assert payload["status"] == "error"
        assert payload["error_type"] == expected_type, (
            "the envelope must match sanitized_error_payload, which every "
            "other JSON error in these commands emits"
        )

    @pytest.mark.parametrize(
        "argv",
        [
            ["ingest", "trace", "{missing}.json"],
            ["ingest", "evidence", "{missing}.json"],
            ["ingest", "dbt-manifest", "{missing}"],
            ["ingest", "openlineage", "{missing}.json"],
        ],
        ids=["trace", "evidence", "dbt-manifest", "openlineage"],
    )
    def test_ingest_reports_a_missing_input_as_json(
        self, cli_runner, isolated_cli: Path, argv: list[str]
    ) -> None:
        from trellis_cli.main import app

        missing = str(isolated_cli / "nope")
        result = cli_runner.invoke(
            app, [a.format(missing=missing) for a in argv] + ["--format", "json"]
        )
        self._assert_json_error(result, expected_type="FileNotFoundError")

    def test_policy_show_reports_a_missing_policy_as_json(
        self, cli_runner, isolated_cli: Path
    ) -> None:
        from trellis_cli.main import app

        result = cli_runner.invoke(
            app, ["policy", "show", "no-such-policy", "--format", "json"]
        )
        self._assert_json_error(result, expected_type="PolicyNotFound")


class TestRichCorruptionIsReal:
    """Pins the behaviour the whole rule is a response to."""

    #: Wide enough that nothing wraps, so the *silent* mode is reachable.
    #: A realistic terminal cannot be this wide; that is the point — every
    #: plausible width takes the loud branch, which is why the two have to
    #: be separate tests rather than one parametrized over widths.
    _UNWRAPPED_WIDTH = 100_000

    def test_rich_wrapping_makes_the_payload_unparseable(self) -> None:
        """The loud mode: a newline folded into a JSON string literal.

        Asserted at 80 columns, the width the defect was reported at.
        """
        rendered = _render_through_rich(_TRAP_PAYLOAD, width=80)
        with pytest.raises(json.JSONDecodeError):
            json.loads(rendered)

    def test_rich_corrupts_values_even_when_the_payload_still_parses(
        self,
    ) -> None:
        """The silent mode, and the reason this is two tests and not one.

        An earlier version parametrized one test over widths 40/80/100/200
        and returned early on ``JSONDecodeError``. Every one of those widths
        raises — the 400-char field guarantees wrapping — so the second
        assertion never ran on any parameter, and four cases measured one
        branch. That is the failure this repo keeps producing: a measurement
        path that can only read one value. Splitting them is what makes the
        silent mode reachable at all.
        """
        rendered = _render_through_rich(_TRAP_PAYLOAD, width=self._UNWRAPPED_WIDTH)
        decoded = json.loads(rendered)  # parses — that is the danger

        assert decoded != _TRAP_PAYLOAD, (
            "Rich output parsed AND matched the payload; the corruption this "
            "rule exists for did not occur"
        )
        assert decoded["doc_id"] != _TRAP_PAYLOAD["doc_id"], (
            "Rich no longer substitutes :notes:; re-evaluate the rule"
        )
        assert "[col]" not in decoded["markup"], (
            "Rich no longer strips [...] markup; re-evaluate the rule"
        )
