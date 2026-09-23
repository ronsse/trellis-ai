"""``READ_POINTS`` is a claim about code, derived here from the code.

:data:`trellis.ops.parameter_reachability.READ_POINTS` decides whether a
tuner proposal is refused.  It is hand-written, so it can drift out from
under the tree it describes, and the drift that costs is silent in the
worst direction: a reader that *gains* an axis while the roster still
declares it narrow makes the screen refuse proposals that would now land.
A declaration nothing cross-checks is #512 one layer down.

Three properties, each derived from the AST of ``src/``:

R1  Every declared ``resolvable_axes`` equals the axes the reader really
    supplies to its ``ParameterScope``, unioned over *every* read point in
    the tree that can reach that component — not just the ones in the
    declared module, because a second reader elsewhere is exactly how the
    roster goes stale in the expensive direction.

R2  Every declared gated key really sits behind a guard on its declared
    axis, and no *other* key in the same reader does.  ``curated_boost`` is
    the negative control: same resolution shape, same function, guarded
    too — but on a node property rather than on a learning axis, so a rule
    that keys on "is used inside an ``if``" classifies it as gated and
    fails here.

R3  No read point in ``src/`` is missing from the roster.

The vacuity guards follow ``tests/ast_rules.py``: a hand-read floor the
scan cannot compute for itself, and negative controls the scan has to
*not* report.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, name_of
from trellis.ops.parameter_reachability import LEARNING_AXES, READ_POINTS

SRC = Path(__file__).resolve().parents[2] / "src"

#: The registry methods that take a :class:`ParameterScope` as their first
#: argument.  Both are on ``ParameterRegistry``; ``get`` reads one key,
#: ``get_values`` the whole resolved set.
_READER_METHODS = frozenset({"get", "get_values"})

#: Hand-read off the tree on 2026-09-23, by eye and not by this scan:
#: 12 textual ``registry.get`` / ``registry.get_values`` sites in 10
#: modules, serving 11 declared components. The scan reports *more* than
#: 12, because ``learning/scoring.py``'s single textual site reads a
#: parameter and is counted at its four callers instead — 12 is the floor
#: precisely because it is the number a person can recount without
#: running this scan.
_FLOOR_READ_POINTS = 12
_FLOOR_READER_MODULES = 10
_FLOOR_COMPONENTS = 11


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadSite:
    """One resolved ``registry.get(ParameterScope(...), ...)`` call."""

    module: str
    lineno: int
    #: ``None`` when the component id is an expression this scan cannot
    #: fold to a literal — a function parameter, typically, which means the
    #: site serves every component routed through it.
    component_id: str | None
    axes: frozenset[str]


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_bindings(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level ``NAME = <expr>`` bindings, for constant folding.

    The value is kept as an expression rather than a string because two of
    the five folds in this tree go through a *rename* — ``_COMPONENT_ID =
    MMR_RERANKER_COMPONENT_ID`` — so a map of literals alone resolves
    neither reranker.
    """
    out: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = value
    return out


def _import_maps(tree: ast.Module) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """``from x import N [as a]`` and ``import x [as a]`` bindings."""
    from_imports: dict[str, tuple[str, str]] = {}
    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                from_imports[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name] = alias.name
    return from_imports, module_aliases


