"""No surface reads the holdout outcome without the readout seal.

``TRELLIS_HOLDOUT_READOUT_AFTER`` is a convention until code keeps it. The
gate is :func:`trellis.analyze.holdout_gate.require_readout_open`, and a gate
only holds if nothing can reach the outcome around it. Five facts, each pinned
below by a scan of ``src/``:

1. **The population.** Every public function in ``holdout.py`` is classified
   by hand: the outcome read, the operational read, or pure arithmetic over
   numbers the caller already holds (``weighted_difference`` and the like). A
   new public function fails here until a person classifies it, whatever its
   parameters are called, and only the two reads may take an ``event_log``.
2. **The only door.** Nothing outside ``holdout.py`` and ``holdout_gate.py``
   names the outcome function; the surfaces name ``analyze_holdout_gated``.
3. **The roster of surfaces.** The modules that import the holdout analysis
   are a hand-read set. A new surface (an MCP tool, say) turns this red until
   it is added here, which is the moment someone checks it calls the gate.
4. **The gate leads.** The wrapper calls ``require_readout_open`` before the
   function it wraps, and the REST route does so before its cache lookup and
   before running the report.
5. **The REST roster.** A report whose handler calls the gated function is
   marked ``readout_gated=True``, and no other report is.

Each scan is a function of a source root, so the last test runs all of them
over a deliberately defective synthetic tree and requires each to name its
defect, following ``docs/agent-guide/testing.md``. Every identifier in the
synthetic tree is invented.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, iter_modules, name_of

SRC = Path(__file__).resolve().parents[3] / "src"

HOLDOUT = Path("trellis/analyze/holdout.py")
GATE = Path("trellis/analyze/holdout_gate.py")
ROUTES = Path("trellis_api/routes/analyze.py")
CLI = Path("trellis_cli/analyze.py")

#: Every public module-level ``holdout.py`` function, hand-read. ``pure`` ones
#: take numbers the caller already holds and read no store.
PUBLIC_FUNCTIONS = {
    "analyze_holdout": "outcome",
    "analyze_holdout_operational": "operational",
    "weighted_difference": "pure",
    "permute_within_strata": "pure",
    "permutation_p_value": "pure",
    "bootstrap_ci": "pure",
    "simulate_power": "pure",
    "binomial_two_sided": "pure",
    "analytic_mde": "pure",
    "share_for_rate": "pure",
    "n_for_mde": "pure",
}
#: The public functions that take an ``event_log``: the two reads.
EVENT_LOG_READERS = {
    name: kind for name, kind in PUBLIC_FUNCTIONS.items() if kind != "pure"
}
OUTCOME_ENTRY = "analyze_holdout"
GATED_ENTRY = "analyze_holdout_gated"
GATE_CALL = "require_readout_open"
#: The modules allowed to import the holdout analysis, hand-read. Both reach
#: the outcome only through ``analyze_holdout_gated``.
SURFACE_MODULES = {ROUTES, CLI}
#: ``(module, function)`` of each surface that calls the gated entry, hand-read.
GATED_SURFACES = {(CLI, "holdout"), (ROUTES, "_run_holdout")}
ANALYSIS_MODULES = ("trellis.analyze.holdout", "trellis.analyze.holdout_gate")


def _modules(root: Path) -> dict[Path, ast.Module]:
    return {path.relative_to(root): tree for path, tree in iter_modules(root)}


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and name_of(child.func) == name
    ]


def _public_functions(
    modules: dict[Path, ast.Module],
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in modules[HOLDOUT].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]


def _readers(modules: dict[Path, ast.Module]) -> dict[str, str]:
    """Public module-level ``holdout.py`` functions taking ``event_log``."""
    found: dict[str, str] = {}
    for node in _public_functions(modules):
        params = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        if any(arg.arg == "event_log" for arg in params):
            found[node.name] = "unclassified"
    return found


def unclassified_readers(modules: dict[Path, ast.Module]) -> list[str]:
    """A public function nobody classified, or an ``event_log`` reader that is
    not one of the two reads.

    The first half is what catches a new outcome read whose store parameter is
    named something other than ``event_log``.
    """
    public = {node.name for node in _public_functions(modules)}
    unlisted = public - set(PUBLIC_FUNCTIONS)
    return sorted(unlisted | (set(_readers(modules)) ^ set(EVENT_LOG_READERS)))


def bypasses(modules: dict[Path, ast.Module]) -> list[str]:
    """Places outside the two analysis modules that name the ungated outcome read."""
    out: list[str] = []
    for path, tree in modules.items():
        if path in {HOLDOUT, GATE}:
            continue
        for node in ast.walk(tree):
            named = (
                (isinstance(node, ast.Name) and node.id == OUTCOME_ENTRY)
                or (isinstance(node, ast.Attribute) and node.attr == OUTCOME_ENTRY)
                or (
                    isinstance(node, ast.alias)
                    and node.name.rsplit(".", 1)[-1] == OUTCOME_ENTRY
                )
                or (isinstance(node, ast.Constant) and node.value == OUTCOME_ENTRY)
            )
            if named:
                out.append(f"{path}:{getattr(node, 'lineno', 0)} names {OUTCOME_ENTRY}")
            # ``functools.wraps`` exposes the ungated function as ``__wrapped__``.
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "__wrapped__"
                and name_of(node.value) == GATED_ENTRY
            ):
                out.append(f"{path}:{node.lineno} unwraps {GATED_ENTRY}")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == ANALYSIS_MODULES[0]
                and any(alias.name.startswith("_") for alias in node.names)
            ):
                out.append(f"{path}:{node.lineno} imports a private holdout name")
    return out


def _imports_analysis(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in ANALYSIS_MODULES:
                return True
            if node.module == "trellis.analyze" and any(
                alias.name in {"holdout", "holdout_gate"} for alias in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name in ANALYSIS_MODULES for alias in node.names
        ):
            return True
    return False


def unlisted_surfaces(modules: dict[Path, ast.Module]) -> list[str]:
    importing = {
        path
        for path, tree in modules.items()
        if path not in {HOLDOUT, GATE} and _imports_analysis(tree)
    }
    return sorted(str(path) for path in importing ^ SURFACE_MODULES)


def gated_callers(modules: dict[Path, ast.Module]) -> set[tuple[Path, str]]:
    """Every function outside the gate module that calls the gated entry."""
    return {
        (path, function.name)
        for path, tree in modules.items()
        if path not in {HOLDOUT, GATE}
        for function in _functions(tree)
        if _calls(function, GATED_ENTRY)
    }


def unrostered_callers(modules: dict[Path, ast.Module]) -> list[str]:
    return sorted(
        f"{path}:{name}" for path, name in gated_callers(modules) ^ GATED_SURFACES
    )


def gate_does_not_lead(modules: dict[Path, ast.Module]) -> list[str]:
    """The wrapper's inner function calls the gate before the function it wraps."""
    out: list[str] = []
    tree = modules[GATE]
    sealers = [f for f in _functions(tree) if f.name == "_sealed"]
    if len(sealers) != 1:
        return [f"{GATE}: expected one _sealed wrapper, found {len(sealers)}"]
    inner = [
        f for f in _functions(sealers[0]) if f is not sealers[0] and f.name == "gated"
    ]
    if len(inner) != 1:
        return [f"{GATE}: _sealed has no inner `gated` function"]
    body = inner[0].body
    first = body[0]
    leads = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Call)
        and name_of(first.value.func) == GATE_CALL
    )
    if not leads:
        out.append(f"{GATE}: `gated` does not open with {GATE_CALL}()")
    assigned = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == GATED_ENTRY for t in node.targets)
        and isinstance(node.value, ast.Call)
        and name_of(node.value.func) == "_sealed"
        and [name_of(a) for a in node.value.args] == [OUTCOME_ENTRY]
    ]
    if len(assigned) != 1:
        out.append(f"{GATE}: {GATED_ENTRY} is not `_sealed({OUTCOME_ENTRY})`")
    return out


