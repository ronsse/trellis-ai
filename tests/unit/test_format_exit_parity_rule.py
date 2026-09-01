"""Enforcement for the format/exit-parity rule (#437).

``docs/design/adr-cli-exit-codes.md`` gives operators five codes to branch
on and says JSON output is *unchanged* by them: ``--format json`` callers
parse ``status``, shell callers read the exit code. What it never says —
because nobody thought it needed saying — is that the two surfaces must
agree about whether the command failed.

``trellis admin migrate-graph`` did not. Its ``raise typer.Exit(code=
EXIT_STORE)`` sat inside the ``else`` (text) arm of ``if output_format ==
"json"``, so on a failed migration the text surface exited ``5`` and the
JSON surface emitted its payload and fell through to ``0``. The JSON path
is the one a script reads, so the surface built for machine consumption
was the surface that reported success for a failed store migration, and a
caller doing the documented thing was *strictly worse off* than one
scraping human prose.

That is this repo's named defect class — a mechanism reporting success
while doing nothing — landing on an exit code instead of a metric. It is
also the shape that produced #403 and #422: a ``--format json`` branch
that returns early, before the line that would have reported the failure.

So the rule:

    A command's exit code must not depend on ``--format``. If one arm of
    a format branch can end the process non-zero, the sibling arm must be
    able to as well.

**What the scan proves, and what it does not.** It is a reachability
check, not a path-sensitive proof. For every ``if`` whose test is a
resolvable comparison on ``output_format``, it asks whether one arm
*contains* a non-zero exit and the other arm contains none at all. It
does not try to show that two arms exit under the *same conditions* —
``if a: exit(5)`` in one arm against ``if b: exit(5)`` in the other passes,
and making that decidable is not tractable. The property it does enforce
is the one #437 violated and the one a reviewer cannot eyeball across a
2300-line module: an arm from which non-zero exit is structurally
unreachable, facing one from which it is not.

Four deliberate refinements, each of which changes the answer on real
code:

* **The descent covers ``except`` handlers.** ``ast.ExceptHandler`` is not
  an ``ast.stmt``, so a statement-only walk skips every ``except`` block —
  25 of the 148 format branches in ``trellis_cli``, and the place a
  command is most likely to branch on format while deciding *how to fail*.
* **The sibling of an ``if`` with no ``else`` is the code after it**, not
  nothing — but only when the ``if`` body terminates. That is the #422
  shape (``emit_json(...); return`` above the error exit), and without it
  the scan would miss it entirely.
* **A branch no supported format can reach is skipped.** ``retrieve
  file-context`` guards with ``if output_format not in ("text", "json",
  "jsonl"): raise typer.Exit(EXIT_VALIDATION)``. That arm exits non-zero
  and its sibling does not, but it is an argument validator: no format
  the command renders can reach it. The universe of formats is derived
  per function from the literals it compares against (plus ``text``, the
  option default) rather than hardcoded, so the exemption cannot rot into
  a roster of blessed line numbers.
* **A call to a helper that always exits non-zero counts as an exit.**
  ``ingest._fail`` and ``policy._exit_on_refused_write`` both render on
  the caller's surface and then exit below the branch; a command calling
  one of those from a single arm is a real divergence. Only helpers whose
  *last* statement is the exit are counted — the conservative direction,
  because a missed helper produces a loud false positive a reviewer
  resolves, while an over-eager one produces the silent false negative
  this rule exists to prevent.

**The sweep found ``migrate-graph`` alone.** 148 format-conditioned
branches across 18 modules, one divergence. ``policy list`` /
``policy show`` are the counter-example worth naming: they raise
``EXIT_STORE`` from *inside* the JSON arm and again from the text path, so
both surfaces agree — the pattern was already understood in this
codebase, it just was not enforced anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The one name the CLI uses for the ``--format`` option value. Verified by
#: :func:`test_the_scan_finds_the_format_branches_it_polices`, which would
#: collapse to zero if a rename ever made this stale.
FORMAT_VAR = "output_format"

#: Every ``--format`` option in ``trellis_cli`` defaults to ``text``, and no
#: comparison mentions it in the commands that only branch on ``"json"``. It
#: has to be seeded into the format universe by hand or ``if output_format
#: == "json": ... else: ...`` would compute an empty ``else`` set and exempt
#: itself — which is the #437 site.
DEFAULT_FORMAT = "text"

#: Names that mean "exit code zero" when passed to an exit call. Anything
#: else — a literal, another ``EXIT_*`` constant, a computed value — is
#: treated as non-zero, because a divergence that only shows up for some
#: runtime value of ``exit_code`` is still a divergence.
_ZERO_EXIT_NAMES = frozenset({"EXIT_OK"})


def _cli_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src" / "trellis_cli"
    assert root.is_dir(), f"trellis_cli not found at {root}"
    return root


def _mentions_format(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == FORMAT_VAR for n in ast.walk(node))


def _string_operand(node: ast.expr) -> set[str] | None:
    """The string literal(s) on the right of a comparison, or ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        literals = [
            e.value
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        # All-or-nothing: one non-literal element and the set is a guess.
        return set(literals) if len(literals) == len(node.elts) else None
    return None


def _format_universe(func: ast.AST) -> set[str]:
    """Format values *func* distinguishes: every literal it compares, plus text."""
    universe = {DEFAULT_FORMAT}
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == FORMAT_VAR
        ):
            for comp in node.comparators:
                literals = _string_operand(comp)
                if literals:
                    universe |= literals
    return universe


