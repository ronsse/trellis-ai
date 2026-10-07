"""Every CLI failure line passes ``soft_wrap=True``.

A failure line is a ``*console.print`` of an f-string carrying ``[red]`` or
``[bold red]`` and an interpolation, with a non-zero ``typer.Exit`` or
``SystemExit`` raised on the same path after it. Rich hard-wraps a long
line at the console width, splitting the path or id it carries mid-token;
``soft_wrap=True`` leaves wrapping to the terminal, so the copied text
stays whole.

The scan sees three shapes: the message as a direct f-string, with the
exit a later statement in its own block or an enclosing one, OR nested
inside a later sibling statement such as ``if error is not None: raise
typer.Exit(...)`` (the shape ``test_format_exit_parity_rule.py`` asks
for); and the message as the red arm of a conditional expression
(``ast.IfExp``), on either side of it. Widening to the later-sibling shape
also caught two real lines no one had named: analyze.py's ``graph_shape``
and extract_refresh.py's per-key diff line both reach their function's
exit the same way, later and nested in an ``if``.

It still does not see an exit in a *different* function than the print:
a helper that returns an ``Exit`` for its caller to raise
(``admin_api_keys.py``'s ``_store_error``), or a helper that only prints
and leaves the exit decision to its caller (extract_refresh.py's
``_print_backfill``, called, then ``if refusal is not None: raise
typer.Exit(...)`` back in the caller). Both are wrapped by hand and left
out of the hand-read floor: resolving a second function's control flow is
more machinery than two call sites are worth. Nor does it see a receiver
not named ``*console``. Lines of those shapes are not policed here.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import CallSite, assert_hand_read_floor, iter_modules, name_of

SRC = Path(__file__).parents[2] / "src" / "trellis_cli"

#: 74 sites found by a tokenize recount written independently of this scan
#: (site-for-site equal to it at #766's head), less classify.py's shadow
#: summary, which exits ``EXIT_OK`` (73), plus 8 sites this widening makes
#: visible: admin.py:1533 and :1883, curate.py:78 (a later sibling
#: ``if``); extract_refresh.py:538 and ingest.py:448 and :523 (an
#: ``IfExp`` red arm, the shape the #766 gate named); and two the gate's
#: hand read did not name but the same shape covers once it is scanned
#: rather than eyeballed -- analyze.py:3130 and extract_refresh.py:563.
#: admin_api_keys.py:79 and extract_refresh.py:581 (an ``Exit`` in a
#: different function than the print) are wrapped by hand and stay out of
#: this count; see the module docstring. Counted outside this scan, never
#: computed by it.
HAND_READ_FAILURE_LINE_COUNT = 81

#: Spellings this repo raises to end the process.
_EXIT_NAMES = frozenset({"typer.Exit", "SystemExit", "Exit", "click.exceptions.Exit"})
#: Exit codes that mean success; ``exit_codes.EXIT_OK`` is 0.
_ZERO_CODES = frozenset({"0", "None", "EXIT_OK"})
_BLOCK_FIELDS = ("body", "orelse", "finalbody")


def _raises_nonzero_exit(stmt: ast.stmt) -> bool:
    """A ``raise`` of an exit whose code is not 0.

    ``Exit()``, ``SystemExit()`` and a bare ``raise typer.Exit`` all exit 0.
    """
    if not (isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call)):
        return False
    exc = stmt.exc
    if ast.unparse(exc.func) not in _EXIT_NAMES:
        return False
    codes = [*exc.args, *(kw.value for kw in exc.keywords if kw.arg == "code")]
    return bool(codes) and ast.unparse(codes[0]).rsplit(".", 1)[-1] not in _ZERO_CODES


def _raises_within(node: ast.AST) -> list[ast.Raise]:
    """Every ``raise`` reachable from *node* without crossing into a nested
    function or class body -- the raises a later sibling statement such as
    ``if error is not None: raise typer.Exit(...)`` can reach on some path
    through itself, not just as a direct statement."""
    if isinstance(node, ast.Raise):
        return [node]
    if isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    ):
        return []
    found: list[ast.Raise] = []
    for child in ast.iter_child_nodes(node):
        found.extend(_raises_within(child))
    return found


def _exit_follows(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """A non-zero exit is raised after *call*'s statement, in its own block
    or an enclosing one, before the enclosing function ends.

    "After" reaches into a later sibling statement's own body too (a later
    ``if``'s nested ``raise``, not only a direct one): a fair approximation
    of "on the same path" that does not flag a line whose only later exit
    is zero, since ``_raises_nonzero_exit`` still judges each raise found.
    """
    current: ast.AST = call
    while not isinstance(current, ast.stmt):
        current = parents[current]
    while not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
        owner = parents[current]
        for field in _BLOCK_FIELDS:
            block = getattr(owner, field, None)
            if isinstance(block, list) and current in block:
                later = block[block.index(current) + 1 :]
                if any(
                    _raises_nonzero_exit(raise_stmt)
                    for stmt in later
                    for raise_stmt in _raises_within(stmt)
                ):
                    return True
        current = owner
    return False


def _find_red_message(expr: ast.expr) -> ast.JoinedStr | None:
    """The f-string carrying ``[red]``/``[bold red]``: *expr* itself, or
    whichever arm of a conditional expression (``ast.IfExp``) is red.

    Recurses so a conditional expression nested inside another is found
    too; a plain (non-f) string arm is an ``ast.Constant``, which neither
    branch matches, same as today.
    """
    if isinstance(expr, ast.JoinedStr):
        literal = "".join(p.value for p in expr.values if isinstance(p, ast.Constant))
        if "[red]" in literal or "[bold red]" in literal:
            return expr
        return None
    if isinstance(expr, ast.IfExp):
        return _find_red_message(expr.body) or _find_red_message(expr.orelse)
    return None


def failure_line_sites(root: Path) -> list[CallSite]:
    """Every failure line under *root*, wrapped or not."""
    sites: list[CallSite] = []
    for path, tree in iter_modules(root):
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            ):
                continue
            receiver = name_of(node.func.value) or ""
            if node.func.attr != "print" or not receiver.lower().endswith("console"):
                continue
            if not node.args:
                continue
            red_message = _find_red_message(node.args[0])
            if red_message is None:
                continue
            interpolated = any(
                isinstance(p, ast.FormattedValue) for p in red_message.values
            )
            if interpolated and _exit_follows(node, parents):
                sites.append(CallSite(path=path, node=node))
    return sites


def _has_soft_wrap(call: ast.Call) -> bool:
    return any(
        keyword.arg == "soft_wrap"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def test_every_cli_failure_line_passes_soft_wrap() -> None:
    sites = failure_line_sites(SRC)
    assert_hand_read_floor(
        len(sites),
        HAND_READ_FAILURE_LINE_COUNT,
        subject="CLI failure line (red console.print + non-zero exit)",
        hint="Recount by hand; if the tree really shrank, lower the floor.",
    )
    unwrapped = [site.describe(SRC) for site in sites if not _has_soft_wrap(site.node)]
    assert not unwrapped, (
        "CLI failure line(s) without soft_wrap=True; Rich hard-wraps them at "
        "the console width and splits the path or id they carry: "
        + "; ".join(unwrapped)
    )


def test_rule_discriminates_wrap_exit_code_and_block(tmp_path: Path) -> None:
    """``missing`` and ``enclosing`` are violations (exit in the same block,
    exit after an enclosing ``if``); ``compliant`` is wrapped; ``no_exit``
    raises nothing; ``other_receiver`` prints through a receiver not named
    ``*console``; ``zero_exit`` spells exit 0 four ways."""
    header = "import typer\nfrom trellis_cli.output import console\n\n"
    files = {
        "compliant.py": (
            "def run(path):\n"
            "    console.print(f'[red]Not found: {path}[/red]', soft_wrap=True)\n"
            "    raise typer.Exit(code=1)\n"
        ),
        "missing.py": (
            "def run(path):\n"
            "    console.print(f'[bold red]Not found: {path}[/bold red]')\n"
            "    raise typer.Exit(code=1)\n"
        ),
        "enclosing.py": (
            "def run(path, strict):\n"
            "    if strict:\n"
            "        console.print(f'[red]Strict failure on: {path}[/red]')\n"
            "    raise SystemExit(2)\n"
        ),
        "other_receiver.py": (
            "def run(path):\n"
            "    printer.print(f'[red]Not found: {path}[/red]')\n"
            "    raise typer.Exit(code=1)\n"
        ),
        "no_exit.py": (
            "def run(path):\n"
            "    console.print(f'[red]Warning about: {path}[/red]')\n"
            "    return None\n"
        ),
        "zero_exit.py": (
            "def bare_call(path):\n"
            "    console.print(f'[red]Bare call after: {path}[/red]')\n"
            "    raise typer.Exit()\n"
            "\n"
            "def bare_name(path):\n"
            "    console.print(f'[red]Bare name after: {path}[/red]')\n"
            "    raise typer.Exit\n"
            "\n"
            "def literal_zero(path):\n"
            "    console.print(f'[red]Literal zero after: {path}[/red]')\n"
            "    raise typer.Exit(code=0)\n"
            "\n"
            "def exit_ok(path):\n"
            "    console.print(f'[red]EXIT_OK after: {path}[/red]')\n"
            "    raise typer.Exit(code=EXIT_OK)\n"
        ),
    }
    for name, body in files.items():
        (tmp_path / name).write_text(header + body, encoding="utf-8")

    sites = failure_line_sites(tmp_path)
    found = sorted(site.path.name for site in sites)
    assert found == ["compliant.py", "enclosing.py", "missing.py"], found
    unwrapped = sorted(s.path.name for s in sites if not _has_soft_wrap(s.node))
    assert unwrapped == ["enclosing.py", "missing.py"], unwrapped


def test_rule_finds_later_if_and_ifexp_shapes(tmp_path: Path) -> None:
    """Two shapes the #766 gate found and #766's rule could not see: an
    exit raised inside a *later* sibling ``if`` rather than as a direct
    statement (``later_if.py``, the shape
    ``test_format_exit_parity_rule.py`` requires), and the red arm of a
    conditional expression, on either side of it (``ifexp_red_orelse.py``,
    ``ifexp_red_body.py``). ``_zero`` variants of each must stay out of the
    population entirely, the same as ``zero_exit.py`` above.
    ``helper_returned_exit.py`` pins a third shape -- an ``Exit`` a helper
    returns for its caller to raise -- that stays unseen; see the module
    docstring."""
    header = "import typer\nfrom trellis_cli.output import console\n\n"
    files = {
        "later_if.py": (
            "def run(output_format, path):\n"
            "    error = None\n"
            "    if output_format == 'json':\n"
            "        pass\n"
            "    elif path:\n"
            "        console.print(f'[red]Later-if failure: {path}[/red]')\n"
            "        error = path\n"
            "    if error is not None:\n"
            "        raise typer.Exit(code=1)\n"
        ),
        "later_if_zero.py": (
            "def run(output_format, path):\n"
            "    error = None\n"
            "    if output_format == 'json':\n"
            "        pass\n"
            "    elif path:\n"
            "        console.print(f'[red]Later-if zero: {path}[/red]')\n"
            "        error = path\n"
            "    if error is not None:\n"
            "        raise typer.Exit(code=EXIT_OK)\n"
        ),
        "ifexp_red_orelse.py": (
            "def run(refusal, label):\n"
            "    console.print(\n"
            "        f'[green]OK {label}[/green]'\n"
            "        if refusal is None\n"
            "        else f'[red]Failed {label}[/red]'\n"
            "    )\n"
            "    if refusal is not None:\n"
            "        raise typer.Exit(code=1)\n"
        ),
        "ifexp_red_body.py": (
            "def run(refusal, label):\n"
            "    console.print(\n"
            "        f'[red]Failed {label}[/red]'\n"
            "        if refusal is not None\n"
            "        else f'[green]OK {label}[/green]'\n"
            "    )\n"
            "    if refusal is not None:\n"
            "        raise typer.Exit(code=1)\n"
        ),
        "ifexp_zero_exit.py": (
            "def run(refusal, label):\n"
            "    console.print(\n"
            "        f'[red]Failed {label}[/red]'\n"
            "        if refusal is not None\n"
            "        else f'[green]OK {label}[/green]'\n"
            "    )\n"
            "    if refusal is not None:\n"
            "        raise typer.Exit(code=EXIT_OK)\n"
        ),
        "helper_returned_exit.py": (
            "def _build_error(path):\n"
            "    console.print(f'[red]Store error: {path}[/red]')\n"
            "    return typer.Exit(code=1)\n"
            "\n"
            "\n"
            "def run(path):\n"
            "    raise _build_error(path)\n"
        ),
    }
    for name, body in files.items():
        (tmp_path / name).write_text(header + body, encoding="utf-8")

    sites = failure_line_sites(tmp_path)
    found = sorted(site.path.name for site in sites)
    assert found == ["ifexp_red_body.py", "ifexp_red_orelse.py", "later_if.py"], found
    assert not any(_has_soft_wrap(s.node) for s in sites)