class _Resolver:
    """Folds a component-id expression to a string literal, or gives up.

    Giving up is a first-class answer: ``component_id`` is a *parameter* at
    two of the twelve read points, and a site that serves whatever it is
    handed serves every component routed through it.  Guessing a value
    there would manufacture exactly the false precision this module refuses
    on the unknown-component side.
    """

    def __init__(self) -> None:
        self._trees: dict[str, ast.Module] = {}
        self._bindings: dict[str, dict[str, ast.expr]] = {}
        self._imports: dict[str, tuple[dict[str, tuple[str, str]], dict[str, str]]] = {}

    def load(self, module: str) -> ast.Module | None:
        if module in self._trees:
            return self._trees[module]
        rel = module.replace(".", "/")
        for candidate in (SRC / f"{rel}.py", SRC / rel / "__init__.py"):
            if candidate.is_file():
                tree = ast.parse(candidate.read_text())
                self._trees[module] = tree
                self._bindings[module] = _module_bindings(tree)
                self._imports[module] = _import_maps(tree)
                return tree
        return None

    def resolve(self, module: str, node: ast.expr, *, depth: int = 0) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if depth > 4 or self.load(module) is None:
            return None
        if isinstance(node, ast.Name):
            return self._resolve_name(module, node, depth)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return self._resolve_attribute(module, node, depth)
        return None

    def _resolve_name(self, module: str, node: ast.Name, depth: int) -> str | None:
        bindings = self._bindings[module]
        if node.id in bindings:
            # Recurse: the binding may itself be a rename of an import.
            return self.resolve(module, bindings[node.id], depth=depth + 1)
        from_imports, _ = self._imports[module]
        if node.id not in from_imports:
            return None
        origin, original = from_imports[node.id]
        return self.resolve(
            origin, ast.Name(id=original, ctx=ast.Load()), depth=depth + 1
        )

    def _resolve_attribute(
        self, module: str, node: ast.Attribute, depth: int
    ) -> str | None:
        assert isinstance(node.value, ast.Name)
        alias = node.value.id
        from_imports, module_aliases = self._imports[module]
        origin = module_aliases.get(alias)
        if origin is None and alias in from_imports:
            # ``from a import b`` then ``b.NAME`` — ``b`` is a module.
            pkg, leaf = from_imports[alias]
            origin = f"{pkg}.{leaf}"
        if origin is None:
            return None
        return self.resolve(
            origin, ast.Name(id=node.attr, ctx=ast.Load()), depth=depth + 1
        )


def _scope_construction(node: ast.expr) -> ast.Call | None:
    if isinstance(node, ast.Call) and name_of(node.func) == "ParameterScope":
        return node
    return None


def _scope_bindings(
    tree: ast.Module,
) -> tuple[dict[int, int | None], dict[int | None, dict[str, ast.Call]]]:
    """``name = ParameterScope(...)`` bindings, scoped to their function.

    Returns ``(owner, binds)``: ``owner`` maps a node to the id of its
    enclosing ``FunctionDef`` (``None`` at module level), ``binds`` maps
    that id to the scope constructions bound in it.

    Function scope is load-bearing rather than tidy, and it was measured
    to be: ``trellis_cli/classify.py`` binds the name ``scope`` in two
    different functions, to two different components, and a tree-wide map
    let the second overwrite the first — so the scan attributed the tag
    reader's site to the domain-normalization component.  Both folds are
    now correct.  ``learning/scoring.py`` is the other half of the same
    hazard: a helper whose *parameter* is named ``scope`` beside a caller
    that binds one, which a tree-wide map resolves by accident.
    """
    owner: dict[int, int | None] = {}
    binds: dict[int | None, dict[str, ast.Call]] = {None: {}}

    def walk(node: ast.AST, current: int | None) -> None:
        for child in ast.iter_child_nodes(node):
            owner[id(child)] = current
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                binds.setdefault(id(child), {})
                walk(child, id(child))
                continue
            if isinstance(child, ast.Assign):
                call = _scope_construction(child.value)
                if call is not None:
                    for target in child.targets:
                        if isinstance(target, ast.Name):
                            binds[current][target.id] = call
            walk(child, current)

    walk(tree, None)
    return owner, binds


def _axes_supplied(call: ast.Call) -> frozenset[str]:
    """Axis kwargs the construction supplies with something other than ``None``."""
    axes = set()
    for kw in call.keywords:
        if kw.arg not in LEARNING_AXES:
            continue
        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
            continue
        axes.add(kw.arg)
    return frozenset(axes)


def _reader_sinks(tree: ast.Module) -> dict[str, set[int]]:
    """``{helper name: {parameter positions used as a read scope}}``.

    A scope does not have to be constructed at the call that reads it.
    ``learning/scoring.py`` builds one in the caller and hands it to
    ``_resolve_required_threshold``, which is where ``registry.get`` runs
    — so a scan that follows only inline constructions and local bindings
    reports that module as parametric and R3 stops policing it.
    """
    sinks: dict[str, set[int]] = {}
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        params = [a.arg for a in func.args.args]
        for node in ast.walk(func):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if name_of(node.func) not in _READER_METHODS:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in params:
                sinks.setdefault(func.name, set()).add(params.index(first.id))
    return sinks


