"""Every CLI failure line passes ``soft_wrap=True``.

A failure line is a ``*console.print`` of an f-string carrying ``[red]`` or
``[bold red]`` and an interpolation, with a non-zero ``typer.Exit`` or
``SystemExit`` raised after it in its own block or an enclosing one. Rich
hard-wraps a long line at the console width, splitting the path or id it
carries mid-token; ``soft_wrap=True`` leaves wrapping to the terminal, so
the copied text stays whole.

The scan sees only that shape. It does not see an exit raised under a later
condition (``if error is not None: raise typer.Exit(...)``, the shape
``test_format_exit_parity_rule.py`` asks for), an ``Exit`` a helper returns
for its caller to raise, a print whose argument is a conditional
expression, or a receiver not named ``*console``. Lines of those shapes are
not policed here.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import CallSite, assert_hand_read_floor, iter_modules, name_of

SRC = Path(__file__).parents[2] / "src" / "trellis_cli"

#: 74 sites found by a tokenize recount written independently of this scan
#: (site-for-site equal to it), less classify.py's shadow summary, which
#: exits ``EXIT_OK``. A floor read off the tree, not computed by the scan.
HAND_READ_FAILURE_LINE_COUNT = 73

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


def _exit_follows(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """A non-zero exit is raised after *call*'s statement, in its own block
    or an enclosing one, before the enclosing function ends."""
    current: ast.AST = call
    while not isinstance(current, ast.stmt):
        current = parents[current]
    while not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
        owner = parents[current]
        for field in _BLOCK_FIELDS:
            block = getattr(owner, field, None)
            if isinstance(block, list) and current in block:
                later = block[block.index(current) + 1 :]
                if any(_raises_nonzero_exit(stmt) for stmt in later):
                    return True
        current = owner
    return False


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
            if not node.args or not isinstance(node.args[0], ast.JoinedStr):
                continue
            parts = node.args[0].values
            literal = "".join(p.value for p in parts if isinstance(p, ast.Constant))
            if "[red]" not in literal and "[bold red]" not in literal:
                continue
            interpolated = any(isinstance(p, ast.FormattedValue) for p in parts)
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