def route_does_not_gate_first(modules: dict[Path, ast.Module]) -> list[str]:
    """``get_analyze_report`` raises the seal before the cache and before the run."""
    routes = [f for f in _functions(modules[ROUTES]) if f.name == "get_analyze_report"]
    if len(routes) != 1:
        return [f"{ROUTES}: expected one get_analyze_report, found {len(routes)}"]
    handler = routes[0]
    guards = [
        node
        for node in handler.body
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "entry.readout_gated"
        and any(
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and name_of(stmt.value.func) == GATE_CALL
            for stmt in node.body
        )
    ]
    if len(guards) != 1:
        return [f"{ROUTES}: no `if entry.readout_gated: {GATE_CALL}()` in the route"]
    guard_line = guards[0].lineno
    return [
        f"{ROUTES}:{call.lineno} calls {target} before the seal"
        for target in ("_cache_get", "run", "get_registry")
        for call in _calls(handler, target)
        if call.lineno < guard_line
    ]


def _report_entries(tree: ast.Module) -> dict[str, tuple[str | None, bool]]:
    """``_REPORTS`` as ``{report: (run handler name, readout_gated)}``."""
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        if not (isinstance(target, ast.Name) and target.id == "_REPORTS"):
            continue
        assert isinstance(node.value, ast.Dict)
        entries: dict[str, tuple[str | None, bool]] = {}
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            assert isinstance(key, ast.Constant)
            assert isinstance(value, ast.Call)
            keywords = {kw.arg: kw.value for kw in value.keywords}
            run = keywords.get("run")
            gated = keywords.get("readout_gated")
            entries[str(key.value)] = (
                name_of(run) if run is not None else None,
                isinstance(gated, ast.Constant) and gated.value is True,
            )
        return entries
    msg = f"{ROUTES}: no _REPORTS table"
    raise AssertionError(msg)


