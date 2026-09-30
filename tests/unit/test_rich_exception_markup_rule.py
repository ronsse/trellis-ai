"""Exception text reaching a Rich renderer is escaped: the id rule's third shape.

Rich reads ``[word…]`` in a printed string as a style tag and deletes it, and
an unmatched ``[/…]`` raises ``MarkupError``. Exception text carries brackets
nobody in the CLI chose: pydantic ends every error line with
``[type=missing, input_value=…, input_type=dict]``, an install hint names an
extra (``uv pip install -e ".[cloud]"`` printed as ``-e "."``), and a YAML
error quotes the offending line, so ``"[/x]"`` in ``learning_params.yaml``
turned ``analyze learning-candidates``'s error message into a traceback.
:mod:`tests.unit.test_rich_id_markup_rule` polices names *shaped* like a
handle; this module polices text *derived* from an exception, over that
module's renderer definition, imported so the two rules cannot disagree
about what a renderer is.

    **Exception-derived text reaching a Rich renderer in ``src/trellis_cli``
    is wrapped in ``rich.markup.escape`` at its outermost node, or the render
    call passes ``markup=False``.**

Exception-derived, per function (the innermost ``def`` around the render): a
name bound by ``except … as NAME``; a parameter annotated with a class whose
name ends ``Error`` or ``Exception`` (``_store_error(exc: Exception, …)``);
and, to a fixed point, any name the function binds (``=``, annotated,
augmented, walrus, ``for`` target) from an expression that reads a derived
name, unless that expression is ``escape(...)``. Hops are unbounded inside
the function; the deepest live one is two (``main.py``'s ``failure`` loop).

It misses, on purpose, text that leaves the function (a result object such as
``CommandResult.message`` or ``report.errors``, or a helper's return value),
a closure reading its enclosing function's exception, and a parameter typed
with an exception class named otherwise (``typer.BadParameter``). It
over-collects, on purpose, ``console.print(exc)`` (Rich renders the object
itself without markup, but no AST check can tell it from a string rebound to
the same name) and non-text attributes such as ``exc.start``: escaping those
costs a ``str()``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor, iter_modules, name_of
from tests.unit.test_rich_id_markup_rule import (
    _cli_root,
    _enclosing_scopes,
    _is_escaped,
    _markup_disabled,
    _rendered_values,
    _rich_entry_points,
)

_EXCEPTION_SUFFIXES = ("Error", "Exception")


def _names(node: ast.AST, ctx: type[ast.expr_context] = ast.Load) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ctx)
    }


def _exception_derived_names(scope: ast.AST) -> set[str]:
    """The names in *scope* that hold exception text, per the docstring."""
    derived: set[str] = set()
    bindings: list[tuple[ast.expr, ast.expr]] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.ExceptHandler) and node.name:
            derived.add(node.name)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            if any(
                (name_of(part) or "").endswith(_EXCEPTION_SUFFIXES)
                for part in ast.walk(node.annotation)
            ):
                derived.add(node.arg)
        elif isinstance(node, ast.Assign):
            bindings.extend((target, node.value) for target in node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            if node.value is not None:
                bindings.append((node.target, node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bindings.append((node.target, node.iter))
    grew = True
    while grew:
        grew = False
        for target, value in bindings:
            if _is_escaped(value) or not _names(value) & derived:
                continue
            new = _names(target, ast.Store) - derived
            derived |= new
            grew = grew or bool(new)
    return derived


def _exception_renders(root: Path | None = None) -> tuple[list[str], int]:
    """(``file:line: values`` for each raw render, renders carrying exception text).

    *root* is injectable so the judgement test runs this shipped function,
    not a copy of it, over a synthetic tree.
    """
    offences: list[str] = []
    population = 0
    for path, tree in iter_modules(root if root is not None else _cli_root()):
        scopes = _enclosing_scopes(tree)
        derived: dict[ast.AST, set[str]] = {}
        for call in _rich_entry_points(tree):
            scope = scopes.get(call, tree)
            if scope not in derived:
                derived[scope] = _exception_derived_names(scope)
            values = [v for v in _rendered_values(call) if _names(v) & derived[scope]]
            population += bool(values)
            raw = [value for value in values if not _is_escaped(value)]
            if raw and not _markup_disabled(call):
                rendered = ", ".join(ast.unparse(value) for value in raw)
                offences.append(f"{path.name}:{call.lineno}: {rendered}")
    return offences, population


def test_no_exception_text_reaches_a_rich_renderer_unescaped() -> None:
    offences, _ = _exception_renders()
    assert not offences, (
        "Rich deletes `[word]` from exception text and raises MarkupError on a "
        "stray `[/x]`: pydantic's `[type=missing, ...]` and an install hint's "
        '`".[cloud]"` vanished. Print `escape(str(exc))`, or pass '
        "`markup=False` to a line with no styling of its own.\n  "
        + "\n  ".join(offences)
    )


#: Hand-read at ``a0593812`` (the ``admin.py`` delta to ``39c9a318`` adds none):
#: 73 ``except … as exc`` handlers and 7 exception-annotated parameters in
#: ``src/trellis_cli``; 44 render calls carry exception-derived text, 30 raw
#: and 14 already escaped.
_EXCEPTION_RENDER_FLOOR = 44


def test_the_scan_finds_the_exception_renders_it_polices() -> None:
    assert_hand_read_floor(
        _exception_renders()[1],
        _EXCEPTION_RENDER_FLOOR,
        subject="Rich render carrying exception text in trellis_cli",
        hint="except-as names, exception-annotated parameters, locals built from them.",
    )


#: Each line's comment carries its real line number in the rendered file.
_JUDGEMENTS = """
import sys
from rich.markup import escape

