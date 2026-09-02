"""Enforcement for the subprocess-checkout rule (#431).

pytest's ``pythonpath = ["src", "."]`` applies to the **test-driver process
only**. Anything it spawns inherits the parent shell's environment and
resolves ``trellis`` through the venv's editable install — which points at
whichever checkout was ``pip install -e``'d. From a git worktree that is a
*different branch*, measured::

    in-process   import trellis -> /mnt/ssd/trellis-worktrees/wt-430/src
    subprocess   import trellis -> /home/nronsse/projects/trellis-ai/src

So a subprocess test can validate production code, report it as the branch's
result, and give no error, no skip and no warning while doing it. #428 pinned
``PYTHONPATH`` in the three fixtures that existed. What it did not do is stop
a fourth being written without one — this is that enforcer, and #431 is the
issue asking for it.

**What is enforced.** Every subprocess launch under ``tests/`` must be handed
an ``env=`` that is either

* **statically pinned** — its expression reaches one of the repo's env
  builders, a set this module *derives* rather than lists (see
  :func:`_pinned_builders`); or
* **guarded at the boundary** — the enclosing helper takes its env as a
  parameter and calls
  ``tests.integration._live_server.assert_env_pins_this_checkout`` before
  launching. Five helpers are pass-throughs whose caller owns the pin —
  ``run_cli``, ``initialize_trellis_stores``, ``spawn_uvicorn``,
  ``_run_stdio_server`` and ``assert_subprocess_imports_this_checkout`` —
  and ``run_cli`` is reached through a closure a fixture returns, so no
  static rule can see its callers at all. A string check at the launch
  boundary can, and costs nothing.

**What is not.** That a ``PYTHONPATH`` containing this ``src/`` really makes
a child import this checkout is not a static property at all. It is proven
end to end, by spawning a real interpreter and comparing the package
directory it resolved, once per env fixture:
``tests/integration/cli/test_subprocess_smoke.py`` and
``tests/integration/mcp/test_stdio_stream_hygiene.py`` each carry a
``test_the_subprocess_under_test_is_this_checkout``, and
:func:`test_the_pin_actually_reaches_a_child` covers the builder they share.
Removing the pin from ``cli_env`` turns every test in that directory red,
which is how the two halves were verified against each other.

Scope is ``tests/`` only. Production code spawning a subprocess is not
affected by which checkout pytest collected from.

**Guarding the guard.** #457 shipped an AST rule with three vacuity guards —
a floor on branches found, a resolvability ratio, and a non-empty helper set
— and all three stayed green while the scanner silently under-collected
148 → 123, because the ratio's denominator was taken from the population the
bug had already truncated. A guard that measures its subject using its
subject is not a guard. So the population here is counted a **second time by
a different mechanism**: :func:`_launch_sites` parses, while
:func:`_token_launch_sites` tokenizes, and
:func:`test_the_two_scans_agree_on_the_population` fails if they disagree by
one site. A parser bug and a tokenizer bug do not coincide.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

from tests.integration._live_server import (
    assert_subprocess_imports_this_checkout,
    build_subprocess_env,
    repo_src_pythonpath,
)

#: ``subprocess`` attributes that start a process. ``run``/``call``/
#: ``check_call``/``check_output`` all construct a ``Popen`` internally, but
#: they are matched by name rather than through it — the rule is about the
#: call site an author writes, not the implementation underneath.
_SUBPROCESS_LAUNCHERS = frozenset(
    {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
    }
)

#: ``os`` attributes that start or replace a process. ``system`` and
#: ``popen`` take no ``env`` at all, which is precisely why they belong here:
#: under this rule they are unwritable in ``tests/``, and that is the correct
#: answer for a suite that must control its child's import path.
_OS_LAUNCHERS = frozenset(
    {
        "system",
        "popen",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnv",
        "spawnve",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
    }
)

#: asyncio's own spawn surface. It does **not** route through
#: ``subprocess.Popen``, so a runtime monkeypatch on ``Popen`` would miss it
#: — one of the reasons this rule is static.
_ASYNCIO_LAUNCHERS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})

#: MCP client transports. Each one spawns a server process and forwards
#: ``env`` to it, so they are launch sites wearing a constructor's clothes —
#: and they are how two of the repo's eight launch sites start a process.
_TRANSPORT_CTORS = frozenset(
    {
        "StdioTransport",
        "PythonStdioTransport",
        "NodeStdioTransport",
        "NpxStdioTransport",
        "UvxStdioTransport",
        "UvStdioTransport",
    }
)

#: Modules whose launcher attributes are matched. Keyed by the *bare module
#: name* as written at the call site, which is sound only while nothing under
#: ``tests/`` aliases or from-imports them —
#: :func:`test_the_scans_unaliased_import_assumption_holds` enforces that,
#: rather than leaving it as an unstated premise.
_LAUNCHER_ATTRS = {
    "subprocess": _SUBPROCESS_LAUNCHERS,
    "os": _OS_LAUNCHERS,
    "asyncio": _ASYNCIO_LAUNCHERS,
}

#: The seed of the pinned-builder fixpoint. Everything else is derived.
_PIN_SOURCE = "repo_src_pythonpath"

#: The boundary check a pass-through helper must call before launching.
_BOUNDARY_GUARD = "assert_env_pins_this_checkout"


def _tests_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    assert root.is_dir(), f"tests/ not found at {root}"
    return root


def _callee_name(node: ast.Call) -> str | None:
    """The bare name being called: ``json.dumps`` and ``dumps`` both give ``dumps``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_launch(node: ast.AST) -> bool:
    """Is *node* a call that starts a process?"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _TRANSPORT_CTORS
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr in _LAUNCHER_ATTRS.get(func.value.id, frozenset())
    return False


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# ── population: two independent counts of the same thing ──────────────


def _launch_sites(root: Path) -> list[tuple[str, int]]:
    """Every process-launching call under *root*, by ``(path, line)`` — via AST."""
    sites: list[tuple[str, int]] = []
    for path in _python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sites.extend(
            (str(path.relative_to(root)), node.lineno)
            for node in ast.walk(tree)
            if _is_launch(node)
        )
    return sorted(sites)


def _token_launch_sites(root: Path) -> list[tuple[str, int]]:
    """The same population, counted by tokenizing instead of parsing.

    Reads only ``NAME`` and ``OP`` tokens, so a launcher name inside a
    docstring, a comment or a string constant — this module's own
    ``_SUBPROCESS_LAUNCHERS`` set, for instance — cannot inflate the count.
    Shares the name sets above with :func:`_launch_sites`, because those are
    the *spec*; what is deliberately not shared is the traversal, which is
    where under-collection actually comes from.
    """
    sites: list[tuple[str, int]] = []
    for path in _python_files(root):
        stream = io.StringIO(path.read_text(encoding="utf-8"))
        code = [
            tok
            for tok in tokenize.generate_tokens(stream.readline)
            if tok.type in (tokenize.NAME, tokenize.OP)
        ]
        rel = str(path.relative_to(root))
        for i, tok in enumerate(code):
            names = [t.string for t in code[i : i + 4]]
            dotted = (  # ``<module> . <launcher> (``
                len(names) == 4
                and names[1] == "."
                and names[3] == "("
                and names[2] in _LAUNCHER_ATTRS.get(names[0], frozenset())
            )
            bare = (  # ``<Transport> (``, not ``x.StdioTransport(``
                len(names) >= 2
                and names[0] in _TRANSPORT_CTORS
                and names[1] == "("
                and (i == 0 or code[i - 1].string != ".")
            )
            if dotted or bare:
                sites.append((rel, tok.start[0]))
    return sorted(sites)


# ── which environments carry the pin ──────────────────────────────────


def _pinned_builders(trees: dict[str, ast.Module]) -> set[str]:
    """Names of functions under ``tests/`` that return a pinned environment.

    Derived, not listed: the seed is the one function that writes the pin
    (:data:`_PIN_SOURCE`) and everything else is whatever the code makes of
    it. A roster would rot the first time a fixture was renamed; this cannot.

    Two propagation rules earn their place against real call sites:
    ``cli_env`` builds its dict with ``os.environ.copy()`` and only becomes
    pinned at the ``env.update({... repo_src_pythonpath()})`` line, and
    ``initialized_cli_env`` is pinned solely because its *parameter* is named
    after a pinned fixture — which is how pytest injects one.
    """
    builders = {_PIN_SOURCE}
    for _ in range(len(trees) + 2):  # bounded fixpoint; converges in 2-3 passes
        before = set(builders)
        for tree in trees.values():
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _returns_pinned(fn, builders):
                    builders.add(fn.name)
        if builders == before:
            break
    return builders


def _local_pinned_names(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, builders: set[str]
) -> set[str]:
    """Names inside *fn* that hold a pinned environment."""
    local = {
        arg.arg
        for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
        if arg.arg in builders
    }
    # Two passes so a binding used before its assignment appears in the walk
    # order (decorated fixtures, nested defs) still resolves.
    for _ in range(2):
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and _is_pinned(node.value, local, builders):
                local.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif (
                isinstance(node, (ast.AnnAssign, ast.NamedExpr))
                and node.value is not None
                and isinstance(node.target, ast.Name)
                and _is_pinned(node.value, local, builders)
            ):
                local.add(node.target.id)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"update", "setdefault"}
                and isinstance(node.func.value, ast.Name)
                and any(_is_pinned(a, local, builders) for a in node.args)
            ):
                # ``env.update({..., "PYTHONPATH": repo_src_pythonpath()})``
                local.add(node.func.value.id)
    return local


def _returns_pinned(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, builders: set[str]
) -> bool:
    local = _local_pinned_names(fn, builders)
    return any(
        isinstance(node, (ast.Return, ast.Yield))
        and node.value is not None
        and _is_pinned(node.value, local, builders)
        for node in ast.walk(fn)
    )


def _is_pinned(expr: ast.expr, local: set[str], builders: set[str]) -> bool:
    """Does *expr* evaluate to an environment carrying this checkout's ``src``?"""
    if isinstance(expr, ast.Name):
        return expr.id in local
    if isinstance(expr, (ast.Await, ast.Starred, ast.NamedExpr)):
        return _is_pinned(expr.value, local, builders)
    if isinstance(expr, ast.Dict):
        # ``{**pinned, "EXTRA": "x"}`` keeps the pin, and so does a literal
        # that sets PYTHONPATH from the pin source. A ``**`` unpack appears
        # as a value with a ``None`` key, so iterating values covers both.
        return any(_is_pinned(v, local, builders) for v in expr.values)
    if isinstance(expr, ast.Call):
        name = _callee_name(expr)
        if name in builders:
            return True
        # A helper that decorates a pinned env — ``_server_env(cli_env)`` —
        # cannot remove a PYTHONPATH entry it was handed without saying so.
        return any(
            _is_pinned(a, local, builders)
            for a in [*expr.args, *[kw.value for kw in expr.keywords]]
        )
    return False