def rest_roster_defects(modules: dict[Path, ast.Module]) -> list[str]:
    tree = modules[ROUTES]
    handlers = {f.name: f for f in _functions(tree)}
    out: list[str] = []
    for report, (run, marked) in sorted(_report_entries(tree).items()):
        calls_gate = run in handlers and bool(_calls(handlers[run], GATED_ENTRY))
        if calls_gate and not marked:
            out.append(f"{report}: runs {GATED_ENTRY} but is not readout_gated=True")
        if marked and not calls_gate:
            out.append(f"{report}: readout_gated=True but its handler is not gated")
    return out


SCANS = {
    "unclassified_readers": unclassified_readers,
    "bypasses": bypasses,
    "unlisted_surfaces": unlisted_surfaces,
    "unrostered_callers": unrostered_callers,
    "gate_does_not_lead": gate_does_not_lead,
    "route_does_not_gate_first": route_does_not_gate_first,
    "rest_roster_defects": rest_roster_defects,
}


@pytest.fixture(scope="module")
def real() -> dict[Path, ast.Module]:
    return _modules(SRC)


@pytest.mark.parametrize("scan", sorted(SCANS))
def test_the_tree_holds_the_rule(real: dict[Path, ast.Module], scan: str) -> None:
    assert SCANS[scan](real) == []


def test_the_population_floors_are_hand_read(real: dict[Path, ast.Module]) -> None:
    """Two readers, two surface modules, two gated surfaces, one gated report.

    Counted by hand from the tree, not from a scan: ``analyze_holdout`` and
    ``analyze_holdout_operational``; the CLI module and the REST routes module;
    the CLI ``holdout`` command and REST ``_run_holdout``; the ``holdout`` row
    of the REST report table.
    """
    assert_hand_read_floor(
        len(_readers(real)), 2, subject="holdout.py event_log readers"
    )
    assert_hand_read_floor(
        len(
            {
                p
                for p, t in real.items()
                if p not in {HOLDOUT, GATE} and _imports_analysis(t)
            }
        ),
        2,
        subject="modules importing the holdout analysis",
    )
    assert_hand_read_floor(
        len(gated_callers(real)), 2, subject="surfaces calling the gated entry"
    )
    gated_reports = [
        r for r, (_, marked) in _report_entries(real[ROUTES]).items() if marked
    ]
    assert_hand_read_floor(
        len(gated_reports),
        1,
        subject="readout-gated REST reports",
        sole_site_reason="the holdout is the only report that reads a per-arm outcome.",
    )
    assert EVENT_LOG_READERS.keys() == _readers(real).keys()