def _matching_formats(test: ast.expr, universe: set[str]) -> set[str] | None:
    """Formats in *universe* for which *test* holds; ``None`` if undecidable.

    Only single-operator comparisons directly on ``output_format`` are
    resolved. A test the scan cannot read is skipped rather than guessed
    at — an unreadable test is a reviewer's problem, and inventing an
    answer for it is how a rule starts reporting on shapes it does not
    understand.
    """
    if not (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == FORMAT_VAR
        and len(test.ops) == 1
        and len(test.comparators) == 1
    ):
        return None
    literals = _string_operand(test.comparators[0])
    if literals is None:
        return None
    op = test.ops[0]
    if isinstance(op, (ast.Eq, ast.In)):
        return universe & literals
    if isinstance(op, (ast.NotEq, ast.NotIn)):
        return universe - literals
    return None


def _exit_kind(node: ast.AST, exiting_helpers: frozenset[str]) -> str | None:
    """``"nonzero"`` / ``"zero"`` if *node* ends the process, else ``None``."""
    call: ast.Call | None = None
    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
        name = getattr(node.exc.func, "attr", None) or getattr(
            node.exc.func, "id", None
        )
        if name == "Exit":
            call = node.exc
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        name = getattr(node.value.func, "attr", None) or getattr(
            node.value.func, "id", None
        )
        if name == "exit":
            call = node.value
        elif name in exiting_helpers:
            return "nonzero"
    if call is None:
        return None
    args = list(call.args) + [k.value for k in call.keywords if k.arg == "code"]
    if not args:
        # ``typer.Exit()`` defaults to code 0.
        return "zero"
    code = args[0]
    if isinstance(code, ast.Constant):
        return "zero" if code.value in (0, None) else "nonzero"
    if isinstance(code, ast.Name):
        return "zero" if code.id in _ZERO_EXIT_NAMES else "nonzero"
    return "nonzero"


def _can_exit_nonzero(stmts: list[ast.stmt], exiting_helpers: frozenset[str]) -> bool:
    return any(
        _exit_kind(n, exiting_helpers) == "nonzero" for s in stmts for n in ast.walk(s)
    )


def _terminates(stmts: list[ast.stmt]) -> bool:
    return bool(stmts) and isinstance(stmts[-1], (ast.Return, ast.Raise))


def _always_exiting_helpers(tree: ast.Module) -> frozenset[str]:
    """Module functions whose *last* statement ends the process non-zero.

    Deliberately must-exit rather than may-exit. See the module docstring:
    an unrecognised helper costs a false positive, an over-recognised one
    costs a silent miss.
    """
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in found or not node.body:
                continue
            if _exit_kind(node.body[-1], frozenset(found)) == "nonzero":
                found.add(node.name)
                changed = True
    return frozenset(found)