# ── the rule ──────────────────────────────────────────────────────────


def _enclosing_functions(
    tree: ast.Module,
) -> dict[ast.AST, ast.FunctionDef | ast.AsyncFunctionDef]:
    owner: dict[ast.AST, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                # Innermost wins: the outer function's walk runs first, then
                # a nested def overwrites its own descendants.
                owner[node] = fn
    return owner


def _guards_at_the_boundary(
    fn: ast.FunctionDef | ast.AsyncFunctionDef | None, launch_lineno: int
) -> bool:
    """Does *fn* call the boundary guard *before* the launch on that line?

    Ordering is checked because a guard that runs after the spawn silences
    the rule without preventing anything — the child is already started, and
    on the ``Popen`` path it is not even waited on.
    """
    if fn is None:
        return False
    return any(
        isinstance(node, ast.Call)
        and _callee_name(node) == _BOUNDARY_GUARD
        and node.lineno < launch_lineno
        for node in ast.walk(fn)
    )


def _violations(root: Path | None = None) -> list[str]:
    """Launch sites whose environment is neither pinned nor boundary-guarded.

    *root* is injectable so :func:`test_the_scan_catches_every_known_evasion`
    exercises **this** function over a synthetic tree rather than a copy of
    its logic — the mistake ``test_machine_output_rule`` records having made,
    where stubbing the scanner out left the guard green.
    """
    tests_root = root if root is not None else _tests_root()
    trees = {
        str(p.relative_to(tests_root)): ast.parse(p.read_text(encoding="utf-8"))
        for p in _python_files(tests_root)
    }
    builders = _pinned_builders(trees)

    found: list[str] = []
    for rel, tree in sorted(trees.items()):
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not _is_launch(node):
                continue
            assert isinstance(node, ast.Call)
            env_kwarg = next(
                (kw.value for kw in node.keywords if kw.arg == "env"), None
            )
            fn = owner.get(node)
            local = _local_pinned_names(fn, builders) if fn is not None else set()
            if env_kwarg is None:
                found.append(
                    f"{rel}:{node.lineno}: no env= argument, so the child "
                    f"inherits the shell's"
                )
            elif not (
                _is_pinned(env_kwarg, local, builders)
                or _guards_at_the_boundary(fn, node.lineno)
            ):
                found.append(
                    f"{rel}:{node.lineno}: "
                    f"env={ast.unparse(env_kwarg)[:60]} is not a pinned env"
                )
    return found


def test_every_subprocess_launch_under_tests_pins_this_checkout() -> None:
    """The rule (#431)."""
    violations = _violations()
    assert not violations, (
        "a subprocess under tests/ would resolve `trellis` through the venv's "
        "editable install — a different checkout when the session runs from a "
        "git worktree — and report the result as this branch's. Build the env "
        "with `tests.integration._live_server.repo_src_pythonpath()`, or call "
        f"`{_BOUNDARY_GUARD}(env, what=...)` if the env is a parameter.\n  "
        + "\n  ".join(violations)
    )


# ── guarding the guard ────────────────────────────────────────────────


def test_the_two_scans_agree_on_the_population() -> None:
    """A parser bug and a tokenizer bug do not coincide (see module docstring).

    This is the guard #457's three vacuity checks could not be: it compares
    the scanned population against an independently-derived count of the same
    population, so under-collection cannot hide inside its own denominator.
    """
    root = _tests_root()
    by_ast = _launch_sites(root)
    by_tokens = _token_launch_sites(root)
    assert by_ast == by_tokens, (
        "the AST scan and the token scan disagree about which lines start a "
        "process, so at least one of them is under-collecting and the rule "
        "above is silently policing a subset.\n"
        f"  only in AST scan:   {sorted(set(by_ast) - set(by_tokens))}\n"
        f"  only in token scan: {sorted(set(by_tokens) - set(by_ast))}"
    )


def test_the_scan_reaches_every_test_module() -> None:
    """Every ``*.py`` under ``tests/`` parses and is scanned.

    A scan that silently skipped unparseable files would shrink its own
    population without shrinking its confidence.
    """
    root = _tests_root()
    files = _python_files(root)
    assert len(files) > 400, f"only {len(files)} test modules found under {root}"
    for path in files:
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover — a red suite either way
            pytest.fail(f"{path} does not parse, so the scan skips it: {exc}")


def test_the_scans_unaliased_import_assumption_holds() -> None:
    """Both scans match ``subprocess.run``-shaped names, not resolved symbols.

    That is sound only while nothing under ``tests/`` writes
    ``import subprocess as sp`` or ``from subprocess import Popen``. Stating
    the premise in a comment would leave it to rot; asserting it means the
    day someone writes the alias, this test says so instead of the rule going
    quietly blind.
    """
    offenders: list[str] = []
    for path in _python_files(_tests_root()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}:{node.lineno}: import {a.name} as {a.asname}"
                    for a in node.names
                    if a.name in _LAUNCHER_ATTRS and a.asname is not None
                ]
            elif isinstance(node, ast.ImportFrom) and node.module in _LAUNCHER_ATTRS:
                offenders += [
                    f"{path.name}:{node.lineno}: from {node.module} import {a.name}"
                    for a in node.names
                    if a.name in _LAUNCHER_ATTRS[node.module]
                ]
    assert not offenders, (
        "a launcher was imported under a name the scans cannot see; widen "
        "`_is_launch` and `_token_launch_sites` before adding one.\n  "
        + "\n  ".join(offenders)
    )