def test_the_operational_read_is_not_a_gated_surface(
    real: dict[Path, ast.Module],
) -> None:
    """The operational read computes no outcome: no gate call stands before it."""
    assert EVENT_LOG_READERS["analyze_holdout_operational"] == "operational"
    for tree in real.values():
        for function in _functions(tree):
            if function.name == "analyze_holdout_operational":
                assert not _calls(function, GATE_CALL)


# ---------------------------------------------------------------------------
# Prove the guard bites: the scans over a deliberately defective tree.
# ---------------------------------------------------------------------------

_HOLDOUT_SRC = """
def analyze_holdout(event_log, *, days=60):
    return None


def analyze_holdout_operational(event_log, *, days=60):
    return None


def analyze_holdout_by_week(event_log, *, days=60):
    return None


def _gather(event_log):
    return None


def weighted_difference(values):
    return None
"""

_THIRD_READER = "def analyze_holdout_by_week(event_log, *, days=60):\n    return None\n"

#: A new outcome read whose store parameter is not named ``event_log``.
_RENAMED_READER = "\n\ndef analyze_holdout_weekly(log, *, days=60):\n    return None\n"

_GATE_GOOD = """
from trellis.analyze.holdout import analyze_holdout


def require_readout_open():
    return None


def _sealed(function):
    def gated(*args, **kwargs):
        require_readout_open()
        return function(*args, **kwargs)

    return gated


analyze_holdout_gated = _sealed(analyze_holdout)
"""

_GATE_BAD = """
from trellis.analyze.holdout import analyze_holdout


def require_readout_open():
    return None


def _sealed(function):
    def gated(*args, **kwargs):
        result = function(*args, **kwargs)
        require_readout_open()
        return result

    return gated


analyze_holdout_gated = analyze_holdout
"""

_ROUTES_GOOD = """
from trellis.analyze.holdout_gate import analyze_holdout_gated, require_readout_open


def _run_holdout(params, registry):
    return analyze_holdout_gated(registry.log)


def _run_other(params, registry):
    return {}


_REPORTS = {
    "holdout": _ReportEntry(run=_run_holdout, readout_gated=True),
    "other": _ReportEntry(run=_run_other),
}


def get_analyze_report(report, request):
    entry = _REPORTS[report]
    if entry.readout_gated:
        require_readout_open()
    registry = get_registry()
    hit = _cache_get(report, registry, "k")
    return hit or entry.run(None, registry)
"""

_ROUTES_BAD = """
from trellis.analyze.holdout import analyze_holdout
from trellis.analyze.holdout_gate import analyze_holdout_gated, require_readout_open


def _run_holdout(params, registry):
    return analyze_holdout_gated(registry.log)


def _run_week(params, registry):
    return analyze_holdout_gated(registry.log)


def _run_direct(params, registry):
    return analyze_holdout(registry.log)


def _run_other(params, registry):
    return {}


_REPORTS = {
    "holdout": _ReportEntry(run=_run_holdout),
    "week": _ReportEntry(run=_run_week, readout_gated=True),
    "other": _ReportEntry(run=_run_other, readout_gated=True),
}


def get_analyze_report(report, request):
    entry = _REPORTS[report]
    registry = get_registry()
    hit = _cache_get(report, registry, "k")
    if entry.readout_gated:
        require_readout_open()
    return hit or entry.run(None, registry)
"""

_CLI_GOOD = """
from trellis.analyze.holdout_gate import analyze_holdout_gated


def holdout():
    return analyze_holdout_gated(None)
"""

_CLI_BAD_ALIAS = """
import trellis.analyze.holdout as study
from trellis.analyze.holdout import analyze_holdout as read_outcome, _gather


def holdout():
    return read_outcome(None)


def by_attribute():
    return study.analyze_holdout(None)


def by_name():
    return getattr(study, "analyze_holdout")(None)
"""

