"""No REST route builds an executor or reads documents at a literal clearance.

Every route that builds a governed executor or reads documents takes the
caller's clearance from the principal (``request_clearance(ctx)``, #1037).
``build_curate_executor`` has no default for its ``clearance`` keyword, so
omitting it is a type error, but nothing forbids passing a constant.
``clearance="restricted"`` reads and writes every label, and a route written
that way passes every functional test (the tests run with auth off, which
grants everything) and every roster rule (those check that a route names a
gate, not what the gate was given). ``Sensitivity`` is a closed ``Literal``,
so the bypass is always a string constant.

A violation, in any module under ``src/trellis_api/routes/``, is one of:

* **keyword**: a keyword argument named ``clearance``, on any call, whose
  value holds a ``Sensitivity`` string constant anywhere inside it (so a
  ternary with one literal arm counts);
* **policed_call**: a call to one of :data:`POLICED_CALLS` carrying such a
  constant as any positional argument or inside any keyword value, however
  the callee is spelled (an ``import ... as`` rename or a rebinding does not
  hide it);
* **binding**: an assignment, annotated assignment or walrus whose target is
  a name ``clearance`` or ``*_clearance`` and whose value holds one;
* **param_default**: a function, method or lambda parameter named that way
  whose default is one, so a caller that omits it silently gets the literal.

A comparison is not a binding: ``clearance == "restricted"`` reads a label and
grants nothing, so the constants under an ``ast.Compare`` are never counted.
A parameter with no default is a pass-through and is clean.

What it does not see: a literal that reaches a clearance through another name
(``LEVEL = "restricted"`` passed as ``clearance=LEVEL``), a ``clearance``
attribute assignment, and a literal built outside ``routes/``. The first two
are not seen because the scan is a name-and-constant check, not data flow.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, calls_named, iter_modules
from trellis.schemas.classification import SENSITIVITY_ORDER

ROUTES = Path(__file__).parents[2] / "src" / "trellis_api" / "routes"

#: The four ``Sensitivity`` values, derived from the type so a value added to
#: it is policed without an edit here.
SENSITIVITY_VALUES = frozenset(SENSITIVITY_ORDER)

#: Calls whose clearance argument decides what a request may read or write.
POLICED_CALLS = (
    "DocumentClearance",
    "readable_ids",
    "conceal_document_rows",
    "partition_by_clearance",
    "conceal_unreadable_successors",
    "refuse_overwrite",
    "build_curate_executor",
    "build_file_context",
)

#: 33 sites found by a grep recount at the branch point, written independently
#: of this scan: the 20 ``clearance=`` keywords (``grep -rn "clearance="`` over
#: ``routes/``), the 7 policed calls that carry no such keyword
#: (``grep -rnE "\b(<each policed name>)\("``: ``DocumentClearance`` x5 and
#: ``conceal_document_rows`` x2, in ``_principal.py``, ``explore.py`` and
#: ``retrieve.py``), the 4 ``clearance = request_clearance(ctx)`` bindings
#: (``grep -rnE "^\s+clearance = "``), and the 2 parameters
#: (``grep -rn "clearance: Sensitivity"``: ``_execute_command``'s keyword-only
#: one and ``_evidence_links``'s). Rounded down to 30 so a new compliant site
#: cannot turn the suite red. Counted outside this scan, never computed by it.
HAND_READ_CLEARANCE_SITE_COUNT = 30

#: ``"<module>:<function>"`` -> why that function may hold a literal. Empty,
#: and it may only shrink: a route that needs a fixed clearance is a design
#: question to answer in review, not a line to add here.
EXEMPT_ROUTE_CLEARANCE_LITERALS: dict[str, str] = {}

_MIN_REASON_WORDS = 4
_MIN_REASON_CHARS = 20


@dataclass(frozen=True)
class Violation:
    module: str
    function: str
    lineno: int
    shape: str
    snippet: str

    @property
    def key(self) -> str:
        return f"{self.module}:{self.function}"

    def describe(self) -> str:
        return f"{self.key}:{self.lineno} [{self.shape}] {self.snippet}"


@dataclass(frozen=True)
class Scan:
    violations: list[Violation]
    #: Sites the scan judged: each policed call or call carrying a
    #: ``clearance`` keyword, each clearance-named binding, each
    #: clearance-named parameter.
    sites: int


def _literals(expr: ast.AST) -> list[str]:
    """``Sensitivity`` constants anywhere in *expr* except under a comparison."""
    compared = {
        id(inner)
        for node in ast.walk(expr)
        if isinstance(node, ast.Compare)
        for inner in ast.walk(node)
    }
    return [
        node.value
        for node in ast.walk(expr)
        if id(node) not in compared
        and isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in SENSITIVITY_VALUES
    ]


def _is_clearance_name(name: str) -> bool:
    return name == "clearance" or name.endswith("_clearance")


#: What judging one node yields: the number of sites it was (0 or more, one
#: per clearance-named parameter) and the ``(node, shape)`` pairs it flagged.
Judgement = tuple[int, list[tuple[ast.AST, str]]]


def _judge_call(call: ast.Call, policed: set[int]) -> Judgement:
    clearance_kws = [kw for kw in call.keywords if kw.arg == "clearance"]
    is_policed = id(call) in policed
    if not (is_policed or clearance_kws):
        return 0, []
    if any(_literals(kw.value) for kw in clearance_kws):
        return 1, [(call, "keyword")]
    if is_policed:
        values = [*call.args, *(kw.value for kw in call.keywords)]
        if any(_literals(value) for value in values):
            return 1, [(call, "policed_call")]
    return 1, []


def _judge_binding(node: ast.Assign | ast.AnnAssign | ast.NamedExpr) -> Judgement:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if not any(isinstance(t, ast.Name) and _is_clearance_name(t.id) for t in targets):
        return 0, []
    if node.value is not None and _literals(node.value):
        return 1, [(node, "binding")]
    return 1, []


def _judge_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> Judgement:
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    # Positional defaults align to the right; ``None`` marks a keyword-only
    # parameter with no default.
    defaults: dict[ast.arg, ast.expr | None] = dict(
        zip(reversed(positional), reversed(args.defaults), strict=False)
    )
    defaults.update(zip(args.kwonlyargs, args.kw_defaults, strict=True))
    sites = 0
    flagged: list[tuple[ast.AST, str]] = []
    for arg in [*positional, *args.kwonlyargs]:
        if not _is_clearance_name(arg.arg):
            continue
        sites += 1
        default = defaults.get(arg)
        if default is not None and _literals(default):
            flagged.append((default, "param_default"))
    return sites, flagged


def inspect_source(source: str, filename: str) -> Scan:
    """Judge one module. The real tree and every subject below run through it."""
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    module = Path(filename).stem

    def enclosing(node: ast.AST) -> str:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current.name
            current = parents.get(current)
        return "<module>"

    policed = {
        id(call)
        for name in POLICED_CALLS
        for call in calls_named(name, tree, resolve=True)
    }
    violations: list[Violation] = []
    sites = 0
    for node in ast.walk(tree):
        judged: Judgement = (0, [])
        if isinstance(node, ast.Call):
            judged = _judge_call(node, policed)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            judged = _judge_binding(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            judged = _judge_parameters(node)
        sites += judged[0]
        violations.extend(
            Violation(
                module=module,
                function=enclosing(site),
                lineno=getattr(site, "lineno", 0),
                shape=shape,
                snippet=ast.unparse(site).replace("\n", " ")[:90],
            )
            for site, shape in judged[1]
        )
    return Scan(violations=violations, sites=sites)


def scan_source(source: str, filename: str) -> list[Violation]:
    return inspect_source(source, filename).violations


def scan_tree(root: Path) -> Scan:
    violations: list[Violation] = []
    sites = 0
    for path, _tree in iter_modules(root):
        scan = inspect_source(path.read_text(encoding="utf-8"), path.name)
        violations.extend(scan.violations)
        sites += scan.sites
    return Scan(violations=violations, sites=sites)


def check_floor(scan: Scan) -> None:
    assert_hand_read_floor(
        scan.sites,
        HAND_READ_CLEARANCE_SITE_COUNT,
        subject="route clearance site",
        hint="Recount by hand; if the routes really shrank, lower the floor.",
    )


def roster_problems(roster: dict[str, str], violations: list[Violation]) -> list[str]:
    """Entries that carry no written reason, or that no violation needs."""
    problems: list[str] = []
    found = {violation.key for violation in violations}
    for key, reason in roster.items():
        stripped = reason.strip()
        if (
            len(stripped) < _MIN_REASON_CHARS
            or len(stripped.split()) < _MIN_REASON_WORDS
        ):
            problems.append(f"{key}: no written reason")
        if key not in found:
            problems.append(f"{key}: stale, the scan finds no violation there")
    return problems


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------


def test_no_route_holds_a_literal_clearance() -> None:
    scan = scan_tree(ROUTES)
    remaining = [
        v for v in scan.violations if v.key not in EXEMPT_ROUTE_CLEARANCE_LITERALS
    ]
    assert not remaining, (
        "a REST route builds an executor or reads documents at a literal "
        "clearance, which reads and writes every label whoever calls it; "
        "take it from request_clearance(ctx): "
        + "; ".join(v.describe() for v in remaining)
    )


def test_the_scan_inspects_at_least_the_hand_counted_sites() -> None:
    check_floor(scan_tree(ROUTES))


def test_a_scan_that_matches_nothing_fails_the_floor(tmp_path: Path) -> None:
    (tmp_path / "empty.py").write_text("x = 1\n", encoding="utf-8")
    scan = scan_tree(tmp_path)
    assert scan.sites == 0
    with pytest.raises(AssertionError, match="below the hand-read floor"):
        check_floor(scan)


def test_the_unrelated_health_constant_is_not_flagged() -> None:
    path = ROUTES / "health.py"
    source = path.read_text(encoding="utf-8")
    # The subject must still exist for this to prove anything.
    assert 'OPS_DETAIL_PUBLIC = "public"' in source
    assert scan_source(source, path.name) == []


def test_a_real_route_with_a_literal_is_flagged(tmp_path: Path) -> None:
    source = (ROUTES / "curate.py").read_text(encoding="utf-8")
    spelling = "clearance=request_clearance(ctx)"
    assert spelling in source, "curate.py no longer spells the clearance this way"
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "curate.py").write_text(
        source.replace(spelling, 'clearance="restricted"', 1), encoding="utf-8"
    )
    violations = scan_tree(tmp_path).violations
    assert len(violations) == 1
    assert violations[0].module == "curate"
    assert violations[0].shape == "keyword"


# ---------------------------------------------------------------------------
# Defective subjects: each banned shape, exactly one violation
# ---------------------------------------------------------------------------

_DEFECTIVE = [
    pytest.param(
        "from trellis.mutate import build_curate_executor\n"
        "def route(registry):\n"
        '    return build_curate_executor(registry, clearance="restricted")\n',
        [("route", "keyword")],
        id="policed-keyword",
    ),
    pytest.param(
        "from trellis.mutate import build_curate_executor as build\n"
        "def route(registry):\n"
        '    return build(registry, clearance="restricted")\n',
        [("route", "keyword")],
        id="aliased-import-keyword",
    ),
    pytest.param(
        "from trellis.stores import DocumentClearance as Gate\n"
        "def route(store):\n"
        '    return Gate(store, "restricted")\n',
        [("route", "policed_call")],
        id="aliased-import-positional",
    ),
    pytest.param(
        "from trellis.stores import DocumentClearance\n"
        "Gate = DocumentClearance\n"
        "def route(store):\n"
        '    return Gate(store, "internal")\n',
        [("route", "policed_call")],
        id="rebinding-positional",
    ),
    pytest.param(
        'def route(store):\n    return DocumentClearance(store, "restricted")\n',
        [("route", "policed_call")],
        id="positional",
    ),
    pytest.param(
        "def route(store, ids):\n"
        '    return readable_ids(store, ids, clearance="confidential")\n',
        [("route", "keyword")],
        id="readable-ids-keyword",
    ),
    pytest.param(
        "def route(store, docs):\n"
        '    return conceal_document_rows(docs, store, "public")\n',
        [("route", "policed_call")],
        id="conceal-rows-positional",
    ),
    pytest.param(
        "def route(registry):\n"
        '    clearance = "restricted"\n'
        "    return build_curate_executor(registry, clearance=clearance)\n",
        [("route", "binding")],
        id="binding-then-passed-on",
    ),
    pytest.param(
        "def route(registry):\n"
        '    clearance: Sensitivity = "internal"\n'
        "    return build_curate_executor(registry, clearance=clearance)\n",
        [("route", "binding")],
        id="annotated-binding",
    ),
    pytest.param(
        "def route(registry):\n"
        '    if (read_clearance := "public"):\n'
        "        return read_clearance\n",
        [("route", "binding")],
        id="walrus-binding",
    ),
    pytest.param(
        "def route(registry, ctx, flag):\n"
        "    return helper(\n"
        '        clearance="restricted" if flag else request_clearance(ctx)\n'
        "    )\n",
        [("route", "keyword")],
        id="ternary-literal-arm",
    ),
    pytest.param(
        'def route(clearance: Sensitivity = "restricted"):\n    return clearance\n',
        [("route", "param_default")],
        id="parameter-default",
    ),
    pytest.param(
        "def route(*, read_clearance: Sensitivity = 'internal'):\n"
        "    return read_clearance\n",
        [("route", "param_default")],
        id="keyword-only-suffixed-parameter-default",
    ),
    pytest.param(
        "def route():\n"
        '    make = lambda clearance="restricted": clearance\n'
        "    return make\n",
        [("route", "param_default")],
        id="lambda-parameter-default",
    ),
    pytest.param(
        "def route(registry):\n"
        "    def inner():\n"
        '        return build_curate_executor(registry, clearance="restricted")\n'
        "    return inner\n",
        [("inner", "keyword")],
        id="nested-function",
    ),
    pytest.param(
        "def route(registry):\n"
        '    return lambda: build_curate_executor(registry, clearance="restricted")\n',
        [("route", "keyword")],
        id="lambda-body",
    ),
    pytest.param(
        "def route(registry):\n"
        "    try:\n"
        "        return build_curate_executor(\n"
        "            registry, clearance=request_clearance(ctx)\n"
        "        )\n"
        "    except KeyError:\n"
        '        return build_curate_executor(registry, clearance="restricted")\n',
        [("route", "keyword")],
        id="except-handler",
    ),
    pytest.param(
        "async def route(registry):\n"
        '    return build_curate_executor(registry, clearance="restricted")\n',
        [("route", "keyword")],
        id="async-route",
    ),
    pytest.param(
        'executor = build_curate_executor(None, clearance="restricted")\n',
        [("<module>", "keyword")],
        id="module-scope",
    ),
]


@pytest.mark.parametrize(("source", "expected"), _DEFECTIVE)
def test_each_banned_shape_is_flagged_exactly_once(
    source: str, expected: list[tuple[str, str]]
) -> None:
    found = [(v.function, v.shape) for v in scan_source(source, "subject.py")]
    assert found == expected


@pytest.mark.parametrize("value", ["public", "internal", "confidential", "restricted"])
def test_every_sensitivity_value_is_a_literal(value: str) -> None:
    assert value in SENSITIVITY_VALUES
    source = f'def route(registry):\n    return helper(clearance="{value}")\n'
    assert [v.shape for v in scan_source(source, "subject.py")] == ["keyword"]


# ---------------------------------------------------------------------------
# Clean subjects: none of these may be flagged
# ---------------------------------------------------------------------------

_CLEAN = [
    pytest.param(
        "def route(registry, ctx):\n"
        "    executor = build_curate_executor(\n"
        "        registry, clearance=request_clearance(ctx)\n"
        "    )\n"
        "    return executor\n",
        id="request-clearance",
    ),
    pytest.param(
        "def route(registry, ctx):\n"
        "    clearance = request_clearance(ctx)\n"
        "    return build_curate_executor(registry, clearance=clearance)\n",
        id="variable-bound-from-request-clearance",
    ),
    pytest.param(
        "def run(cmd, *, clearance: Sensitivity):\n"
        "    return _execute_command(cmd, clearance=clearance)\n",
        id="pass-through-parameter",
    ),
    pytest.param(
        'def route(clearance):\n    if clearance == "restricted":\n        return 1\n',
        id="equality-comparison",
    ),
    pytest.param(
        'def route(gate):\n    return gate.clearance != "restricted"\n',
        id="inequality-comparison",
    ),
    pytest.param(
        "def route(registry, ctx, mode):\n"
        "    return build_curate_executor(\n"
        "        registry,\n"
        "        clearance=request_clearance(ctx),\n"
        '        strict=(mode == "restricted"),\n'
        "    )\n",
        id="comparison-inside-a-policed-call",
    ),
    pytest.param(
        "def route(ctx, other, mode):\n"
        '    clearance = request_clearance(ctx) if mode != "public" else other\n'
        "    return clearance\n",
        id="comparison-inside-a-binding",
    ),
    pytest.param(
        'OPS_DETAIL_PUBLIC = "public"\n',
        id="unrelated-module-constant",
    ),
    pytest.param(
        'def route(registry):\n    logger.info("restricted")\n    return registry\n',
        id="literal-to-an-unrelated-call",
    ),
    pytest.param(
        "def route(clearance=None, other='restricted'):\n    return clearance\n",
        id="non-clearance-default",
    ),
]


@pytest.mark.parametrize("source", _CLEAN)
def test_a_compliant_shape_is_not_flagged(source: str) -> None:
    assert scan_source(source, "subject.py") == []


# ---------------------------------------------------------------------------
# The roster
# ---------------------------------------------------------------------------


def _violation(module: str = "curate", function: str = "route") -> Violation:
    return Violation(module, function, 1, "keyword", "helper(clearance='restricted')")


def test_the_roster_starts_empty_and_is_clean() -> None:
    scan = scan_tree(ROUTES)
    assert roster_problems(EXEMPT_ROUTE_CLEARANCE_LITERALS, scan.violations) == []
    assert EXEMPT_ROUTE_CLEARANCE_LITERALS == {}


def test_a_roster_entry_with_no_reason_fails() -> None:
    reasons = ["", "   ", "because", "short reason  " + " " * 30]
    for reason in reasons:
        problems = roster_problems({"curate:route": reason}, [_violation()])
        assert problems == ["curate:route: no written reason"], reason


def test_a_roster_entry_with_no_violation_is_stale() -> None:
    roster = {"curate:gone": "a fixed clearance is required here by design"}
    problems = roster_problems(roster, [_violation()])
    assert problems == ["curate:gone: stale, the scan finds no violation there"]


def test_a_justified_roster_entry_over_a_real_violation_is_accepted() -> None:
    roster = {"curate:route": "a fixed clearance is required here by design"}
    assert roster_problems(roster, [_violation()]) == []