def test_the_derived_builder_set_is_exactly_the_real_ones() -> None:
    """The derivation is checked in both directions, and equality is why.

    Under-deriving is loud: launch sites stop looking pinned and the rule
    above fails. **Over**-deriving is silent — every launch site looks pinned
    and the rule stops rejecting anything — so a subset assertion would let
    the scan rot in the one direction that matters. The synthetic corpus in
    :func:`test_the_scan_catches_every_known_evasion` covers over-derivation
    for the shapes it names; this covers it for the real tree.

    Adding a genuine env builder is expected to fail this test. Add it here.
    """
    root = _tests_root()
    trees = {
        str(path.relative_to(root)): ast.parse(path.read_text(encoding="utf-8"))
        for path in _python_files(root)
    }
    assert _pinned_builders(trees) == {
        "repo_src_pythonpath",  # the seed
        "build_subprocess_env",  # sets PYTHONPATH directly
        "cli_env",  # pinned via env.update({...})
        "mcp_subprocess_env",  # pinned via env.update({...})
        "initialized_cli_env",  # pinned only by returning its cli_env param
    }


#: Every shape that must be rejected, and the ones that must not. Line
#: numbers are the assertion; the comments say what each one is.
_EVASIONS = """
import os
import subprocess

from tests.integration._live_server import repo_src_pythonpath


def cooked_env():
    env = os.environ.copy()
    env.update({"PYTHONPATH": repo_src_pythonpath()})
    return env


def passthrough(env):
    assert_env_pins_this_checkout(env, what="passthrough")
    subprocess.run(["x"], env=env)              # 15 ALLOWED: boundary guard


def unguarded_passthrough(env):
    subprocess.run(["x"], env=env)              # 19 param, nothing checks it


def straight_up():
    subprocess.run(["x"])                       # 23 no env= at all


def handrolled():
    subprocess.run(["x"], env=os.environ.copy())  # 27 copy without the pin


def literal():
    subprocess.run(["x"], env={"PATH": "/usr/bin"})  # 31 fresh dict, no pin


def popen_sibling():
    subprocess.Popen(["x"], env=os.environ)     # 35 the shell's env verbatim


def transport_sibling():
    StdioTransport(command="x", env={})         # 39 transport is a launcher


def os_system_has_no_env_at_all():
    os.system("x")                              # 43 unwritable here, on purpose


def asyncio_escapes_a_popen_monkeypatch():
    asyncio.create_subprocess_exec("x", env={})  # 47 not routed via Popen


def uses_the_builder():
    subprocess.run(["x"], env=cooked_env())     # 51 ALLOWED: derived builder


def uses_a_bound_builder():
    e = cooked_env()
    subprocess.run(["x"], env=e)                # 56 ALLOWED: bound from one


def decorates_a_pinned_env():
    e = cooked_env()
    subprocess.run(["x"], env={**e, "K": "v"})  # 61 ALLOWED: unpack keeps it


def wraps_a_pinned_env():
    subprocess.run(["x"], env=dict(cooked_env()))  # 65 ALLOWED: helper call


def guards_too_late(env):
    subprocess.run(["x"], env=env)               # 69 guard runs after the spawn
    assert_env_pins_this_checkout(env, what="late")
"""