try:
    run()
except ValueError as exc:
    console.print(f"[red]{exc}[/red]")                # 7  the control
    console.print(f"[red]{escape(str(exc))}[/red]")   # 8  ALLOWED: escaped
    console.print(f"[red]{exc}[/red]", markup=False)  # 9  ALLOWED: markup off
    console.print(exc)                                # 10 over-collected: the object
    err_console.print(str(exc))                       # 11 not an f-string
    message = f"{type(exc).__name__}: {exc}"          # 12
    console.print(f"store error: {message}")          # 13 one hop
    detail = message.strip()                          # 14
    table.add_row(detail)                             # 15 two hops, a Table cell
    reason = escape(str(exc))                         # 16
    console.print(f"{reason}")                        # 17 ALLOWED: escaped when bound
    console.print(f"{exc.encoding} at {exc.start}")   # 18 over-collected: attributes
    console.print(f"{escape(message)}: {exc}")        # 19 one escaped, one not
    typer.echo(f"{exc}")                              # 20 ALLOWED: not Rich
    sys.stdout.write(f"{exc}\\n")                      # 21 ALLOWED: not a renderer
    console.print(f"{count} rows")                    # 22 ALLOWED: not exception text
    for line in exc.errors():                         # 23
        console.print(line)                           # 24 a loop target
    Table(title=f"failed: {exc}")                     # 25 a title renders markup


def _store_error(exc: Exception, output_format: str) -> None:
    console.print(f"[red]{exc}[/red]")                # 29 an annotated parameter


async def _refused(err: StoreError | None) -> None:
    hint: str = f"{err}"                              # 33
    console.print(hint)                               # 34 annotated binding, a union
    try:
        run()
    except LookupError as exc:
        error = {"message": exc.message}              # 38
    if text := error["message"]:                      # 39
        console.print(text)                           # 40 after the handler, a walrus
    note = "lookup failed: "                          # 41
    note += str(exc)                                  # 42
    console.print(note)                               # 43 augmented assignment
    console.print(f"{message} {output_format}")       # 44 ALLOWED: other scopes' names
"""

_EXPECTED_JUDGEMENT_LINES = [7, 10, 11, 13, 15, 18, 19, 24, 25, 29, 34, 40, 43]


def test_the_scan_makes_the_judgements_this_rule_claims(tmp_path: Path) -> None:
    """The offences and the allowed lines both: either half alone is vacuous."""
    (tmp_path / "judgements.py").write_text(_JUDGEMENTS.lstrip("\n"))
    offences, _ = _exception_renders(tmp_path)
    reported = sorted(int(offence.split(":")[1]) for offence in offences)
    assert reported == _EXPECTED_JUDGEMENT_LINES, (
        f"missing={sorted(set(_EXPECTED_JUDGEMENT_LINES) - set(reported))} "
        f"spurious={sorted(set(reported) - set(_EXPECTED_JUDGEMENT_LINES))}"
    )