_MCP_BAD = """
from trellis.analyze.holdout_gate import analyze_holdout_gated


def holdout_tool():
    return analyze_holdout_gated(None)


def holdout_tool_unwrapped():
    return analyze_holdout_gated.__wrapped__(None)
"""


def _tree(tmp_path: Path, files: dict[Path, str]) -> dict[Path, ast.Module]:
    for relative, source in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.lstrip("\n"), encoding="utf-8")
    return _modules(tmp_path)


def test_the_scans_pass_a_compliant_synthetic_tree(tmp_path: Path) -> None:
    modules = _tree(
        tmp_path,
        {
            HOLDOUT: _HOLDOUT_SRC.replace(_THIRD_READER, ""),
            GATE: _GATE_GOOD,
            ROUTES: _ROUTES_GOOD,
            CLI: _CLI_GOOD,
        },
    )
    assert unclassified_readers(modules) == []
    assert bypasses(modules) == []
    assert unlisted_surfaces(modules) == []
    assert unrostered_callers(modules) == []
    assert gate_does_not_lead(modules) == []
    assert route_does_not_gate_first(modules) == []
    assert rest_roster_defects(modules) == []


def test_the_scans_name_every_defect_in_a_synthetic_tree(tmp_path: Path) -> None:
    modules = _tree(
        tmp_path,
        {
            HOLDOUT: _HOLDOUT_SRC + _RENAMED_READER,
            GATE: _GATE_BAD,
            ROUTES: _ROUTES_BAD,
            CLI: _CLI_BAD_ALIAS,
            Path("trellis/mcp/holdout_tool.py"): _MCP_BAD,
        },
    )

    # A third public reader nobody classified, and one whose store parameter
    # has another name.
    assert unclassified_readers(modules) == [
        "analyze_holdout_by_week",
        "analyze_holdout_weekly",
    ]

    # The direct import, the aliased import, the attribute and the string spelling
    # in the CLI module, the import in the routes module, and the private import.
    found = bypasses(modules)
    assert sum(1 for line in found if line.startswith(f"{CLI}:")) >= 4
    assert any(line.startswith(f"{ROUTES}:") for line in found)
    assert any("private holdout name" in line for line in found)
    assert any(f"unwraps {GATED_ENTRY}" in line for line in found)

    # A module that imports the analysis but is not on the roster (an MCP tool).
    assert unlisted_surfaces(modules) == ["trellis/mcp/holdout_tool.py"]
    # The MCP tool and an unrostered REST handler call the gated entry; the CLI
    # command in this tree no longer does (it reads the outcome around the gate).
    assert unrostered_callers(modules) == sorted(
        [
            f"{CLI}:holdout",
            f"{ROUTES}:_run_week",
            "trellis/mcp/holdout_tool.py:holdout_tool",
        ]
    )

    # A wrapper that gates after the call, and an ungated alias of the read.
    defects = gate_does_not_lead(modules)
    assert any("does not open with" in line for line in defects)
    assert any("is not `_sealed(" in line for line in defects)

    # A route that consults the cache before the seal.
    late = route_does_not_gate_first(modules)
    assert any("_cache_get before the seal" in line for line in late)

    # A gated handler left unmarked, and two marked reports that are not gated.
    assert rest_roster_defects(modules) == [
        "holdout: runs analyze_holdout_gated but is not readout_gated=True",
        "other: readout_gated=True but its handler is not gated",
        # ``week`` is gated and marked: consistent, so it is not listed.
    ]


def test_a_route_with_no_seal_at_all_is_named(tmp_path: Path) -> None:
    source = _ROUTES_GOOD.replace(
        "    if entry.readout_gated:\n        require_readout_open()\n", ""
    )
    modules = _tree(
        tmp_path,
        {HOLDOUT: _HOLDOUT_SRC, GATE: _GATE_GOOD, ROUTES: source, CLI: _CLI_GOOD},
    )

    assert route_does_not_gate_first(modules) == [
        f"{ROUTES}: no `if entry.readout_gated: {GATE_CALL}()` in the route"
    ]