#: The lines above the rule must report. Everything marked ALLOWED is absent.
_EXPECTED_EVASION_LINES = [19, 23, 27, 31, 35, 39, 43, 47, 69]


def test_the_scan_catches_every_known_evasion(tmp_path: Path) -> None:
    """Mutation guard, run through the shipped scanner rather than a copy.

    Both directions matter. Missing a rejection means the rule is decorative;
    reporting an ALLOWED line means it would be turned off by the first
    author it inconveniences.
    """
    (tmp_path / "evasions.py").write_text(_EVASIONS.lstrip("\n"))
    reported = sorted(int(v.split(":")[1]) for v in _violations(root=tmp_path))
    assert reported == _EXPECTED_EVASION_LINES, (
        f"scanner reported {reported}, expected {_EXPECTED_EVASION_LINES}; "
        f"missing={sorted(set(_EXPECTED_EVASION_LINES) - set(reported))} "
        f"spurious={sorted(set(reported) - set(_EXPECTED_EVASION_LINES))}"
    )


def test_the_two_scans_agree_on_the_synthetic_tree(tmp_path: Path) -> None:
    """The cross-check itself, exercised on a tree with a known answer.

    On the real tree the two scans agreeing could in principle mean both are
    broken the same way. Here the expected population is written down.
    """
    (tmp_path / "evasions.py").write_text(_EVASIONS.lstrip("\n"))
    expected = [
        ("evasions.py", n)
        for n in (15, 19, 23, 27, 31, 35, 39, 43, 47, 51, 56, 61, 65, 69)
    ]
    assert _launch_sites(tmp_path) == expected
    assert _token_launch_sites(tmp_path) == expected


# ── the end-to-end half ───────────────────────────────────────────────


def test_the_pin_actually_reaches_a_child(tmp_path: Path) -> None:
    """``repo_src_pythonpath`` makes a real child import **this** checkout.

    Everything above is a claim about text. This is the one that spawns an
    interpreter, and it covers ``build_subprocess_env`` — the builder behind
    ``live_api_server`` and ``loop_env``, both of which skip without live
    infra, so their own suites can never assert it in CI.
    """
    assert str(_tests_root().parent / "src") in repo_src_pythonpath()
    assert_subprocess_imports_this_checkout(
        build_subprocess_env(tmp_path / ".trellis", tmp_path / "data")
    )
