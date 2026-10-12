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
(``ast.IfExp``), on either side of it.

It does not see an exit raised in a *different* function than the print
-- a helper that returns an ``Exit`` for its caller to raise
(``admin_api_keys.py``'s ``_store_error``), or one that only prints and
leaves the exit to its caller (extract_refresh.py's ``_print_backfill``)
-- nor a receiver not named ``*console``. Those lines stay out of the
hand-read floor below. A cross-function line that can print an unbounded
id, path or error text before a non-zero exit is listed by hand in
``CROSS_FUNCTION_FAILURE_LINES`` instead, and the second test checks it;
so is a yellow line of that kind whose exit is in its own function
(retrieve.py's ``Trace not found``), and so is a same-function line that
carries no colour tag at all (curate.py's ``_execute_command``, its
uncoloured ``Message:`` line) -- the red-only scan skips both. One that
interpolates only a fixed vocabulary or a count, or that cannot print on a
non-zero exit, is left off.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import CallSite, assert_hand_read_floor, iter_modules, name_of

SRC = Path(__file__).parents[2] / "src" / "trellis_cli"

#: 74 sites found by a tokenize recount written independently of this scan
#: (site-for-site equal to it at #766's head), less classify.py's shadow
#: summary, which exits ``EXIT_OK`` (73), plus 8 sites this widening makes
#: visible: admin.py:1533 and :1884, analyze.py:3130, curate.py:78 and
#: extract_refresh.py:565 (an exit under a later ``if``);
#: extract_refresh.py:539, ingest.py:448 and :524 (an ``IfExp`` red arm).
#: Lines whose exit is raised in a different function than the print stay
#: out of this count; see the module docstring. Counted outside this scan,
#: never computed by it.
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
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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


#: Hand count of the roster below, kept as its own literal rather than
#: ``len(CROSS_FUNCTION_FAILURE_LINES)`` -- the same shape as
#: ``HAND_READ_FAILURE_LINE_COUNT`` above, so a roster entry quietly
#: dropped (or renamed past what it matches) shrinks the *measured*
#: population below this floor instead of moving the floor with it.
HAND_READ_CROSS_FUNCTION_COUNT = 19

#: (file, function, message fragment) for each cross-function failure line:
#: a helper prints it in red or yellow, and a caller in the same file
#: raises a non-zero exit after the call -- a yellow line qualifies the
#: same way a red one does, and so does a line with no colour tag at all:
#: ``_named_function_print_calls`` collects every ``*console.print`` in the
#: named function regardless of colour or markup, and the fragment is what
#: narrows it to one. retrieve.py's ``trace`` and ``entity`` entries print
#: and exit in the same function; they are here because they are yellow,
#: which ``failure_line_sites`` does not scan. curate.py's
#: ``_execute_command`` entry is same-function too, and uncoloured.
#: extract_refresh.py's yellow ``~`` diff line is left off: a refused batch
#: commits nothing, so there is no diff to print before the exit. Its
#: ``_print_backfill`` per-trace row is uncoloured, and prints before
#: ``traces()``'s refusal exit anyway -- its entity/edge counts come from
#: extraction, computed before the batch is executed, so a row with a long
#: domain still prints when every write in the batch is denied. The
#: fragment tells apart entries that share a function: admin.py's
#: ``_render_smoke_text`` (two reds), ingest_conversations.py's
#: ``_render_report`` (its ``warning``/``withheld`` pair), and
#: ingest_corpus.py's ``_render_report`` (its ``prune``/``warning``/
#: ``withheld`` trio). admin_migrate_provenance.py's ``_print_text_report``
#: prints the batch's per-edge errors and returns; its caller now raises a
#: non-zero exit right after when ``report.errors`` is non-empty.
CROSS_FUNCTION_FAILURE_LINES = (
    ("admin.py", "_print_skills_summary", "failed"),
    ("admin.py", "_print_check_extractors_report", "not configurable from"),
    ("admin.py", "_render_smoke_text", "check['error']"),
    ("admin.py", "_render_smoke_text", "info['error']"),
    ("admin_api_keys.py", "_store_error", "store error"),
    ("admin_migrate_provenance.py", "_print_text_report", "escape(err)"),
    ("curate.py", "_execute_command", "Message:"),
    ("curate.py", "_print_warnings", "Warning:"),
    ("extract_refresh.py", "_print_backfill", "row['trace_id']"),
    ("extract_refresh.py", "_print_results", "first:"),
    ("ingest_conversations.py", "_render_report", "warning['kind']"),
    ("ingest_conversations.py", "_render_report", "withheld_name"),
    ("ingest_corpus.py", "_render_report", "pruned_name"),
    ("ingest_corpus.py", "_render_report", "warning['kind']"),
    ("ingest_corpus.py", "_render_report", "withheld_name"),
    ("policy.py", "_render_degradation", "POLICY STORE DEGRADED"),
    ("retrieve.py", "entity", "Entity not found"),
    ("retrieve.py", "trace", "Trace not found"),
    ("worker.py", "_render_embed_traces_text", "trace_id"),
)


def _named_function_print_calls(tree: ast.Module, function_name: str) -> list[ast.Call]:
    """Every ``*console.print(...)`` call lexically inside a function
    named *function_name*, anywhere in *tree*."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ):
            continue
        for child in ast.walk(node):
            if not (
                isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            ):
                continue
            receiver = name_of(child.func.value) or ""
            if child.func.attr == "print" and receiver.lower().endswith("console"):
                calls.append(child)
    return calls


def _cross_function_roster_matches(root: Path) -> list[CallSite]:
    """One ``CallSite`` per roster entry that resolves to exactly one
    ``*console.print`` call carrying its literal fragment, in the named
    function, in the named file.

    An entry that resolves to zero or more than one call is left out
    rather than raised here: the hand-read floor in the test below is
    what notices a roster that has quietly stopped matching, the same
    division of labour ``failure_line_sites`` has with its own floor.
    """
    modules = list(iter_modules(root))
    sites: list[CallSite] = []
    for file_name, function_name, fragment in CROSS_FUNCTION_FAILURE_LINES:
        for path, tree in modules:
            if path.name != file_name:
                continue
            calls = [
                call
                for call in _named_function_print_calls(tree, function_name)
                if fragment in ast.unparse(call)
            ]
            if len(calls) == 1:
                sites.append(CallSite(path=path, node=calls[0]))
    return sites


def test_cross_function_failure_lines_pass_soft_wrap() -> None:
    """Each roster entry resolves to exactly one print, and it passes
    ``soft_wrap=True``. Entries are found by function and message
    fragment, not by ``failure_line_sites``, which cannot see an exit
    raised in another function or a line that is not red."""
    sites = _cross_function_roster_matches(SRC)
    assert_hand_read_floor(
        len(sites),
        HAND_READ_CROSS_FUNCTION_COUNT,
        subject="cross-function CLI failure line",
        hint=(
            "Recount the cross-function failure lines under src/trellis_cli "
            "by hand before lowering this floor; a dropped or renamed entry "
            "should show up here as a shrunk population, not just a shorter "
            "tuple."
        ),
    )
    unwrapped = [site.describe(SRC) for site in sites if not _has_soft_wrap(site.node)]
    assert not unwrapped, (
        "cross-function CLI failure line(s) without soft_wrap=True; Rich "
        "hard-wraps them at the console width and splits the id or path "
        "they carry: " + "; ".join(unwrapped)
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
    """An exit raised inside a *later* sibling ``if`` rather than as a
    direct statement (``later_if.py``, the shape
    ``test_format_exit_parity_rule.py`` requires), and the red arm of a
    conditional expression, on either side of it (``ifexp_red_orelse.py``,
    ``ifexp_red_body.py``). A zero exit under a later ``if`` stays out, as
    ``zero_exit.py`` does above, and so does a raise inside a nested
    function, which is not on the print's path (``nested_def.py``)."""
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
        "nested_def.py": (
            "def run(path):\n"
            "    console.print(f'[red]Nested-def failure: {path}[/red]')\n"
            "\n"
            "    def _abort():\n"
            "        raise typer.Exit(code=1)\n"
            "\n"
            "    return _abort\n"
        ),
    }
    for name, body in files.items():
        (tmp_path / name).write_text(header + body, encoding="utf-8")

    sites = failure_line_sites(tmp_path)
    found = sorted(site.path.name for site in sites)
    assert found == ["ifexp_red_body.py", "ifexp_red_orelse.py", "later_if.py"], found
