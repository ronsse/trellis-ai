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
resolvable comparison on ``output_format``, it grades each arm's ability
to end the process non-zero — see :data:`_NO_EXIT` for the three levels —
and reports the two arms disagreeing. It does not try to show that two
arms exit under the *same conditions* — ``if a: exit(5)`` in one arm
against ``if b: exit(5)`` in the other passes, and making that decidable
is not tractable. The property it does enforce is the one #437 violated
and the one a reviewer cannot eyeball across a 2300-line module: an arm
from which non-zero exit is structurally unreachable, facing one from
which it is not — plus, since #491, the weaker asymmetry where the only
route to an exit runs through a helper that takes it on some paths and
returns on others.

Five deliberate refinements, each of which changes the answer on real
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
* **A call to a helper that can exit non-zero counts as an exit.**
  ``ingest._fail``, ``policy._exit_on_refused_write`` and
  ``analyze._exit_on_refused_advisory_write`` all render on the caller's
  surface and then exit below the branch; a command calling one of those
  from a single arm is a real divergence. Helpers are read at two
  strengths — see :data:`_NO_EXIT` — because a *must*-exit roster alone is
  not conservative in the way this file used to claim.
* **Helpers are resolved across modules** (#491). The roster used to be
  computed per file, inside :func:`_violations`' own ``rglob`` loop, so
  moving a must-exit helper into a shared module disarmed the rule for
  every command that imported it — an ordinary refactor, silently. Run
  over two synthetic trees carrying the identical divergence, the
  same-module one reported it and the cross-module one reported nothing.
  Resolution is *import-gated*, not global-by-name: a module sees a
  helper only when it imports the module or the name, and a local
  definition shadows an import.

The descent's completeness is itself pinned, by
:func:`test_the_descent_reaches_every_format_branch_in_the_tree`, which
counts the same branches with a dumb ``ast.walk`` and requires the two to
agree. That is how the ``ExceptHandler`` bug above was found — by hand,
once. None of the vacuity guards would have caught it: 123 clears a
floor of 100, and the resolvability ratio is taken over the branches the
descent found, so it read 123/123.

**The sweep found ``migrate-graph`` alone.** 148 format-conditioned
branches across 18 modules, one divergence. ``policy list`` /
``policy show`` are the counter-example worth naming: they raise
``EXIT_STORE`` from *inside* the JSON arm and again from the text path, so
both surfaces agree — the pattern was already understood in this
codebase, it just was not enforced anywhere.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tests.ast_rules import assert_hand_read_floor, construction_names, name_of

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

#: The mirror of that hazard, seeded for the same reason. A command that
#: only ever names ``"text"`` (``if output_format == "text": ... else:``)
#: would derive the singleton universe ``{"text"}``, leaving an empty
#: complement — and the reachability exemption, which exists to skip a
#: branch no supported format reaches, would skip the whole command
#: instead. ``json`` is not optional in this CLI: CLAUDE.md promises every
#: command supports it, so it is always in the universe whether or not the
#: function happens to mention it. Every function in ``trellis_cli`` today
#: names both, so this seeds nothing that is not already there; it is what
#: keeps that true.
_SEED_FORMATS = frozenset({DEFAULT_FORMAT, "json"})

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
    """Format values *func* distinguishes: every literal it compares, plus the seeds."""
    universe = set(_SEED_FORMATS)
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
        if name_of(node.exc.func) == "Exit":
            call = node.exc
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        name = name_of(node.value.func)
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


#: How able a block of statements is to end the process non-zero. Ordered,
#: and the rule reports **any** difference between an ``if``'s two arms
#: rather than only ``_NO_EXIT`` against something.
#:
#: The middle level is #491's third finding. Before it, a helper that
#: exits on some paths and returns on others was dropped from the roster
#: entirely, and this file justified that as *"the safe direction: it
#: drops a helper from the roster, and a dropped helper costs the loud
#: false positive, not the silent miss."* **That was false.** It holds
#: only when the *sibling* arm carries a real exit — then the dropped
#: helper's arm reads as non-exiting, the two disagree, and a reviewer
#: sees it. When the arm calling the dropped helper faces a sibling with
#: nothing, both read ``_NO_EXIT``, they agree, and the rule is silently
#: green. That is a miss, in the shape the rule exists to catch, and
#: ``worker._exit_if_advisory_write_refused`` (#481) is a shipped helper
#: of exactly that shape.
#:
#: Three levels make both directions loud instead. A conditional-exit
#: helper facing an empty sibling is ``1`` against ``0``; facing a direct
#: exit it is ``1`` against ``2``. Neither is a proof of divergence — the
#: rule is a reachability check and says so above — but neither is silent,
#: which is the property the old two-valued reading claimed and did not
#: have.
_NO_EXIT = 0
_CONDITIONAL_EXIT = 1
_REACHABLE_EXIT = 2

#: Rendered into the violation message, so a report says which of the
#: three asymmetries it found rather than always claiming "one arm only".
_LEVEL_NAMES = {
    _NO_EXIT: "cannot exit non-zero",
    _CONDITIONAL_EXIT: "exits only via a helper that exits on some paths",
    _REACHABLE_EXIT: "can exit non-zero",
}


def _statement_call_name(node: ast.AST) -> str | None:
    """Bare name of a call made as a *statement*, else ``None``.

    Statement position only, matching :func:`_exit_kind`: ``registry =
    _get_registry()`` binds a value and the exit inside it is incidental
    to the branch, while ``_exit_if_degraded(store, fmt)`` is written for
    its effect. Widening this to value position would pull
    ``stores._get_registry`` — imported by fifteen modules and called for
    its return value at 34 sites — into almost every arm.
    """
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        return name_of(node.value.func)
    return None


def _exit_level(
    node: ast.AST, must_exit: frozenset[str], may_exit: frozenset[str]
) -> int:
    """The strongest exit reachable from *node*."""
    level = _NO_EXIT
    for inner in ast.walk(node):
        if _exit_kind(inner, must_exit) == "nonzero":
            return _REACHABLE_EXIT
        name = _statement_call_name(inner)
        if name is not None and name in may_exit:
            level = _CONDITIONAL_EXIT
    return level


def _block_level(
    stmts: list[ast.stmt], must_exit: frozenset[str], may_exit: frozenset[str]
) -> int:
    return max((_exit_level(s, must_exit, may_exit) for s in stmts), default=_NO_EXIT)


def _terminates(stmts: list[ast.stmt]) -> bool:
    return bool(stmts) and isinstance(stmts[-1], (ast.Return, ast.Raise))


@dataclass(frozen=True)
class _Module:
    """One parsed module, keyed the way an import spells it.

    *key* is the dotted path below the scanned package — ``"policy"``,
    ``"skills"`` for ``skills/__init__.py``, ``""`` for the package's own
    ``__init__``. The
    first cut of #491 keyed on ``Path.stem``, which collided immediately:
    ``trellis_cli/__init__.py`` and ``trellis_cli/skills/__init__.py``
    both stem to ``__init__``, and two modules sharing a key would share
    a helper roster — an over-count, which is this rule's silent
    direction.
    """

    key: str
    path: Path
    tree: ast.Module
    is_package: bool


def _module_key(path: Path, root: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = bool(parts) and parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _containing_package(module: _Module, level: int) -> str | None:
    """Where a ``from .``-style import of depth *level* resolves to.

    Python's own rule: a module's depth-1 anchor is its containing
    package, a package's is itself.
    """
    parts = module.key.split(".") if module.key else []
    if not module.is_package and parts:
        parts.pop()
    for _ in range(level - 1):
        if not parts:
            return None
        parts.pop()
    return ".".join(parts)


def _join(prefix: str, tail: str) -> str:
    return f"{prefix}.{tail}" if prefix else tail


def _package_bindings(
    module: _Module, package: str, modules: Collection[str]
) -> set[tuple[str, str | None]]:
    """``(module key, imported name)`` for every in-package import in *module*.

    ``None`` for the name means the *module* itself was bound, so every
    helper it defines is reachable as ``module.helper(...)`` — which
    :func:`name_of` reads as the bare ``helper``, the same spelling a
    direct import produces.

    Import-gating is the whole point (#491). A roster keyed only on the
    helper's bare name would count *any* module's ``_fail`` as
    ``ingest._fail``, and over-counting a helper is this rule's silent
    direction: an arm that merely might exit standing in for one that
    does. So a binding has to be traceable to a module in the scanned
    tree, through the scanned package — ``from trellis_api.app import
    main`` must not admit ``trellis_cli/main.py``'s helpers into
    ``serve.py``, and before the *package* half of this check it did.
    """
    bound: set[tuple[str, str | None]] = set()

    def admit(source: str, names: list[ast.alias]) -> None:
        if source in modules:
            bound.update((source, alias.name) for alias in names)
        for alias in names:
            submodule = _join(source, alias.name)
            if submodule in modules:
                bound.add((submodule, None))

    for node in ast.walk(module.tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = _containing_package(module, node.level)
                if anchor is None:
                    continue
                admit(_join(anchor, node.module or ""), node.names)
                continue
            dotted = node.module or ""
            if dotted != package and not dotted.startswith(f"{package}."):
                continue
            admit(dotted[len(package) :].lstrip("."), node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith(f"{package}."):
                    continue
                key = alias.name[len(package) :].lstrip(".")
                if key in modules:
                    bound.add((key, None))
    return bound


def _visible_helpers(
    module: _Module,
    package: str,
    must: Mapping[str, set[str]],
    may: Mapping[str, set[str]],
) -> tuple[frozenset[str], frozenset[str]]:
    """The helper names *module* can actually call, own and imported.

    Alias resolution is :func:`tests.ast_rules.construction_names`, not a
    second copy of it: ``from .shared import _fail as _die`` and ``_die =
    _fail`` both have to reach the roster, and that resolver already
    follows both to a fixed point. #490's finding was a harness declaring
    three shapes unresolvable that this function had already solved.
    """
    own_must, own_may = must[module.key], may[module.key]
    visible_must, visible_may = set(own_must), set(own_may)
    for source, imported in _package_bindings(module, package, must):
        if source == module.key:
            continue
        names = {imported} if imported is not None else must[source] | may[source]
        for name in names:
            if name in must[source]:
                visible_must |= construction_names(name, module.tree)
            if name in may[source]:
                visible_may |= construction_names(name, module.tree)
    # A module's own definition wins over anything it imported under the
    # same name: ``def _fail(...): return None`` beside ``from .shared
    # import _fail`` is not the shared helper, and reading it as one is
    # the over-count above.
    shadowed = {func.name for func in _functions(module.tree)} - own_must - own_may
    return frozenset(visible_must - shadowed), frozenset(visible_may - shadowed)


@dataclass(frozen=True)
class _ExitRoster:
    """Which functions each module can exit through, own and imported.

    ``must`` / ``may`` are keyed by defining module; ``visible_must`` /
    ``visible_may`` by *calling* module, and are what :func:`_violations`
    reads. The two differ exactly where a helper crosses a module
    boundary, which is what #491 was filed about.
    """

    must: Mapping[str, frozenset[str]]
    may: Mapping[str, frozenset[str]]
    visible_must: Mapping[str, frozenset[str]]
    visible_may: Mapping[str, frozenset[str]]

    def imported(self, key: str) -> frozenset[str]:
        """Helper names *key* can reach only because it imported them."""
        return frozenset(
            (self.visible_must[key] | self.visible_may[key])
            - self.must[key]
            - self.may[key]
        )


def _build_exit_roster(modules: Sequence[_Module], package: str) -> _ExitRoster:
    """Classify every function in *modules*, to a fixed point across files.

    ``must`` is unchanged from the per-module version: the last statement
    is a non-zero exit and no ``return`` is reachable under the function.
    Deliberately still an under-approximation — an over-read ``must`` lets
    an arm that might exit stand in for one that does, and nothing
    reports it.

    What is new is that the residue is no longer discarded. A function
    with *any* reachable non-zero exit lands in ``may``, so the helpers
    the ``return`` rule drops — ``policy._exit_if_degraded``,
    ``analyze._exit_if_advisory_store_degraded``,
    ``worker._exit_if_advisory_write_refused`` — are visible to the rule
    at :data:`_CONDITIONAL_EXIT` instead of being invisible at
    :data:`_NO_EXIT`.

    The loop is global rather than per module because a helper's
    classification can depend on one it imports, and that dependency
    crosses files. Say what it is on this tree rather than implying more:
    **no ``trellis_cli`` function is currently classified differently
    because of a cross-module binding.** The one in-package helper import
    the machinery reads is ``worker``'s
    ``analyze._build_learning_registry_or_exit``, and ``worker`` calls it
    in value position, which :func:`_statement_call_name` deliberately
    does not read. The global loop is here for the consolidation #491 was
    filed about, and
    :func:`test_helpers_resolve_across_module_boundaries` is where it is
    demonstrated. Growth is monotone in both sets, so the fixed point
    terminates.
    """
    must: dict[str, set[str]] = {module.key: set() for module in modules}
    may: dict[str, set[str]] = {module.key: set() for module in modules}
    changed = True
    while changed:
        changed = False
        for module in modules:
            visible_must, visible_may = _visible_helpers(module, package, must, may)
            for func in _functions(module.tree):
                if not func.body:
                    continue
                if func.name not in may[module.key] and (
                    _exit_level(func, visible_must, visible_may) != _NO_EXIT
                ):
                    may[module.key].add(func.name)
                    changed = True
                if func.name in must[module.key]:
                    continue
                if _exit_kind(func.body[-1], visible_must) != "nonzero":
                    continue
                # Any ``return`` under the function keeps it out of
                # ``must``, including one inside a closure it defines,
                # which cannot actually return past the exit. The
                # over-strictness now costs a demotion to
                # :data:`_CONDITIONAL_EXIT` rather than a disappearance.
                if any(isinstance(inner, ast.Return) for inner in ast.walk(func)):
                    continue
                must[module.key].add(func.name)
                changed = True
    visible = {
        module.key: _visible_helpers(module, package, must, may) for module in modules
    }
    return _ExitRoster(
        must={key: frozenset(names) for key, names in must.items()},
        may={key: frozenset(names) for key, names in may.items()},
        visible_must={key: pair[0] for key, pair in visible.items()},
        visible_may={key: pair[1] for key, pair in visible.items()},
    )


def _parse_tree(root: Path) -> list[_Module]:
    """Every module under *root*, in path order, keyed as an import spells it."""
    modules: list[_Module] = []
    for path in sorted(root.rglob("*.py")):
        key, is_package = _module_key(path, root)
        modules.append(
            _Module(
                key=key,
                path=path,
                tree=ast.parse(path.read_text(encoding="utf-8")),
                is_package=is_package,
            )
        )
    return modules


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


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
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

    The whole tree is parsed before any of it is scanned, because the
    helper roster is cross-module (#491). Building the roster inside the
    file loop is what made the interprocedural half see only same-module
    helpers.
    """
    cli_root = root if root is not None else _cli_root()
    modules = _parse_tree(cli_root)
    roster = _build_exit_roster(modules, cli_root.name)
    found: list[str] = []
    for module in modules:
        must = roster.visible_must[module.key]
        may = roster.visible_may[module.key]
        for func in _functions(module.tree):
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
                elif _terminates(body) or _block_level(body, must, may) != _NO_EXIT:
                    # A body that terminates hands its sibling the code
                    # after the branch (the #422 shape). A body that merely
                    # *falls through* normally makes the two arms agree —
                    # they meet again below — but not if it can exit
                    # non-zero on the way there. Skipping that case because
                    # "control merges" reads the merge and ignores the
                    # branch's own exit, which is the divergence.
                    sibling = block[index + 1 :]
                else:
                    # Control merges; whatever follows applies to both arms.
                    continue
                body_level = _block_level(body, must, may)
                sibling_level = _block_level(sibling, must, may)
                if body_level != sibling_level:
                    found.append(
                        f"{module.path.name}:{branch.lineno}: {func.name}() — "
                        f"'{ast.unparse(branch.test)}': the matched arm "
                        f"{_LEVEL_NAMES[body_level]}, its sibling "
                        f"{_LEVEL_NAMES[sibling_level]}"
                    )
    return found


@functools.lru_cache(maxsize=1)
def _real_modules() -> tuple[_Module, ...]:
    """``src/trellis_cli``, parsed once for every guard in this file."""
    return tuple(_parse_tree(_cli_root()))


@functools.lru_cache(maxsize=1)
def _real_roster() -> _ExitRoster:
    return _build_exit_roster(list(_real_modules()), _cli_root().name)


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
    assert_hand_read_floor(
        branches,
        100,
        subject="format-conditioned branch",
        hint=(
            "148 by hand at 8ec879c. A drop here means output_format was "
            "renamed, the package moved, or --format became a callback — "
            "each of which leaves this file green and enforcing nothing."
        ),
    )
    assert resolvable > 0.9 * branches, (
        f"only {resolvable}/{branches} format tests are resolvable; the scan "
        f"skips what it cannot read, so a drop here is silent under-coverage"
    )


def test_the_descent_reaches_every_format_branch_in_the_tree() -> None:
    """Completeness guard: the hand-rolled descent skips no node type.

    This is the check that caught the one defect the scan has actually
    had. :func:`_format_branches` walks by hand, and its first version
    descended only ``ast.stmt`` — ``ast.ExceptHandler`` is not one, so it
    silently skipped every ``except`` block: 25 of the 148 branches, and
    the place a command is most likely to branch on format while deciding
    *how* to fail. It reported 123 and looked complete.

    Nothing shipped would have said so. 123 clears the branch floor, and
    the resolvability ratio is computed over the branches the descent
    *found*, so it read 123/123 — a metric wired to the population it was
    meant to be auditing. The bug was caught by hand, once, by counting
    the same thing a second way; this ships that second count.

    ``ast.walk`` is the right oracle precisely because it is dumb: it
    visits every node reachable from the module with no notion of which
    ones are statements, so it cannot inherit the descent's blind spots.
    A mismatch means one of two things, both real: a node type the
    descent does not enter (``match_case`` bodies, a future
    ``ExceptHandler``-shaped addition), or a format branch outside any
    function, which :func:`_violations` never scans because it iterates
    :func:`_functions`.
    """
    by_descent = 0
    by_walk = 0
    unreached: list[str] = []
    for py_file in sorted(_cli_root().rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        seen = {
            id(branch)
            for func in _functions(tree)
            for _block, _index, branch in _format_branches(func)
        }
        by_descent += len(seen)
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _mentions_format(node.test):
                by_walk += 1
                if id(node) not in seen:
                    unreached.append(f"{py_file.name}:{node.lineno}")

    assert_hand_read_floor(
        by_walk,
        100,
        subject="format-conditioned branch, counted by ast.walk",
        hint="the oracle itself has drifted; 148 by hand at 8ec879c.",
    )
    assert not unreached, (
        f"the structural descent never reached {len(unreached)} format "
        f"branch(es); it is skipping a node type or a module-level branch, "
        f"which under-reports silently and clears every other guard here: "
        f"{unreached}"
    )
    assert by_descent == by_walk, (
        f"the descent reached {by_descent} branches against {by_walk} by "
        f"walk with none missed, so it is counting something twice"
    )


def test_the_must_exit_helper_set_is_exactly_the_must_exit_helpers() -> None:
    """Vacuity guard, for the interprocedural half of the scan.

    ``ingest._fail``, ``policy._exit_on_refused_write``,
    ``analyze._exit_on_refused_advisory_write`` and
    ``main._render_boundary_failure`` are the shape: render on the
    caller's surface, then exit below the branch. If this set silently
    emptied, every command that delegates its exit to a helper would stop
    being checked, and nothing else in this file would notice.

    Pinned as an *equality*, not a membership test, because this set is
    wrong in both directions and only one of them is loud. A missing
    helper costs a demotion to :data:`_CONDITIONAL_EXIT`, which is
    reported against either sibling. An extra one — a ``may``-exit helper
    counted as ``must`` — lets an arm that might exit stand in for one
    that does, and nothing reports it. Naming all four also keeps the
    roster *derived*: it is recomputed from the tree here, so a new helper
    has to be admitted deliberately rather than inherited from a
    hand-maintained list that drifts (the #443 shape). The fourth entry
    arrived that way: #459's boundary was written, the roster went red,
    and admitting it was a decision rather than an inheritance.

    Keyed by *defining* module. Since #491 a helper is also visible to
    every module that imports it, which is
    :func:`test_helpers_resolve_across_module_boundaries_in_trellis_cli`'s
    subject, not this one's.
    """
    roster = _real_roster()
    helpers_by_module = {key: set(names) for key, names in roster.must.items() if names}
    assert helpers_by_module == {
        "analyze": {"_exit_on_refused_advisory_write"},
        "ingest": {"_fail"},
        # The fourth is the shared boundary #459 added: same shape as the
        # other three (render on the caller's surface, then exit below the
        # format branch), reached from ``_BoundaryGroup.invoke`` rather
        # than from a command body. Admitted deliberately, per this
        # docstring — it exits on every path and defines no ``return``.
        "main": {"_render_boundary_failure"},
        "policy": {"_exit_on_refused_write"},
    }, (
        f"the must-exit helper roster changed: {helpers_by_module}. An "
        f"addition is fine if the helper really exits on every path; a "
        f"may-exit helper here is a silent false negative for every arm "
        f"that calls it."
    )


def test_the_conditional_exit_helper_roster_is_exactly_the_private_may_exiters() -> (
    None
):
    """The middle level is populated, and by the helpers #491 named.

    This is the roster the third finding turns on. Before it, every one of
    these was simply absent from the scan's world: a ``return`` anywhere
    under the function dropped it, and an arm whose only exit ran through
    one read as :data:`_NO_EXIT` — identical to an arm that cannot exit at
    all. ``worker._exit_if_advisory_write_refused`` (#481),
    ``policy._exit_if_degraded`` and
    ``analyze._exit_if_advisory_store_degraded`` are the three the issue
    names, and they are here.

    Restricted to *private* names because the public ones are Typer
    command bodies, which reach a non-zero exit as a matter of course and
    say nothing about whether the middle level works. That restriction is
    a derived predicate (``name.startswith("_")``), not a hand-kept list,
    so a new private helper has to be admitted deliberately — the same
    #443-avoidance the must-exit roster above uses.
    """
    roster = _real_roster()
    conditional = {
        key: sorted(
            name for name in roster.may[key] - roster.must[key] if name.startswith("_")
        )
        for key in roster.may
    }
    assert {k: v for k, v in conditional.items() if v} == {
        "admin": ["_load_graph_store_from_yaml", "_lookup_candidate_payload"],
        "analyze": [
            "_build_learning_registry_or_exit",
            "_exit_if_advisory_store_degraded",
        ],
        "classify": ["_require_llm_facet_classifier"],
        "ingest_corpus": ["_parse_tags"],
        "policy": ["_exit_if_degraded", "_refuse_stale"],
        "stores": ["_get_registry"],
        "worker": [
            "_build_auto_promote_policy_or_exit",
            "_exit_if_advisory_write_refused",
            "_require_llm_client_or_exit",
        ],
    }, (
        f"the conditional-exit helper roster changed: "
        f"{ {k: v for k, v in conditional.items() if v} }. An empty one "
        f"puts the rule back where #491 found it, with every "
        f"exit-on-some-paths helper invisible in both arms."
    )


def test_the_rule_reasons_over_a_real_population_of_helper_call_sites() -> None:
    """Hand-read floors for both halves of the interprocedural scan.

    Counted by hand off ``src/trellis_cli`` on 2026-09-04, at
    ``8ec879c``. **Must-exit call sites: 10.** **Conditional-exit call
    sites: 18** — six of them the helpers above (``policy._exit_if_degraded``
    twice, ``policy._refuse_stale``, ``analyze._exit_if_advisory_store_degraded``,
    ``worker._exit_if_advisory_write_refused`` twice) and twelve
    command-layer calls: the five ``admin`` sub-command ``register``
    hooks, six Typer wrappers delegating to a ``*_command`` body, and one
    ``store.create(record)`` that bare-name matching reads as
    ``admin_api_keys``' own ``create`` command — the over-collection
    :func:`~tests.ast_rules.name_of` documents, a same-named method on an
    unrelated object. All twelve are conditional-exit for a real reason (a
    command body exits) and none sits in a format arm, so none of them
    costs anything.

    Both floors sit below the counted number so that adding a call site is
    ordinary and *removing the scan's ability to see them* is not. They
    are arguments rather than computations for the reason
    :func:`~tests.ast_rules.assert_hand_read_floor` exists: #466's floor
    was ``len(SITES) > 0``, satisfied by three blind spots, and a floor
    divided out of the scan's own output is satisfied by a scan that
    merely shrank.
    """
    roster = _real_roster()
    must_sites = 0
    conditional_sites = 0
    for module in _real_modules():
        must = roster.visible_must[module.key]
        conditional = roster.visible_may[module.key] - must
        for node in ast.walk(module.tree):
            name = _statement_call_name(node)
            if name is None:
                continue
            must_sites += name in must
            conditional_sites += name in conditional

    assert_hand_read_floor(
        must_sites,
        8,
        subject="must-exit helper call",
        hint=(
            "10 by hand at 8ec879c. A collapse here means _exit_kind has "
            "stopped recognising helper calls, and every command that "
            "delegates its exit stops being checked."
        ),
    )
    assert_hand_read_floor(
        conditional_sites,
        12,
        subject="conditional-exit helper call",
        hint=(
            "18 by hand at 8ec879c. A collapse here restores #491's "
            "third finding: an arm whose only exit runs through a "
            "may-exit helper reading as an arm that cannot exit."
        ),
    )


def test_helpers_resolve_across_module_boundaries_in_trellis_cli() -> None:
    """Cross-module resolution is *live* on the real tree, not just synthetic.

    #491's fix cannot be measured by the violation count, and that is the
    honest awkward part: ``trellis_cli`` currently keeps each must-exit
    helper in the module that uses it, so resolving imports changes zero
    violations today. The rule is for the refactor that has not happened
    — the one a cross-PR review recommended *against* precisely because
    this scan would have gone blind to it.

    What can be measured is that the resolver actually resolves. Counted
    by hand off ``src/trellis_cli`` on 2026-09-04: **25 imported helper
    bindings** — ``stores._get_registry`` into fifteen modules, six
    ``register`` aliases into ``admin``, ``ingest_conversations`` and
    ``ingest_corpus`` into ``ingest``, ``ingest_corpus._parse_tags`` into
    ``ingest_conversations``, and
    ``analyze._build_learning_registry_or_exit`` into ``worker``. Floored
    below that so an import moving is ordinary; a *zero* here means the
    package check, the relative-import anchor or the alias resolution has
    silently stopped matching, and the interprocedural half is back to
    per-file with nothing else in this file noticing.
    """
    roster = _real_roster()
    imported = {
        module.key: sorted(roster.imported(module.key))
        for module in _real_modules()
        if roster.imported(module.key)
    }
    assert_hand_read_floor(
        sum(len(names) for names in imported.values()),
        20,
        subject="cross-module helper binding",
        hint=(
            f"25 by hand at 8ec879c; found {imported}. Zero means "
            f"_package_bindings resolves nothing — check that the "
            f"package name is still the root directory's."
        ),
    )
    assert "_exit_if_advisory_write_refused" not in roster.imported("worker"), (
        "worker *defines* that helper, so it is not an imported binding. "
        "Counting it as one would make the floor above a count of every "
        "visible helper — which a resolver that resolves nothing still "
        "satisfies, and the floor would then be measuring the wrong "
        "population (#466's defect one level up)."
    )
    # Named, not just counted: a floor is satisfied by any 20 bindings,
    # and these two are the ones whose *source* module classified them as
    # able to exit. Everything else the resolver admits is a plain
    # function that happens to cross a boundary.
    assert "_build_learning_registry_or_exit" in roster.visible_may["worker"], (
        "worker imports analyze._build_learning_registry_or_exit, a "
        "conditional-exit helper, so it has to be visible in worker."
    )
    assert "_get_registry" in roster.visible_may["curate"], (
        "stores._get_registry is a conditional-exit helper imported by "
        "fifteen modules; if it is not visible in one of them the "
        "name-level import branch has stopped resolving."
    )


#: Every shape the rule must catch, plus the six it must leave alone. The
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


def _may_exit(result, output_format):
    if result.ok:
        return
    raise typer.Exit(code=EXIT_STORE)


def conditional_helper_faces_an_arm_that_cannot_exit(output_format, result):
    if output_format == "json":                  # 116 #491's third finding
        emit_json(result)
    else:
        _may_exit(result, output_format)


def conditional_helper_faces_a_direct_exit(output_format, result):
    if output_format == "json":                  # 123 the loud half, kept loud
        emit_json(result)
        raise typer.Exit(code=EXIT_STORE)
    _may_exit(result, output_format)


def both_arms_reach_the_same_conditional_helper(output_format, result):
    if output_format == "json":                  # 130 ALLOWED — arms agree
        emit_json(result)
        _may_exit(result, output_format)
    else:
        console.print(result)
        _may_exit(result, output_format)


def direct_exit_faces_a_must_exit_helper(output_format, report):
    if output_format == "json":                  # 139 ALLOWED — both arms exit
        emit_json(report)
        raise typer.Exit(code=EXIT_STORE)
    _always_exits("failed", output_format)
"""

#: Lines :data:`_SHAPES` must report. 69, 80, 89, 92, 130 and 139 are
#: deliberately absent — 69, 80, 130 and 139 are correct code (both arms
#: agree, at each of the three levels and across the two spellings of a
#: must-exit one), and 89 is an argument validator whose raising arm no
#: rendered format can reach.
_EXPECTED_SHAPE_LINES = [15, 24, 31, 39, 47, 55, 62, 102, 116, 123]


def test_the_scan_catches_every_known_shape(tmp_path: Path) -> None:
    """Mutation guard, run through the shipped scanner rather than a copy.

    Every part of the scan is load-bearing, measured by stubbing each one
    out and re-running this list rather than by assertion:

    ==================================  ==============================
    weakening                           result
    ==================================  ==============================
    no fallthrough sibling              loses 24, 31, 47, 55, 123
    ``sys.exit`` not counted            loses 39
    ``!=`` / ``not in`` polarity gone   loses 55
    no must-exit helper set             **gains 139**
    no conditional-exit level           loses 116
    levels read as a boolean            loses 123
    ``EXIT_OK`` counted as a failure    loses 47
    no reachability exemption           **gains 89**, a false positive
    descent stops at ``ast.stmt``       loses 102
    no cross-module resolution          **no effect here**
    ==================================  ==============================

    Two rows are worth reading rather than skimming.

    **The must-exit set no longer earns its keep by catching 62.** Since
    #491 a must-exit helper is also a conditional-exit one — ``must`` is a
    subset of ``may`` — so deleting the must-exit branch demotes line 62
    from :data:`_REACHABLE_EXIT` to :data:`_CONDITIONAL_EXIT` and it is
    still reported, against a sibling at :data:`_NO_EXIT`. What the
    distinction buys is 139, correct code that the weakened scan reports
    as a false positive. That is the whole reason the roster is still
    read at two strengths, and the reason this corpus grew a shape whose
    two arms are *allowed* to differ in how they spell the same exit.

    **The last row is honest, not a gap.** ``_SHAPES`` is a single file,
    so cross-module resolution cannot change its answer by construction.
    That property has its own corpus —
    :func:`test_helpers_resolve_across_module_boundaries` — and its own
    negative controls, because the failure mode there is over-resolution
    rather than under-.

    The ``ast.stmt`` row is the scan's own historical bug, and it is the
    one this list cannot be trusted alone for: on the real tree it drops
    148 branches to 123 while every assertion in this file still passes.
    That is why
    :func:`test_the_descent_reaches_every_format_branch_in_the_tree`
    exists.
    """
    (tmp_path / "shapes.py").write_text(_SHAPES.lstrip("\n"), encoding="utf-8")

    reported = sorted(int(v.split(":")[1]) for v in _violations(root=tmp_path))
    assert reported == _EXPECTED_SHAPE_LINES, (
        f"scanner reported {reported}, expected {_EXPECTED_SHAPE_LINES}; "
        f"missing={sorted(set(_EXPECTED_SHAPE_LINES) - set(reported))} "
        f"spurious={sorted(set(reported) - set(_EXPECTED_SHAPE_LINES))}"
    )


#: Package name the cross-module corpus is rendered under. It is the
#: directory name, and :func:`_violations` derives the package it will
#: resolve absolute imports against from exactly that — so the constant
#: and the ``from clipkg.shared import ...`` lines below have to agree, and
#: :func:`test_helpers_resolve_across_module_boundaries` checks that they
#: do rather than trusting the two spellings to stay in step.
_CROSS_PACKAGE = "clipkg"

#: Marks an ``if`` the scan **must** report, by shape id.
_REPORT_MARKER = "#!report:"

#: Marks an ``if`` the scan must **never** report. Four of them, and they
#: are the half that makes the other half mean something: without a
#: negative control, "every marked line was reported" is satisfied by a
#: predicate that resolves every bare name globally — which is the
#: over-count that costs this rule a silent miss, not a loud one.
_SILENT_MARKER = "#!silent:"

_CROSS_MODULE_CORPUS: dict[str, str] = {
    "__init__.py": "",
    "shared.py": """
import typer


def _always_exits(message, output_format):
    if output_format == "json":
        emit_json({"status": "error", "message": message})
    else:
        console.print(message)
    raise typer.Exit(code=5)


def _may_exit(result, output_format):
    if result.ok:
        return
    console.print("refused")
    raise typer.Exit(code=5)
""",
    # The regression control. #491 is about the cross-module case, and a
    # fix that traded the same-module case for it would be no fix at all.
    "same_module.py": """
import typer


def _local_always_exits(message, output_format):
    console.print(message)
    raise typer.Exit(code=5)


def cmd(output_format, report):
    if output_format == "json":  #!report:same_module
        emit_json(report)
    else:
        _local_always_exits("failed", output_format)
""",
    "absolute_import.py": """
from clipkg.shared import _always_exits


def cmd(output_format, report):
    if output_format == "json":  #!report:absolute_import
        emit_json(report)
    else:
        _always_exits("failed", output_format)
""",
    "aliased_relative_import.py": """
from .shared import _always_exits as _die


def cmd(output_format, report):
    if output_format == "json":  #!report:aliased_relative_import
        emit_json(report)
    else:
        _die("failed", output_format)
""",
    "module_qualified.py": """
from . import shared


def cmd(output_format, report):
    if output_format == "json":  #!report:module_qualified
        emit_json(report)
    else:
        shared._always_exits("failed", output_format)
""",
    "rebound.py": """
from clipkg.shared import _always_exits

_bail = _always_exits


def cmd(output_format, report):
    if output_format == "json":  #!report:rebound
        emit_json(report)
    else:
        _bail("failed", output_format)
""",
    # #491's third finding, one module further away than the issue put it:
    # a helper that exits on some paths, called from the arm whose sibling
    # carries nothing at all.
    "conditional_import.py": """
from clipkg.shared import _may_exit


def cmd(output_format, result):
    if output_format == "json":  #!report:conditional_import
        emit_json(result)
    else:
        _may_exit(result, output_format)
""",
    # A helper living in a *package* ``__init__``. ``from clipkg.sub
    # import _sub_always_exits`` resolves only if the ``__init__`` is
    # keyed as the package it is, and a mutant that keys it
    # ``sub.__init__`` survives every other shape here.
    "sub/__init__.py": """
import typer


def _sub_always_exits(message, output_format):
    console.print(message)
    raise typer.Exit(code=5)
""",
    "sub/nested.py": """
from clipkg.shared import _always_exits


def cmd(output_format, report):
    if output_format == "json":  #!report:nested_package
        emit_json(report)
    else:
        _always_exits("failed", output_format)
""",
    # ``ast.Import`` rather than ``ast.ImportFrom`` — a separate branch of
    # _package_bindings, and one no shape reached until a mutant that
    # deleted it survived everything else here.
    "plain_import.py": """
import clipkg.shared
import clipkg.shared as sh


def dotted(output_format, report):
    if output_format == "json":  #!report:plain_import
        emit_json(report)
    else:
        clipkg.shared._always_exits("failed", output_format)


def aliased(output_format, report):
    if output_format == "json":  #!report:aliased_module_import
        emit_json(report)
    else:
        sh._always_exits("failed", output_format)
""",
    "package_init_helper.py": """
from clipkg.sub import _sub_always_exits


def cmd(output_format, report):
    if output_format == "json":  #!report:package_init_helper
        emit_json(report)
    else:
        _sub_always_exits("failed", output_format)
""",
    # ---- negative controls ------------------------------------------
    # Nothing imports the helper here. A resolver keyed on the bare name
    # would report it, and that over-count is the direction that ends in a
    # silent miss elsewhere.
    "never_imported.py": """
def cmd(output_format, report):
    if output_format == "json":  #!silent:never_imported
        emit_json(report)
    else:
        _always_exits("failed", output_format)
""",
    # A local definition wins over the import above it.
    "shadowed.py": """
from clipkg.shared import _always_exits


def _always_exits(message, output_format):
    console.print(message)
    return None


def cmd(output_format, report):
    if output_format == "json":  #!silent:shadowed
        emit_json(report)
    else:
        _always_exits("failed", output_format)
""",
    # The ``serve.py`` shape: ``from trellis_api.app import main`` must not
    # admit ``trellis_cli/main.py``'s helpers. Before the package half of
    # _package_bindings it did, on the real tree.
    "foreign_package.py": """
from otherpkg.shared import _always_exits


def cmd(output_format, report):
    if output_format == "json":  #!silent:foreign_package
        emit_json(report)
    else:
        _always_exits("failed", output_format)
""",
    # Resolution has to leave *correct* code alone too, or the rule is a
    # ban on importing a helper.
    "both_arms.py": """
from clipkg.shared import _always_exits


def cmd(output_format, report):
    if output_format == "json":  #!silent:both_arms
        emit_json(report)
        _always_exits("failed", output_format)
    else:
        console.print(report)
        _always_exits("failed", output_format)
""",
}


def _marked_lines(marker: str) -> dict[str, str]:
    """``{shape id: "file.py:lineno"}`` for every *marker* in the corpus."""
    found: dict[str, str] = {}
    for name, source in _CROSS_MODULE_CORPUS.items():
        for offset, line in enumerate(source.lstrip("\n").splitlines(), start=1):
            index = line.find(marker)
            if index < 0:
                continue
            shape = line[index + len(marker) :].strip()
            assert shape not in found, f"duplicate shape id {shape!r}"
            found[shape] = f"{Path(name).name}:{offset}"
    return found


def _write_cross_module_corpus(tmp_path: Path) -> Path:
    root = tmp_path / _CROSS_PACKAGE
    for name, source in _CROSS_MODULE_CORPUS.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.lstrip("\n"), encoding="utf-8")
    return root


def test_helpers_resolve_across_module_boundaries(tmp_path: Path) -> None:
    """The #491 fix, run through the shipped scanner over a synthetic tree.

    The plan review's measurement is the shape of this test: it ran the
    shipped ``_violations`` over two trees carrying the *identical*
    divergence and got ``1`` for the same-module one and ``0`` for the
    cross-module one. Both are here, alongside every spelling a real
    consolidation would produce: an absolute import, a relative import
    under an alias, ``from . import shared`` with a module-qualified
    call, ``import clipkg.shared`` plain and aliased, a local rebinding, a
    submodule one package deep, a helper defined in a package
    ``__init__``, and a conditional-exit helper.

    Asserting today's roster would prove nothing: ``trellis_cli`` has no
    cross-module must-exit helper to find, which is exactly why the
    blindness could ship. So the subject is a tree built to carry the
    defect, and the assertions are equalities over marked lines rather
    than a count.

    The four ``#!silent`` shapes carry the weight. Resolution that is
    *global by bare name* passes every ``#!report`` assertion here and
    fails these, and it is the worse defect of the two: an over-read
    helper roster lets an arm that merely might exit stand in for one that
    does, and nothing reports that.
    """
    # The corpus writes its own absolute imports; the directory they have
    # to resolve against comes from _CROSS_PACKAGE. Drift between the two
    # stops absolute resolution silently, and only the #!report
    # assertions below would notice — loudly, but naming the wrong cause.
    package_imports = [
        line
        for source in _CROSS_MODULE_CORPUS.values()
        for line in source.splitlines()
        if line.startswith(("from ", "import ")) and _CROSS_PACKAGE in line
    ]
    assert len(package_imports) >= 5, package_imports
    assert all(f"{_CROSS_PACKAGE}." in line for line in package_imports), (
        package_imports
    )

    must_report = _marked_lines(_REPORT_MARKER)
    must_stay_silent = _marked_lines(_SILENT_MARKER)
    assert_hand_read_floor(
        len(must_report),
        10,
        subject="cross-module shape the corpus renders",
        hint=(
            "Dropping a shape from _CROSS_MODULE_CORPUS is an exemption "
            "by another name (tests.ast_rules._validate_roster). Say why "
            "in the diff if a spelling genuinely cannot occur."
        ),
    )
    assert len(must_stay_silent) >= 4, (
        f"only {len(must_stay_silent)} negative controls; without them "
        f"'every marked line was reported' is satisfied by a predicate "
        f"that resolves every bare name globally"
    )

    root = _write_cross_module_corpus(tmp_path)
    reported = {":".join(v.split(":", 2)[:2]) for v in _violations(root=root)}

    missing = sorted(
        shape for shape, where in must_report.items() if where not in reported
    )
    assert not missing, (
        f"the shipped scan did not report {missing} — cross-module helper "
        f"resolution has stopped resolving, which is the state #491 was "
        f"filed against. Reported: {sorted(reported)}."
    )
    leaked = sorted(
        shape for shape, where in must_stay_silent.items() if where in reported
    )
    assert not leaked, (
        f"the shipped scan reported {leaked}, which no supported "
        f"resolution reaches: a helper that was never imported, one a "
        f"local definition shadows, one imported from outside the scanned "
        f"package, and a branch whose two arms both call it. Each is a "
        f"bare-name over-read, and over-reading the helper roster is this "
        f"rule's silent direction."
    )
    known = set(must_report.values()) | set(must_stay_silent.values())
    spurious = sorted(reported - known)
    assert not spurious, (
        f"the shipped scan reported unmarked line(s) {spurious}; a "
        f"predicate that reports everything satisfies the coverage check "
        f"above without resolving anything"
    )
