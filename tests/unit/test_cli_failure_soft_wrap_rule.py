"""Enforcement for the CLI failure-line soft-wrap rule (#758 follow-up 1).

#750 and #758 each hand-fixed a batch of CLI failure lines by adding
``soft_wrap=True`` to a Rich ``console.print`` call, so a long message
(one carrying a path or an id an operator will copy) prints as one line
instead of being hard-wrapped at the console width, which splits the
handle mid-token. Each PR was a hand-enumerated batch, and each left
same-shaped lines beside the ones it fixed — #750 left ``admin.py:1688``,
#758 left four more in the very function it was editing. The #758 gate's
own AST scan of ``src/trellis_cli`` found 49 such lines still missing the
kwarg. Nothing before this rule stops a 50th: the existing behavioural
test (``tests/unit/cli/test_failure_lines_do_not_wrap.py``) enumerates 20
sites by hand and proves only that the kwarg does what it says on those
20, the same shape of under-coverage that let the first 49 accumulate.

So the rule:

    Every ``*console.print(...)`` call whose f-string argument carries
    ``[red]`` or ``[bold red]`` markup and at least one interpolation,
    and after which a non-zero ``typer.Exit``/``SystemExit`` is raised in
    the same statement list or an enclosing one, passes ``soft_wrap=True``.

The scan (:func:`tests.ast_rules.cli_failure_exit_sites`) is independent
of this test file on purpose — three separately-authored implementations
(the #758 gate's own probe script, this module's ``tests/ast_rules.py``
addition, and a from-scratch recount done while writing this test) agree
on the same 74-site population, which is the cross-check the project's
own house standard asks for rather than a claim resting on one scan.

**Scope, deliberately.** A red line with no exit after it is not a
*failure* line (the gate counted 29 such lines, out of scope). A receiver
not literally named ``*console`` — ``analyze.py``'s ``out.print`` — is
outside the scan's receiver check and stays outside (#758 follow-up 4,
pinning console width, is a separate concern). An exit of code 0, or a
bare ``typer.Exit()``/``SystemExit()`` with no code (which exits 0), does
not make the preceding line a failure line either — the rule's business
is the operator-facing failure path, not every red string in the CLI.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.ast_rules import (
    CallSite,
    assert_hand_read_floor,
    cli_failure_exit_sites,
)

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "trellis_cli"

#: Hand-read floor for the full population `cli_failure_exit_sites` finds
#: (wrapped and unwrapped alike) under src/trellis_cli, cross-checked three
#: ways (see the module docstring): the #758 gate's own probes/g758/scan.py
#: copy, this rule's own from-scratch tests/ast_rules.py scan, and a third,
#: separately-written recount performed while authoring this test. All
#: three agree on 74. Never computed from the scan this test exercises.
HAND_READ_FAILURE_LINE_COUNT = 74

#: Shrink-only roster: a failure line deliberately left without
#: soft_wrap=True, each with a one-line prose reason. Keyed by
#: "relpath:literal-snippet" rather than line number, so a later edit that
#: moves the line without touching its message cannot silently invalidate
#: or orphan an entry (the same reason tests/unit/test_governed_write_rule.py
#: keys its roster on (module, kind) rather than a line number). Empty is
#: the goal, and this sweep reaches it: every one of the 49 sites the gate
#: found has been fixed in this PR, so there is nothing to exempt.
UNWRAPPED_FAILURE_LINE_EXEMPTIONS: dict[str, str] = {}


def _has_soft_wrap(call: ast.Call) -> bool:
    return any(
        keyword.arg == "soft_wrap"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def _literal_snippet(call: ast.Call) -> str:
    """The f-string's literal text (interpolations stripped), first 40 chars.

    Stable under line movement and under a change to the *value* an
    interpolation carries, so reformatting the file cannot change a
    roster key; changing the message's own wording does, which is the
    point — an exemption is for *this* message, not for whatever text
    later replaces it.
    """
    if not call.args or not isinstance(call.args[0], ast.JoinedStr):
        return ""
    literal = "".join(
        value.value
        for value in call.args[0].values
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    )
    return literal[:40]


def _site_key(site: CallSite, root: Path) -> str:
    relpath = site.path.relative_to(root).as_posix()
    return f"{relpath}:{_literal_snippet(site.node)}"


def _unwrapped(root: Path) -> list[CallSite]:
    return [
        site for site in cli_failure_exit_sites(root) if not _has_soft_wrap(site.node)
    ]


def test_every_cli_failure_line_passes_soft_wrap() -> None:
    sites = cli_failure_exit_sites(SRC)
    assert_hand_read_floor(
        len(sites),
        HAND_READ_FAILURE_LINE_COUNT,
        subject="CLI red failure line (console.print + non-zero exit)",
        hint=(
            "Re-run the independent recount in this module's docstring and "
            "reconcile; if the tree really shrank, lower the floor with that "
            "evidence, don't just drop the assertion."
        ),
    )
    unexempted = [
        site.describe(SRC)
        for site in _unwrapped(SRC)
        if _site_key(site, SRC) not in UNWRAPPED_FAILURE_LINE_EXEMPTIONS
    ]
    assert not unexempted, (
        "CLI failure line(s) without soft_wrap=True (Rich hard-wraps a long "
        "line at the console width, splitting a path or id an operator will "
        "copy mid-token): " + "; ".join(unexempted)
    )


def test_unwrapped_failure_line_exemptions_are_exact() -> None:
    found = {_site_key(site, SRC) for site in _unwrapped(SRC)}
    declared = set(UNWRAPPED_FAILURE_LINE_EXEMPTIONS)
    assert found == declared, (
        "unwrapped CLI failure lines differ from the staging roster: "
        f"unrostered={sorted(found - declared)}; stale={sorted(declared - found)}"
    )
    thin = [
        key
        for key, reason in UNWRAPPED_FAILURE_LINE_EXEMPTIONS.items()
        if len(reason.split()) < 4
    ]
    assert not thin, f"soft-wrap exemptions need prose reasons: {thin}"


def test_the_scan_does_not_catch_out_print(tmp_path: Path) -> None:
    """``analyze.py``'s ``out.print`` is outside scope (#758 follow-up 4).

    Not a defect in the rule — a receiver not spelled ``*console`` is
    explicitly out of this PR's scope — but worth pinning so a future
    rename of the scan's receiver check does not silently widen it.
    """
    (tmp_path / "decoy.py").write_text(
        "import typer\n"
        "\n"
        "def run(path):\n"
        "    out.print(f'[red]Not found: {path}[/red]')\n"
        "    raise typer.Exit(code=1)\n",
        encoding="utf-8",
    )
    assert cli_failure_exit_sites(tmp_path) == []


def test_rule_discriminates_wrap_exit_code_and_receiver(tmp_path: Path) -> None:
    """The four shapes mutation testing exercises, as a synthetic fixture.

    One package, four files, each isolating a single axis the rule must
    get right:

    * ``compliant.py`` — red line, non-zero exit, already wrapped: not a
      violation.
    * ``missing.py`` — red line, non-zero exit, no ``soft_wrap``: a
      violation. This is the shape all 49 swept sites had.
    * ``no_exit.py`` — red line with nothing raised after it: collected by
      *no* rule here, because it is not a failure line (the gate's 29
      out-of-scope lines).
    * ``zero_exit.py`` — red line followed by ``typer.Exit()`` (bare, which
      exits 0) and by ``raise typer.Exit(code=0)``: neither counts as a
      failure exit, so neither line is collected even though both are
      unwrapped.
    """
    package = tmp_path / "src"
    package.mkdir()
    (package / "compliant.py").write_text(
        "import typer\n"
        "from trellis_cli.output import console\n"
        "\n"
        "def run(path):\n"
        "    console.print(f'[red]Not found: {path}[/red]', soft_wrap=True)\n"
        "    raise typer.Exit(code=1)\n",
        encoding="utf-8",
    )
    (package / "missing.py").write_text(
        "import typer\n"
        "from trellis_cli.output import console\n"
        "\n"
        "def run(path):\n"
        "    console.print(f'[bold red]Not found: {path}[/bold red]')\n"
        "    raise typer.Exit(code=1)\n",
        encoding="utf-8",
    )
    (package / "no_exit.py").write_text(
        "from trellis_cli.output import console\n"
        "\n"
        "def run(path):\n"
        "    console.print(f'[red]Warning about: {path}[/red]')\n"
        "    return None\n",
        encoding="utf-8",
    )
    (package / "zero_exit.py").write_text(
        "import typer\n"
        "from trellis_cli.output import console\n"
        "\n"
        "def run_bare(path):\n"
        "    console.print(f'[red]Bare exit after: {path}[/red]')\n"
        "    raise typer.Exit()\n"
        "\n"
        "def run_explicit_zero(path):\n"
        "    console.print(f'[red]Explicit zero after: {path}[/red]')\n"
        "    raise typer.Exit(code=0)\n",
        encoding="utf-8",
    )

    sites = cli_failure_exit_sites(package)
    descriptions = {site.describe(package) for site in sites}
    assert len(sites) == 2, descriptions
    assert any("compliant.py" in description for description in descriptions)
    assert any("missing.py" in description for description in descriptions)
    assert all("no_exit.py" not in description for description in descriptions)
    assert all("zero_exit.py" not in description for description in descriptions)

    unwrapped = _unwrapped(package)
    assert len(unwrapped) == 1
    assert "missing.py" in unwrapped[0].describe(package)