def _format_branches(func: ast.AST) -> list[tuple[list[ast.stmt], int, ast.If]]:
    """Every format-conditioned ``If`` in *func*, with its enclosing block.

    The block is carried because an ``if`` with no ``else`` has its real
    sibling in the statements that follow it.
    """
    found: list[tuple[list[ast.stmt], int, ast.If]] = []

    def walk(node: ast.AST) -> None:
        for _field, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            for index, item in enumerate(value):
                # Any AST node in a list, not just ``ast.stmt``:
                # ``Try.handlers`` holds ``ExceptHandler``, which is not a
                # statement, so a stmt-only descent silently skipped every
                # ``except`` block — where a command is most likely to
                # branch on format while deciding how to fail.
                if not isinstance(item, ast.AST):
                    continue
                if isinstance(item, ast.If) and _mentions_format(item.test):
                    found.append((value, index, item))
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(item)

    walk(func)
    return found


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _violations(root: Path | None = None) -> list[str]:
    """Format branches whose two arms disagree about exiting non-zero.

    *root* is injectable so the mutation guard runs **this** function over a
    synthetic tree rather than a copy of its predicates — the mistake
    ``test_machine_output_rule`` had to correct, where the guard guarded a
    reimplementation and stubbing the real scan left the suite green.
    """
    cli_root = root if root is not None else _cli_root()
    found: list[str] = []
    for py_file in sorted(cli_root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        helpers = _always_exiting_helpers(tree)
        for func in _functions(tree):
            universe = _format_universe(func)
            for block, index, branch in _format_branches(func):
                matched = _matching_formats(branch.test, universe)
                if matched is None:
                    continue
                other = universe - matched
                # A branch no supported format reaches cannot make two
                # supported formats disagree (the unsupported-format guard).
                if not matched or not other:
                    continue
                body = branch.body
                if branch.orelse:
                    sibling = branch.orelse
                elif _terminates(body):
                    sibling = block[index + 1 :]
                else:
                    # Control merges; whatever follows applies to both arms.
                    continue
                if _can_exit_nonzero(body, helpers) != _can_exit_nonzero(
                    sibling, helpers
                ):
                    found.append(
                        f"{py_file.name}:{branch.lineno}: {func.name}() — "
                        f"'{ast.unparse(branch.test)}' exits non-zero on one "
                        f"arm only"
                    )
    return found


def test_no_command_exit_diverges_by_output_format() -> None:
    """The rule. Hoist the exit out of the format branch, as #437 did."""
    violations = _violations()
    assert not violations, (
        "A command's exit code must not depend on --format (#437): the JSON "
        "surface is the one a script reads, so a failure it exits 0 on is a "
        "silent success. Move the exit below the format branch and derive "
        "the JSON payload's `status` from the same flag "
        "(docs/design/adr-cli-exit-codes.md).\n  " + "\n  ".join(violations)
    )


def test_the_scan_finds_the_format_branches_it_polices() -> None:
    """Vacuity guard: the scan still sees a real population to reason about.

    A structural test that stops matching — ``output_format`` renamed, the
    package moved, ``--format`` replaced by a callback — keeps passing while
    enforcing nothing. ``mypy src/`` aborting on zero files and reading as
    clean is the cautionary tale this repo already owns.
    """
    branches = 0
    resolvable = 0
    for py_file in sorted(_cli_root().rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for func in _functions(tree):
            universe = _format_universe(func)
            for _block, _index, branch in _format_branches(func):
                branches += 1
                if _matching_formats(branch.test, universe) is not None:
                    resolvable += 1
    assert branches > 100, (
        f"only {branches} format-conditioned branches found in trellis_cli; "
        f"the scan has drifted and is no longer policing anything"
    )
    assert resolvable > 0.9 * branches, (
        f"only {resolvable}/{branches} format tests are resolvable; the scan "
        f"skips what it cannot read, so a drop here is silent under-coverage"
    )


def test_the_always_exiting_helper_set_is_not_empty() -> None:
    """Second vacuity guard, for the interprocedural half of the scan.

    ``ingest._fail`` and ``policy._exit_on_refused_write`` are the shape:
    render on the caller's surface, then exit below the branch. If this set
    silently emptied, every command that delegates its exit to a helper
    would stop being checked, and nothing else in this file would notice.
    """
    helpers_by_module = {}
    for py_file in sorted(_cli_root().rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        helpers = _always_exiting_helpers(tree)
        if helpers:
            helpers_by_module[py_file.name] = helpers
    assert "_fail" in helpers_by_module.get("ingest.py", frozenset())
    assert "_exit_on_refused_write" in helpers_by_module.get("policy.py", frozenset())


#: Every shape the rule must catch, plus the four it must leave alone. The
#: trailing comment on each ``if`` is why it is here; the number is the line
#: the scan is expected to report.
_SHAPES = """
import sys
import typer
from trellis_cli.exit_codes import EXIT_OK, EXIT_STORE, EXIT_VALIDATION


def _always_exits(message, output_format):
    if output_format == "json":
        emit_json({"status": "error", "message": message})
    else:
        console.print(message)
    raise typer.Exit(code=EXIT_STORE)


def json_arm_falls_through_to_zero(output_format, report):
    if output_format == "json":                  # 15 the #437 shape
        emit_json(report)
    else:
        console.print(report)
        if report.errors:
            raise typer.Exit(code=EXIT_STORE)


def text_arm_falls_through_to_zero(output_format, report):
    if output_format == "json":                  # 24 the mirror image
        emit_json(report)
        raise typer.Exit(code=EXIT_STORE)
    console.print(report)


def json_arm_returns_before_the_error_exit(output_format, report):
    if output_format == "json":                  # 31 the #422 early return
        emit_json(report)
        return
    console.print(report)
    raise typer.Exit(code=EXIT_STORE)


def text_arm_uses_sys_exit(output_format, report):
    if output_format == "json":                  # 39 sys.exit, not typer.Exit
        emit_json(report)
    else:
        console.print(report)
        sys.exit(EXIT_STORE)


def json_arm_exits_zero_explicitly(output_format, report):
    if output_format == "json":                  # 47 EXIT_OK is not a failure
        emit_json(report)
        raise typer.Exit(code=EXIT_OK)
    console.print(report)
    raise typer.Exit(code=EXIT_STORE)


def negated_polarity(output_format, report):
    if output_format != "json":                  # 55 text arm is the body
        console.print(report)
        raise typer.Exit(code=EXIT_STORE)
    emit_json(report)


def divergence_via_an_always_exiting_helper(output_format, report):
    if output_format == "json":                  # 62 helper exits, arm does not
        emit_json(report)
    else:
        _always_exits("migration failed", output_format)


def both_arms_exit(output_format, report):
    if output_format == "json":                  # 69 ALLOWED
        emit_json(report)
        if report.errors:
            raise typer.Exit(code=EXIT_STORE)
    else:
        console.print(report)
        if report.errors:
            raise typer.Exit(code=EXIT_STORE)


def exit_hoisted_out_of_the_branch(output_format, report):
    if output_format == "json":                  # 80 ALLOWED — the fix
        emit_json(report)
    else:
        console.print(report)
    if report.errors:
        raise typer.Exit(code=EXIT_STORE)


def unsupported_format_guard(output_format, report):
    if output_format not in ("text", "json", "jsonl"):   # 89 ALLOWED — validator
        console.print("unsupported")
        raise typer.Exit(EXIT_VALIDATION)
    if output_format == "json":                  # 92 ALLOWED — neither arm exits
        emit_json(report)
    else:
        console.print(report)


def divergence_inside_an_except_handler(output_format, report):
    try:
        migrate()
    except OSError:
        if output_format == "json":              # 102 not an ast.stmt list
            emit_json(report)
        else:
            console.print(report)
            raise typer.Exit(code=EXIT_STORE)
"""

#: Lines :data:`_SHAPES` must report. 69, 80, 89 and 92 are deliberately
#: absent — the first two are correct code, and 89 is an argument validator
#: whose raising arm no rendered format can reach.
_EXPECTED_SHAPE_LINES = [15, 24, 31, 39, 47, 55, 62, 102]


def test_the_scan_catches_every_known_shape(tmp_path: Path) -> None:
    """Mutation guard, run through the shipped scanner rather than a copy.

    Every part of the scan is load-bearing, measured by stubbing each one
    out and re-running this list rather than by assertion:

    ==================================  ==============================
    weakening                           result
    ==================================  ==============================
    no fallthrough sibling              loses 24, 31, 47, 55
    ``sys.exit`` not counted            loses 39
    ``!=`` / ``not in`` polarity gone   loses 55
    no always-exiting-helper set        loses 62
    ``EXIT_OK`` counted as a failure    loses 47
    no reachability exemption           **gains 89**, a false positive
    ==================================  ==============================
    """
    (tmp_path / "shapes.py").write_text(_SHAPES.lstrip("\n"), encoding="utf-8")

    reported = sorted(int(v.split(":")[1]) for v in _violations(root=tmp_path))
    assert reported == _EXPECTED_SHAPE_LINES, (
        f"scanner reported {reported}, expected {_EXPECTED_SHAPE_LINES}; "
        f"missing={sorted(set(_EXPECTED_SHAPE_LINES) - set(reported))} "
        f"spurious={sorted(set(reported) - set(_EXPECTED_SHAPE_LINES))}"
    )