def _scan_read_sites() -> list[ReadSite]:
    resolver = _Resolver()
    sites: list[ReadSite] = []
    for path in sorted(SRC.rglob("*.py")):
        module = _module_name(path)
        tree = ast.parse(path.read_text())
        owner, binds = _scope_bindings(tree)
        sinks = _reader_sinks(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            callee = name_of(node.func)
            if callee in _READER_METHODS:
                positions = [0]
            elif callee in sinks:
                # The sink's own body reads a *parameter*, which resolves to
                # nothing; its callers are where a scope is constructed.
                positions = sorted(sinks[callee])
            else:
                continue
            scope_id = owner.get(id(node))
            local = binds.get(scope_id, {})
            for pos in positions:
                if pos >= len(node.args):
                    continue
                arg = node.args[pos]
                call = _scope_construction(arg)
                if call is None and isinstance(arg, ast.Name):
                    call = local.get(arg.id) or binds[None].get(arg.id)
                if call is None:
                    continue
                component = next(
                    (
                        resolver.resolve(module, kw.value)
                        for kw in call.keywords
                        if kw.arg == "component_id"
                    ),
                    None,
                )
                sites.append(
                    ReadSite(
                        module=module,
                        lineno=node.lineno,
                        component_id=component,
                        axes=_axes_supplied(call),
                    )
                )
    return sites


READ_SITES = _scan_read_sites()


def _sites_for(component_id: str, reader_module: str) -> list[ReadSite]:
    """Every site that can reach *component_id*.

    A site whose component id folded to a literal reaches that component
    and no other.  A site whose component id is parametric reaches every
    component the roster routes through that module — which is what makes
    one ``_resolve_param`` serve three search strategies.
    """
    return [
        s
        for s in READ_SITES
        if s.component_id == component_id
        or (s.component_id is None and s.module == reader_module)
    ]


# ---------------------------------------------------------------------------
# Vacuity guards
# ---------------------------------------------------------------------------


def test_scan_meets_its_hand_read_floor() -> None:
    """The scan still finds what a person counted off the tree."""
    assert_hand_read_floor(
        len(READ_SITES),
        _FLOOR_READ_POINTS,
        subject="parameter read",
        hint="Re-read `registry.get(` / `registry.get_values(` call sites in src/.",
    )
    assert_hand_read_floor(
        len({s.module for s in READ_SITES}),
        _FLOOR_READER_MODULES,
        subject="parameter-reading module",
    )
    assert_hand_read_floor(
        len(READ_POINTS),
        _FLOOR_COMPONENTS,
        subject="declared read point",
    )


def test_scan_resolves_all_three_site_shapes() -> None:
    """Inline, one-hop binding and interprocedural sink are each really used.

    A scan that silently handled only the inline shape would still pass a
    floor set against the total, because all three are present in numbers
    — so each is asserted for by the module that can only be reached that
    way.
    """
    modules = {s.module for s in READ_SITES}
    with_axis = {s.module for s in READ_SITES if s.axes}
    assert with_axis == {"trellis.retrieve.strategies"}, (
        f"inline construction with a supplied axis: expected the strategies "
        f"reader and nothing else, got {sorted(with_axis)}"
    )
    assert "trellis.retrieve.rerankers.mmr" in modules, (
        "one-hop `scope = ParameterScope(...)` bindings are no longer resolved"
    )
    assert "trellis.learning.scoring" in modules, (
        "the interprocedural sink hop is no longer resolved; scoring builds "
        "its scope in the caller and reads it in a helper"
    )


def test_component_id_resolution_is_not_vacuous() -> None:
    """Constant folding really crosses a module boundary.

    Every literal fold in this tree goes through an imported constant, so a
    resolver that only read same-module literals would return ``None``
    everywhere and R3 would have nothing to check.
    """
    folded = [s for s in READ_SITES if s.component_id is not None]
    assert len(folded) >= 8, (
        f"only {len(folded)} of {len(READ_SITES)} read sites folded to a "
        "literal component id; the constant resolver has stopped resolving."
    )
    cross_module = [
        s for s in folded if s.module != f"trellis.{s.component_id.split('.')[0]}"
    ]
    assert cross_module, "no cross-module constant fold — resolver is same-file only"


# ---------------------------------------------------------------------------
# R1 — declared axes equal supplied axes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("component_id", sorted(READ_POINTS))
def test_declared_axes_match_the_reader(component_id: str) -> None:
    read_point = READ_POINTS[component_id]
    sites = _sites_for(component_id, read_point.reader_module)
    assert sites, (
        f"{component_id} declares a read point in {read_point.reader_module} "
        "but no call in src/ resolves parameters for it. Either the reader "
        "was removed (drop the entry) or the scan no longer sees it."
    )
    # Union, not intersection: a snapshot only has to be reachable at *one*
    # read point to be able to alter behaviour, and the screen refuses only
    # on positive evidence that it cannot.
    supplied = frozenset().union(*(s.axes for s in sites))
    assert supplied == read_point.resolvable_axes, (
        f"{component_id}: READ_POINTS declares resolvable_axes="
        f"{sorted(read_point.resolvable_axes)} but the readers supply "
        f"{sorted(supplied)} "
        f"(at {', '.join(f'{s.module}:{s.lineno}' for s in sites)}). "
        "Declaring an axis the reader does not supply admits dead "
        "proposals; declaring fewer than it supplies refuses live ones."
    )


def test_no_reader_supplies_intent_family_or_tool_name() -> None:
    """The #617 premise, pinned rather than remembered.

    This is the whole reason the screen has work to do. If it ever fails,
    the deferred intent-family awareness has landed and the roster — and
    the tuner's own scope — want re-reading against it.
    """
    offenders = [
        (s.module, s.lineno, sorted(s.axes))
        for s in READ_SITES
        if s.axes & {"intent_family", "tool_name"}
    ]
    assert not offenders, (
        f"a reader now supplies intent_family/tool_name: {offenders}. "
        "Update READ_POINTS (and re-read the tuner's emit scope) — the "
        "screen is currently refusing proposals at those axes."
    )


# ---------------------------------------------------------------------------
# R2 — declared gated keys sit behind the guard they declare
# ---------------------------------------------------------------------------


def _resolve_param_signature(tree: ast.Module) -> list[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_param":
            return [a.arg for a in node.args.args]
    return []


@dataclass(frozen=True)
class _KeyBinding:
    """``name = _resolve_param(registry, component, <axis_expr>, "key", ...)``."""

    name: str
    key: str
    axis_expr: str
    function: ast.FunctionDef


def _key_bindings(tree: ast.Module) -> list[_KeyBinding]:
    """Every local bound to a ``_resolve_param`` call, with its axis argument.

    The axis argument is found by matching the call against
    ``_resolve_param``'s own signature rather than by position alone, so
    re-ordering the parameters breaks the rule loudly instead of silently
    reading the wrong argument as the domain.
    """
    params = _resolve_param_signature(tree)
    if "domain" not in params or "key" not in params:
        return []
    domain_index = params.index("domain")
    key_index = params.index("key")

    out: list[_KeyBinding] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if name_of(call.func) != "_resolve_param":
                continue
            args = dict(enumerate(call.args))
            for kw in call.keywords:
                if kw.arg in params:
                    args[params.index(kw.arg)] = kw.value
            key_node = args.get(key_index)
            axis_node = args.get(domain_index)
            if (
                not isinstance(key_node, ast.Constant)
                or not isinstance(key_node.value, str)
                or axis_node is None
            ):
                continue
            out.extend(
                _KeyBinding(
                    name=target.id,
                    key=key_node.value,
                    axis_expr=ast.unparse(axis_node),
                    function=func,
                )
                for target in node.targets
                if isinstance(target, ast.Name)
            )
    return out


def _guarded_on(binding: _KeyBinding) -> bool:
    """Is every load of the bound name inside an ``if`` testing the axis?

    Scoped to the binding's own function, because ``floor`` and
    ``half_life`` are also *parameter* names elsewhere in this module and a
    module-wide name scan reads those as loads of this binding.
    """
    loads: list[ast.Name] = [
        n
        for n in ast.walk(binding.function)
        if isinstance(n, ast.Name)
        and n.id == binding.name
        and isinstance(n.ctx, ast.Load)
    ]
    if not loads:
        return False
    guarded_lines: set[int] = set()
    for node in ast.walk(binding.function):
        if not isinstance(node, ast.If):
            continue
        test_names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if binding.axis_expr not in test_names:
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if hasattr(inner, "lineno"):
                    guarded_lines.add(inner.lineno)
    return all(load.lineno in guarded_lines for load in loads)


def _derived_gated_keys(reader_module: str) -> dict[str, str]:
    rel = reader_module.replace(".", "/")
    tree = ast.parse((SRC / f"{rel}.py").read_text())
    return {b.key: "domain" for b in _key_bindings(tree) if _guarded_on(b)}


def test_declared_gated_keys_are_really_gated() -> None:
    """Each declared gated key's value is applied only behind its axis."""
    by_module: dict[str, dict[str, str]] = {}
    checked = 0
    for read_point in READ_POINTS.values():
        if not read_point.gated_keys:
            continue
        derived = by_module.setdefault(
            read_point.reader_module, _derived_gated_keys(read_point.reader_module)
        )
        for key, axis in read_point.gated_keys.items():
            checked += 1
            assert derived.get(key) == axis, (
                f"{read_point.component_id}: READ_POINTS declares {key!r} "
                f"gated on {axis!r}, but every load of it in "
                f"{read_point.reader_module} is not behind an `if` testing "
                "that axis. A key declared gated and applied unconditionally "
                "makes the screen refuse a proposal that would have worked."
            )
    assert checked, "no gated key declared — R2 is checking nothing"


def test_ungated_sibling_is_not_reported_as_gated() -> None:
    """``curated_boost`` is the negative control, and it is a real one.

    It is resolved by the same helper, in the same function, from the same
    component, and it *is* used inside an ``if`` — so a rule that keys on
    "used inside a conditional" reports it and a rule that keys on the
    *axis* does not. Without it, a predicate that returns ``True`` for
    every binding passes the whole of R2.
    """
    derived = _derived_gated_keys("trellis.retrieve.strategies")
    assert "domain_match_boost" in derived, (
        "the positive case stopped being detected; R2's negative control "
        "below would then pass vacuously."
    )
    assert "curated_boost" not in derived, (
        "curated_boost is guarded on a node property, not on the domain "
        "axis — reporting it as gated would refuse live proposals for it."
    )
    # Unconditional siblings must not be reported either.
    assert "position_decay_step" not in derived


# ---------------------------------------------------------------------------
# R3 — nothing in the tree is missing from the roster
# ---------------------------------------------------------------------------


def test_every_resolved_component_is_declared() -> None:
    """A read point the roster has never heard of is drift, not a deployment.

    The unknown-component posture (no claim, ``unchecked``) exists for
    components living *outside* this tree. One inside it that nobody
    declared is the roster going stale, and it reads as "not checked" on
    every surface while looking exactly like a supported third party.
    """
    resolved = {s.component_id for s in READ_SITES if s.component_id is not None}
    missing = sorted(resolved - set(READ_POINTS))
    assert not missing, (
        f"components read parameters in src/ but are absent from "
        f"READ_POINTS: {missing}. Add them (with the axes their reader "
        "supplies) or the screen will silently decline to judge them."
    )


def test_every_parametric_reader_module_is_declared() -> None:
    """No module resolves parameters *parametrically* without being rostered.

    This is the half that catches a new reader whose component id never
    folds to a literal, which the test above cannot see. A module whose
    sites all fold is deliberately *not* required to be a declared
    ``reader_module``: ``trellis_cli`` reads two already-declared
    components at their own seams, and requiring a roster entry per
    reading module would make the roster track call sites rather than
    components.
    """
    declared = {rp.reader_module for rp in READ_POINTS.values()}
    parametric = {s.module for s in READ_SITES if s.component_id is None}
    assert parametric, (
        "no parametric read site found — this guard is checking nothing, "
        "which means the component-id resolver has started guessing."
    )
    unknown = sorted(parametric - declared)
    assert not unknown, (
        f"these modules resolve parameters for a component the scan cannot "
        f"name, and back no declared read point: {unknown}. A parametric "
        "reader serves every component routed through it, so an unrostered "
        "one is a component the screen will silently decline to judge."
    )
