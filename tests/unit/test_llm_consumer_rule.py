"""Every ``build_llm_client(...)`` in ``src/`` must name its ``LLMConsumer``.

``consumer`` picks the ``llm.routes`` entry, and its default — ``None`` — is
the parent ``llm:`` block. That default is what makes tiered routing
*silently optional*: a call site that forgets ``consumer=`` builds from the
parent exactly as it did before tiers existed, so the omission is
indistinguishable from a pre-tier deployment at every level. Nothing raises,
nothing logs, and ``trellis admin llm-routes`` keeps printing the
``reconcile: deep`` the operator declared — a route that is real in the
config, real in the output, and reaching no code.

**The one-line fix is not the deliverable; preventing the seventh site is.**
Six call sites build a client today and two more forward a consumer to one of
them. The next one will be added by someone who has never read
``trellis/llm/routing.py``, and it will work — on the parent block, on
whatever tier the operator did not assign it.

A roster is not an option, and this repo has the receipts: #443 declared 3
control keys against 6 ``pop`` sites, and #457's hand-written scanner dropped
148 branches to 123 while all three of its vacuity guards stayed green. So
the population is derived by AST off ``src/`` on every run, and the rule is
checked against a synthetic corpus of evasions rather than against today's
answer.

Four invariants, all over ``src/`` only:

A. Every ``build_llm_client(...)`` names an ``LLMConsumer`` member, either
   directly or through a parameter of an enclosing function that its own
   callers name (a *forwarder* — ``mcp/server._build_llm_client`` and
   ``worker._require_llm_client_or_exit`` are the two that exist).
B. Every member of ``LLMConsumer`` is named by at least one site. A member
   nothing builds for is a route an operator can declare and never reach —
   the same silence, one layer up.
C. Nothing mentions the builder or a forwarder except to call it. A name
   that escapes into a variable, a ``functools.partial`` or a ``getattr``
   string is a construction the walk above cannot follow.
D. Nothing builds a provider client around the router. ``OpenAIClient`` and
   ``AnthropicClient`` are constructed in exactly two places, and
   ``_build_llm_client_from_route`` / ``_build_llm_client_from_env`` are each
   called from exactly one; a further construction would build a client with
   no consumer at all and no route to resolve.

Tests are excluded on purpose: a test that pins the pre-tier behaviour has to
be able to call the builder without a consumer, and that is the one place
where the parent block is the point.

Guarding against vacuity is the other half, because a scan that finds nothing
passes every rule above. Four pins: floors on the sites found (hand-read off
``src/``, so a scan that merely shrinks fails), the shared evasion roster in
``tests/ast_rules.py`` run through the *shipped* predicates for both the
builder and the provider classes, a hand-written corpus of the shapes that
roster does not cover — a forwarder, a rebound parameter, a decorator and a
default whose scope resolves outward — and ``TestTheRulePremiseStillHolds``,
which proves ``consumer`` is still keyword-only and still defaults to
``None``: a member default would make an omitted consumer silently share that
tier, and every reason printed below would be a lie.

The shared roster is complementary, not redundant. It covers *spelling and
binding* — aliases, rebinding chains, subclasses, walrus — uniformly for
every rule in this repo; this file's own corpus covers what is specific to a
consumer, which is how the argument is *resolved* once the call is found.

Two residue shapes are exempted from the roster guard and closed by invariant
C instead, which is what a residue exemption is for: ``partial_binding``
binds the value of a call, so there is no name to resolve, and
``cross_module_subclass`` defines the subclass in another module. Both are a
*mention* of the subject that is not a call to it, and C fails on any such
mention anywhere in ``src/``.

Honest limits. A computed string — ``getattr(registry, "build_" +
"llm_client")`` — is the genuine static residue, and nothing here sees it;
what bounds it is the premise, not the scan. A build that names no consumer
gets the *parent* block, never another consumer's tier, so the worst such a
site can do is run unrouted — never run on the wrong tier. Forwarders are
matched by bare name, so a same-named method on an unrelated class would be
over-collected; that failure is loud (a spurious violation naming its file
and line), not silent, which is the direction to be wrong in.
"""

from __future__ import annotations

import ast
import functools
import inspect
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from tests.ast_rules import (
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    calls_to_any,
    construction_names,
    name_of,
    render_evasion_corpus,
)
from trellis.llm.routing import LLMConsumer
from trellis.stores.registry import StoreRegistry

#: The builder every LLM call site in ``src/`` has to go through.
_SUBJECT = "build_llm_client"
#: Its keyword-only parameter, which selects the route.
_KWARG = "consumer"
#: The enum that parameter must be spelled with.
_ENUM = "LLMConsumer"

#: Invariant D's allow-list: ``{name: {(src-relative module, function)}}``.
#: A provider class may only be constructed inside the private builder a
#: resolved route reaches, and each private builder may only be called from
#: the one public entry point that resolved that route.
_SANCTIONED: dict[str, set[tuple[str, str]]] = {
    "OpenAIClient": {
        ("trellis/stores/registry.py", "_build_llm_client_from_route"),
        ("trellis/mcp/server.py", "_build_llm_client_from_env"),
    },
    "AnthropicClient": {
        ("trellis/stores/registry.py", "_build_llm_client_from_route"),
        ("trellis/mcp/server.py", "_build_llm_client_from_env"),
    },
    "_build_llm_client_from_route": {
        ("trellis/stores/registry.py", "build_llm_client"),
    },
    "_build_llm_client_from_env": {
        ("trellis/mcp/server.py", "_build_llm_client"),
    },
}

#: Hand-read off ``src/`` on 2026-09-14: memory_ingest_hook, mcp/server,
#: session_capture/sweep, classify, worker, admin.
_BUILD_FLOOR = 6
#: Those six, plus the four calls into the two forwarders.
_NAMED_CONSUMER_FLOOR = 8
#: ``server._build_llm_client`` and ``worker._require_llm_client_or_exit``.
_FORWARDER_FLOOR = 2
#: Hand-read off ``src/`` on 2026-09-14, per subject. A renamed private
#: builder fails loudly here rather than quietly emptying invariant D.
_PROVIDER_FLOORS = {
    "OpenAIClient": 2,
    "AnthropicClient": 2,
    "_build_llm_client_from_route": 1,
    "_build_llm_client_from_env": 1,
}
#: ``assert_hand_read_floor`` refuses a floor below two without one.
_SOLE_SITE_REASONS = {
    "_build_llm_client_from_route": (
        "one call, from build_llm_client, is the whole invariant: the route "
        "is resolved there and nothing may build around it"
    ),
    "_build_llm_client_from_env": (
        "one call, from _build_llm_client, is the whole invariant: the env "
        "fallback is reachable only after the registry declined"
    ),
}


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"expected a src/ directory at {root}"
    return root


def _snippet(node: ast.AST) -> str:
    return ast.unparse(node).replace("\n", " ")[:90]


# ---------------------------------------------------------------------------
# Reading one module
# ---------------------------------------------------------------------------


@dataclass
class _Module:
    path: Path
    source: str
    tree: ast.Module

    @functools.cached_property
    def parents(self) -> dict[int, ast.AST]:
        return {
            id(child): node
            for node in ast.walk(self.tree)
            for child in ast.iter_child_nodes(node)
        }

    @functools.cached_property
    def enum_names(self) -> set[str]:
        return construction_names(_ENUM, self.tree)

    @functools.cached_property
    def build_names(self) -> set[str]:
        return construction_names(_SUBJECT, self.tree)


@functools.cache
def _sources(base: Path) -> tuple[tuple[Path, str], ...]:
    return tuple(
        (path, path.read_text(encoding="utf-8")) for path in sorted(base.rglob("*.py"))
    )


@functools.cache
def _parsed(path: Path, source: str) -> _Module:
    return _Module(path=path, source=source, tree=ast.parse(source))


def _modules(base: Path, names: Collection[str]) -> list[_Module]:
    """Parse only the modules whose text could possibly mention ``names``.

    Sound because every AST identifier requires its spelling to appear in the
    source, and worth doing: parsing all of ``src/`` costs 0.6s and the
    modules that matter here are single digits.
    """
    return [
        _parsed(path, text)
        for path, text in _sources(base)
        if any(name in text for name in names)
    ]


def _enclosing_scope(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    """The function, lambda or class whose *body* ``node`` sits in.

    Decorators, parameter defaults, base classes and annotations resolve
    **outward**: a decorator on ``def inner(consumer)`` runs before ``inner``
    has a ``consumer`` at all, so its argument belongs to whatever encloses
    ``inner``. A resolver that picked the decorated function would read a
    parameter that cannot reach it.
    """
    child = node
    parent = parents.get(id(child))
    while parent is not None:
        if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if any(child is statement for statement in parent.body):
                return parent
        elif isinstance(parent, ast.Lambda) and child is parent.body:
            return parent
        child, parent = parent, parents.get(id(parent))
    return None


def _is_method(function: ast.AST, parents: dict[int, ast.AST]) -> bool:
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    if not isinstance(parents.get(id(function)), ast.ClassDef):
        return False
    return not any(
        name_of(decorator) == "staticmethod" for decorator in function.decorator_list
    )


def _bound_name(node: ast.AST) -> str | None:
    """The name ``node`` binds, if it binds one."""
    if isinstance(node, ast.Name):
        return node.id if isinstance(node.ctx, ast.Store | ast.Del) else None
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    if isinstance(node, ast.alias):
        return (node.asname or node.name).split(".")[0]
    return None


def _rebinds(function: ast.AST, name: str) -> bool:
    body = getattr(function, "body", None)
    if not isinstance(body, list):
        return False
    return any(
        _bound_name(node) == name for statement in body for node in ast.walk(statement)
    )


@dataclass(frozen=True)
class _Parameter:
    #: Position a caller would pass it at, or ``None`` when keyword-only.
    index: int | None
    has_default: bool
    variadic: bool


def _parameter(
    function: ast.AST, name: str, parents: dict[int, ast.AST]
) -> _Parameter | None:
    """How ``name`` appears in ``function``'s signature, if it appears at all."""
    arguments = getattr(function, "args", None)
    if not isinstance(arguments, ast.arguments):
        return None
    if (arguments.vararg and arguments.vararg.arg == name) or (
        arguments.kwarg and arguments.kwarg.arg == name
    ):
        return _Parameter(index=None, has_default=False, variadic=True)
    receiver = 1 if _is_method(function, parents) else 0
    positional = [*arguments.posonlyargs, *arguments.args]
    first_default = len(positional) - len(arguments.defaults)
    for offset, argument in enumerate(positional):
        if argument.arg == name:
            return _Parameter(
                index=offset - receiver,
                has_default=offset >= first_default,
                variadic=False,
            )
    for argument, default in zip(
        arguments.kwonlyargs, arguments.kw_defaults, strict=True
    ):
        if argument.arg == name:
            return _Parameter(
                index=None, has_default=default is not None, variadic=False
            )
    return None


@dataclass(frozen=True)
class _Forwarder:
    """A function whose own callers supply the consumer it passes on."""

    name: str
    index: int | None
    parameter: str


@dataclass(frozen=True)
class _Violation:
    where: str
    line: int
    reason: str
    snippet: str

    @property
    def text(self) -> str:
        return f"{self.where}:{self.line}: {self.reason}: {self.snippet}"


@dataclass(frozen=True)
class _Reading:
    """What one argument expression turned out to be."""

    member: str | None = None
    forwarder: _Forwarder | None = None
    violation: str | None = None


def _unusable(name: str, scope: ast.AST, parameter: _Parameter | None) -> str | None:
    """Why ``name`` cannot be followed out to ``scope``'s callers."""
    if parameter is None:
        return f"{name} is a local or a closure, and can be anything, including None"
    checks = (
        (
            isinstance(scope, ast.Lambda),
            f"{name} is a lambda parameter, which cannot be followed to its callers",
        ),
        (
            parameter.variadic,
            f"{name} collects its caller's arguments, so no caller names a member",
        ),
        (
            parameter.has_default,
            (
                f"{name} has a default, so a caller that omits it builds "
                "from the parent llm: block"
            ),
        ),
        (
            parameter.index is not None and parameter.index < 0,
            f"{name} names the receiver, so a caller cannot pass a member there",
        ),
        (
            _rebinds(scope, name),
            f"{name} is rebound in the body, so a rebound parameter is a local",
        ),
    )
    for failed, reason in checks:
        if failed:
            return reason
    return None


def _read_parameter(
    name: str, scope: ast.AST | None, parents: dict[int, ast.AST]
) -> _Reading:
    if scope is None or isinstance(scope, ast.ClassDef):
        return _Reading(violation=f"{name} is not a parameter of an enclosing function")
    parameter = _parameter(scope, name, parents)
    reason = _unusable(name, scope, parameter)
    if reason is not None or parameter is None:
        return _Reading(violation=reason)
    return _Reading(
        forwarder=_Forwarder(name=scope.name, index=parameter.index, parameter=name)
    )


def _constant_reason(value: object) -> str:
    if value is None:
        return f"{_KWARG}=None builds from the parent llm: block"
    if isinstance(value, str):
        return f"a string is checked only when it runs; spell it {_ENUM}.<MEMBER>"
    return f"a {type(value).__name__} constant is not an {_ENUM} member"


def _read_consumer(
    expr: ast.expr,
    scope: ast.AST | None,
    parents: dict[int, ast.AST],
    enum_names: set[str],
) -> _Reading:
    if isinstance(expr, ast.Attribute) and name_of(expr.value) in enum_names:
        if expr.attr in LLMConsumer.__members__:
            return _Reading(member=expr.attr)
        return _Reading(violation=f"{_ENUM} has no member {expr.attr}")
    if isinstance(expr, ast.Constant):
        return _Reading(violation=_constant_reason(expr.value))
    if isinstance(expr, ast.Name):
        return _read_parameter(expr.id, scope, parents)
    return _Reading(violation=f"{_snippet(expr)} is not a literal {_ENUM} member")


def _kwargs_splat(parameter: str) -> str:
    return f"a ** splat hides whether {parameter}= is passed"


def _build_value(call: ast.Call) -> ast.expr | str:
    for keyword in call.keywords:
        if keyword.arg == _KWARG:
            return keyword.value
    if any(keyword.arg is None for keyword in call.keywords):
        return _kwargs_splat(_KWARG)
    return f"{_KWARG}= is not passed, so it builds from the parent llm: block"


def _forwarded_value(call: ast.Call, forwarder: _Forwarder) -> ast.expr | str:
    for keyword in call.keywords:
        if keyword.arg == forwarder.parameter:
            return keyword.value
    if forwarder.index is not None:
        head = call.args[: forwarder.index + 1]
        if any(isinstance(node, ast.Starred) for node in head):
            return "a positional * splat hides which argument is the consumer"
        if len(call.args) > forwarder.index:
            return call.args[forwarder.index]
    if any(keyword.arg is None for keyword in call.keywords):
        return _kwargs_splat(forwarder.parameter)
    return (
        f"{forwarder.name}'s {forwarder.parameter} is not passed, so it builds "
        "from the parent llm: block"
    )


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scan:
    builds: int
    named: tuple[tuple[str, int, str], ...]
    forwarders: frozenset[_Forwarder]
    violations: tuple[_Violation, ...]


def _sites(
    modules: list[_Module], by_name: dict[str, _Forwarder], base: Path
) -> Iterator[tuple[str, _Module, ast.Call, ast.expr | str, bool]]:
    for module in modules:
        where = module.path.relative_to(base).as_posix()
        for call in calls_to_any(module.build_names, module.tree):
            yield where, module, call, _build_value(call), True
        if not by_name:
            continue
        for call in calls_to_any(set(by_name), module.tree):
            forwarder = by_name[str(name_of(call.func))]
            yield where, module, call, _forwarded_value(call, forwarder), False


def _scan_once(
    modules: list[_Module], known: frozenset[_Forwarder], base: Path
) -> _Scan:
    by_name = {forwarder.name: forwarder for forwarder in known}
    builds = 0
    named: dict[tuple[str, int, str], None] = {}
    violations: dict[tuple[str, int, str], _Violation] = {}
    discovered = set(known)
    for where, module, call, value, is_build in _sites(modules, by_name, base):
        builds += int(is_build)
        if isinstance(value, str):
            reading = _Reading(violation=value)
        else:
            scope = _enclosing_scope(call, module.parents)
            reading = _read_consumer(value, scope, module.parents, module.enum_names)
        if reading.member is not None:
            named[where, call.lineno, reading.member] = None
        elif reading.forwarder is not None:
            discovered.add(reading.forwarder)
        else:
            reason = str(reading.violation)
            violations.setdefault(
                (where, call.lineno, reason),
                _Violation(where, call.lineno, reason, _snippet(call)),
            )
    return _Scan(
        builds=builds,
        named=tuple(sorted(named)),
        forwarders=frozenset(discovered),
        violations=tuple(violations.values()),
    )


def _scan(root: Path | None = None) -> _Scan:
    """Read every build and every forwarded build, to a fixed point.

    Each pass can only *discover* forwarders, and a larger known set examines
    a superset of the calls, so the loop is monotone and terminates.
    """
    base = _src_root() if root is None else root
    forwarders: frozenset[_Forwarder] = frozenset()
    while True:
        names = {_SUBJECT, *(forwarder.name for forwarder in forwarders)}
        result = _scan_once(_modules(base, names), forwarders, base)
        if result.forwarders == forwarders:
            return result
        forwarders = result.forwarders


def consumer_violations(root: Path | None = None) -> list[_Violation]:
    """Every build in ``root`` (default ``src/``) that names no member."""
    return list(_scan(root).violations)


def _annotation_nodes(tree: ast.Module) -> set[int]:
    """Ids of every node inside an annotation, which is not a construction."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, ast.arg | ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotations.append(node.returns)
        for annotation in annotations:
            if annotation is not None:
                skip.update(id(child) for child in ast.walk(annotation))
    return skip


def stray_references(root: Path | None = None) -> list[str]:
    """Mentions of the builder, a forwarder or a private builder that are not calls.

    This is what closes the two residue shapes the shared roster leaves: a
    name bound to a variable, handed to ``functools.partial`` or used as a
    base class is a construction ``_scan`` cannot follow to a call site.
    """
    base = _src_root() if root is None else root
    names = {
        _SUBJECT,
        *(forwarder.name for forwarder in _scan(root).forwarders),
        *_SANCTIONED,
    }
    found: list[str] = []
    for module in _modules(base, names):
        where = module.path.relative_to(base).as_posix()
        callees = {
            id(node.func)
            for node in ast.walk(module.tree)
            if isinstance(node, ast.Call)
        }
        skip = _annotation_nodes(module.tree) | callees
        for node in ast.walk(module.tree):
            if id(node) in skip:
                continue
            mentioned = (
                name_of(node) in names
                if isinstance(node, ast.Name | ast.Attribute)
                else isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in names
            )
            if mentioned:
                found.append(f"{where}:{node.lineno}: {_snippet(node)}")
    return found


@dataclass(frozen=True)
class _ProviderSite:
    subject: str
    where: str
    line: int
    scope: str | None
    snippet: str

    @property
    def sanctioned(self) -> bool:
        return (self.where, self.scope) in _SANCTIONED[self.subject]

    @property
    def violation(self) -> _Violation:
        return _Violation(
            where=self.where,
            line=self.line,
            reason=(
                f"{self.subject} is constructed in "
                f"{self.scope or '<module scope>'}, which resolves no route"
            ),
            snippet=self.snippet,
        )


def _provider_sites(root: Path | None = None) -> list[_ProviderSite]:
    base = _src_root() if root is None else root
    sites: list[_ProviderSite] = []
    for module in _modules(base, _SANCTIONED):
        where = module.path.relative_to(base).as_posix()
        for subject in sorted(_SANCTIONED):
            names = construction_names(subject, module.tree)
            for call in calls_to_any(names, module.tree):
                scope = _enclosing_scope(call, module.parents)
                sites.append(
                    _ProviderSite(
                        subject=subject,
                        where=where,
                        line=call.lineno,
                        scope=scope.name
                        if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef)
                        else None,
                        snippet=_snippet(call),
                    )
                )
    return sites


def bypasses(root: Path | None = None) -> list[_Violation]:
    """Provider clients built outside the one function a route reaches."""
    return [site.violation for site in _provider_sites(root) if not site.sanctioned]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_every_build_names_its_consumer() -> None:
    scan = _scan()
    assert_hand_read_floor(
        scan.builds,
        _BUILD_FLOOR,
        subject=f"{_SUBJECT}() call sites in src/",
        hint=(
            "memory_ingest_hook, mcp/server, session_capture/sweep, classify, "
            "worker, admin"
        ),
    )
    assert_hand_read_floor(
        len(scan.forwarders),
        _FORWARDER_FLOOR,
        subject="functions that forward a consumer to the builder",
        hint="server._build_llm_client and worker._require_llm_client_or_exit",
    )
    violations = consumer_violations()
    assert not violations, (
        "A build that names no LLMConsumer runs on the parent llm: block, so "
        "an llm.routes entry declared for it is real in the config, printed "
        "by `trellis admin llm-routes`, and reaches no code. Pass "
        "consumer=LLMConsumer.<MEMBER>, adding a member in "
        "trellis/llm/routing.py if none fits.\n  "
        + "\n  ".join(violation.text for violation in violations)
    )


def test_every_consumer_is_built_for() -> None:
    scan = _scan()
    assert_hand_read_floor(
        len(scan.named),
        _NAMED_CONSUMER_FLOOR,
        subject=f"sites naming an {_ENUM} member",
        hint="six direct builds plus four calls into the two forwarders",
    )
    built_for = {member for _, _, member in scan.named}
    unbuilt = sorted(set(LLMConsumer.__members__) - built_for)
    assert not unbuilt, (
        "These members are routable and unreachable: an operator can declare "
        "llm.routes for one, see it echoed back, and no client will ever be "
        "built with it. Either build for it or delete it.\n  " + "\n  ".join(unbuilt)
    )


def test_nothing_references_the_builder_except_to_call_it() -> None:
    strays = stray_references()
    assert not strays, (
        "A builder that escapes into a name — a variable, a base class, a "
        "functools.partial, a getattr string — is built somewhere this scan "
        "cannot follow, so its consumer cannot be checked. Call it where it "
        "is used.\n  " + "\n  ".join(strays)
    )


def test_nothing_builds_a_provider_client_around_the_router() -> None:
    sites = _provider_sites()
    for subject, floor in sorted(_PROVIDER_FLOORS.items()):
        assert_hand_read_floor(
            sum(1 for site in sites if site.subject == subject),
            floor,
            subject=f"{subject} call sites in src/",
            hint=", ".join(
                sorted(f"{path}::{function}" for path, function in _SANCTIONED[subject])
            ),
            sole_site_reason=_SOLE_SITE_REASONS.get(subject),
        )
    escapes = bypasses()
    assert not escapes, (
        "A provider client built outside the function a resolved route "
        "reaches has no consumer at all, so no tier, no credential unit and "
        "no llm.routes entry applies to it. Go through "
        "StoreRegistry.build_llm_client(consumer=...).\n  "
        + "\n  ".join(escape.text for escape in escapes)
    )


# ---------------------------------------------------------------------------
# Vacuity guards
# ---------------------------------------------------------------------------

#: Both residue shapes are a *mention* of the subject that is not a call to
#: it, which is exactly what invariant C sweeps all of src/ for.
_RESIDUE_EXEMPTIONS = {
    "partial_binding": (
        "residue: the binding's value is a call, so there is no name for "
        "construction_names to resolve. Closed by stray_references, which "
        "fails on any mention of the subject in src/ that is not a callee, "
        "and proved closed by the parametrized residue test below"
    ),
    "cross_module_subclass": (
        "residue: a subclass defined in another module is not in this tree. "
        "Closed by stray_references, which scans all of src/ for a mention "
        "that is not a callee, so the class statement is caught where it is "
        "written rather than where it is constructed"
    ),
}


def test_the_scan_sees_every_shape_in_the_shared_evasion_roster(tmp_path: Path) -> None:
    assert_scan_is_not_vacuous(
        lambda root: [violation.line for violation in consumer_violations(root=root)],
        subject=_SUBJECT,
        kwarg=_KWARG,
        tmp_path=tmp_path,
        live_population=_scan().builds,
        floor=_BUILD_FLOOR,
        exempt=_RESIDUE_EXEMPTIONS,
    )


def test_the_bypass_scan_sees_every_shape_in_the_shared_evasion_roster(
    tmp_path: Path,
) -> None:
    subject = "OpenAIClient"
    assert_scan_is_not_vacuous(
        lambda root: [violation.line for violation in bypasses(root=root)],
        subject=subject,
        kwarg="api_key",
        tmp_path=tmp_path,
        live_population=sum(1 for site in _provider_sites() if site.subject == subject),
        floor=_PROVIDER_FLOORS[subject],
        exempt=_RESIDUE_EXEMPTIONS,
    )


@pytest.mark.parametrize("subject", [_SUBJECT, "OpenAIClient"])
def test_stray_references_close_the_two_residue_shapes(
    tmp_path: Path, subject: str
) -> None:
    """The exemptions above are a hand-off, so prove the other half catches them."""
    corpus = render_evasion_corpus(subject=subject, kwarg=_KWARG)
    root = tmp_path / "corpus"
    corpus.write(root)
    residue: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        for number, text in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if f"class _Exported({subject})" in text:
                residue["cross_module_subclass"] = number
            if f"functools.partial({subject})" in text:
                residue["partial_binding"] = number
    assert set(residue) == set(_RESIDUE_EXEMPTIONS), (
        "the rendered corpus no longer writes the two residue shapes this "
        f"test reads by source text; found {sorted(residue)}"
    )
    reported = {int(line.split(":")[1]) for line in stray_references(root=root)}
    assert set(residue.values()) <= reported, (
        "stray_references is what the roster exemptions hand off to, and it "
        f"missed {sorted(set(residue.values()) - reported)}; the exemptions "
        "are now holes rather than a division of labour"
    )
    assert not reported & set(corpus.lines.values()), (
        "stray_references reported a line the roster scan already covers "
        f"({sorted(reported & set(corpus.lines.values()))}), so the two "
        "guards overlap instead of closing each other's residue"
    )


#: What the shared roster does not reach: how the argument is *resolved* once
#: the call is found. Line numbers are load-bearing — the expected constants
#: below are keyed by them — so edit this and the constants together.
_EVASIONS = """
import functools

from trellis.llm.routing import LLMConsumer
from trellis.llm.routing import LLMConsumer as Consumer

registry = object()
flag = True
members = []
kwargs = {}
args = ()


def forward(registry, consumer):
    return registry.build_llm_client(consumer=consumer)


def forward_again(consumer):
    return forward(registry, consumer)


class Builder:
    def build_for(self, consumer):
        return registry.build_llm_client(consumer=consumer)

    @staticmethod
    def static(registry, consumer):
        return registry.build_llm_client(consumer=consumer)


def kwonly(registry, *, consumer):
    return registry.build_llm_client(consumer=consumer)


# Sites that name a member.  Every one of these must be silent.
ok_direct = registry.build_llm_client(consumer=LLMConsumer.SESSION_CAPTURE)
ok_positional = forward(registry, LLMConsumer.RECONCILE)
ok_keyword = forward(registry, consumer=LLMConsumer.ENRICHMENT)
ok_aliased = forward(registry, Consumer.PRECEDENT_MINING)
ok_chained = forward_again(LLMConsumer.MEMORY_EXTRACTION)
ok_method = Builder().build_for(LLMConsumer.RECONCILE)
ok_static = Builder.static(registry, LLMConsumer.ENRICHMENT)
ok_kwonly = kwonly(registry, consumer=LLMConsumer.CLASSIFY_SHADOW)


# Direct builds that do not name a member.
missing = registry.build_llm_client()
explicit_none = registry.build_llm_client(consumer=None)
a_string = registry.build_llm_client(consumer="reconcile")
splatted = registry.build_llm_client(**kwargs)
misspelled = registry.build_llm_client(consumer=LLMConsumer.RECONSILE)
a_value = registry.build_llm_client(consumer=LLMConsumer.RECONCILE.value)
conditional = registry.build_llm_client(
    consumer=LLMConsumer.RECONCILE if flag else None
)


def defaulted(registry, consumer=None):
    return registry.build_llm_client(consumer=consumer)


def local_binding(registry):
    consumer = LLMConsumer.RECONCILE
    return registry.build_llm_client(consumer=consumer)


def rebound(registry, consumer):
    consumer = LLMConsumer.RECONCILE
    return registry.build_llm_client(consumer=consumer)


def variadic(registry, *consumer):
    return registry.build_llm_client(consumer=consumer)


def closing_over(registry, consumer):
    def inner():
        return registry.build_llm_client(consumer=consumer)

    return inner


def decorating(registry):
    @registry.build_llm_client(consumer=consumer)
    def inner(consumer):
        return consumer

    return inner


def defaulted_build(consumer, client=registry.build_llm_client(consumer=consumer)):
    return client


lambdas = map(lambda consumer: registry.build_llm_client(consumer=consumer), members)


# Forwarder calls that do not name a member.
forwarded_none = forward(registry, None)
forwarded_missing = forward(registry)
forwarded_splat = forward(registry, *args)
chained_none = forward_again(None)
method_none = Builder().build_for(None)
kwonly_none = kwonly(registry, consumer=None)


# References to the builder that are not calls to it.
dynamic = getattr(registry, "build_llm_client")
handler = registry.build_llm_client
handled = handler()
bound = functools.partial(forward, registry)
""".lstrip("\n")

#: ``{line: (shape, a fragment of the reason that line must be reported for)}``
#: — the shape label is what makes a mismatch legible, and the fragment is
#: what stops a scan from reporting the right lines for the wrong reasons.
_EXPECTED_VIOLATIONS: dict[int, tuple[str, str]] = {
    46: ("no keyword at all", "is not passed"),
    47: ("an explicit None", "None builds from the parent"),
    48: ("the value as a string", "a string is checked only when it runs"),
    49: ("a ** splat", "** splat"),
    50: ("a misspelled member", "no member RECONSILE"),
    51: ("the member's .value", "is not a literal"),
    52: ("a conditional", "is not a literal"),
    58: ("a forwarder whose parameter defaults", "a caller that omits it"),
    63: ("a local, not a parameter", "a local or a closure"),
    68: ("a parameter rebound in the body", "rebound parameter is a local"),
    72: ("a *args parameter", "collects"),
    77: ("a closure over the parameter", "a local or a closure"),
    83: ("a decorator, whose scope is outward", "a local or a closure"),
    90: ("a default, whose scope is the module", "not a parameter of an enclosing"),
    94: ("a lambda parameter", "a lambda parameter"),
    98: ("None through a forwarder", "None builds from the parent"),
    99: ("the forwarded argument omitted", "is not passed"),
    100: ("a positional splat into a forwarder", "positional * splat"),
    101: ("None two forwarders deep", "None builds from the parent"),
    102: ("None through a method", "None builds from the parent"),
    103: ("None through a keyword-only forwarder", "None builds from the parent"),
    109: ("a call through a rebound name", "is not passed"),
}

#: Every member, reached eight ways. A rule that cannot see a *good* site is
#: as broken as one that cannot see a bad one — it would fail the build.
_EXPECTED_NAMED = [
    (35, "SESSION_CAPTURE"),
    (36, "RECONCILE"),
    (37, "ENRICHMENT"),
    (38, "PRECEDENT_MINING"),
    (39, "MEMORY_EXTRACTION"),
    (40, "RECONCILE"),
    (41, "ENRICHMENT"),
    (42, "CLASSIFY_SHADOW"),
]
_EXPECTED_FORWARDERS = ["build_for", "forward", "forward_again", "kwonly", "static"]
_EXPECTED_STRAY = {107, 108, 110}
_EXPECTED_BUILDS = 21


def test_the_hand_written_corpus_is_read_exactly(tmp_path: Path) -> None:
    root = tmp_path / "hand"
    root.mkdir()
    (root / "evasions.py").write_text(_EVASIONS, encoding="utf-8")
    scan = _scan(root=root)

    assert scan.builds == _EXPECTED_BUILDS, (
        f"the corpus holds {_EXPECTED_BUILDS} direct builds and the scan "
        f"counted {scan.builds}"
    )
    assert sorted(forwarder.name for forwarder in scan.forwarders) == (
        _EXPECTED_FORWARDERS
    ), (
        "a forwarder the scan cannot discover takes every call into it out "
        f"of the rule: {sorted(f.name for f in scan.forwarders)}"
    )
    assert [(line, member) for _, line, member in scan.named] == _EXPECTED_NAMED, (
        "a site that names a member must resolve to that member: "
        f"{[(line, member) for _, line, member in scan.named]}"
    )

    reported = {violation.line: violation.reason for violation in scan.violations}
    assert set(reported) == set(_EXPECTED_VIOLATIONS), (
        f"missing={sorted(set(_EXPECTED_VIOLATIONS) - set(reported))} "
        f"spurious={sorted(set(reported) - set(_EXPECTED_VIOLATIONS))}"
    )
    for line, (shape, fragment) in _EXPECTED_VIOLATIONS.items():
        assert fragment in reported[line], (
            f"line {line} ({shape}) is reported, but for the wrong reason: "
            f"{reported[line]!r} does not mention {fragment!r}"
        )

    strays = {int(line.split(":")[1]) for line in stray_references(root=root)}
    assert strays == _EXPECTED_STRAY, (
        f"missing={sorted(_EXPECTED_STRAY - strays)} "
        f"spurious={sorted(strays - _EXPECTED_STRAY)}"
    )


# ---------------------------------------------------------------------------
# The premise
# ---------------------------------------------------------------------------

_PARENT = {
    "provider": "openai",
    "base_url": "http://localhost:11434/v1",
    "api_key_env": "TRELLIS_TEST_PARENT_KEY",
    "model": "hermes3:8b",
}
_DEEP = {
    "base_url": "http://localhost:4000/v1",
    "api_key_env": "TRELLIS_TEST_TIER_KEY",
    "model": "deep",
}
_PARENT_KWARGS = {
    "api_key": "sk-parent-0001",
    "base_url": "http://localhost:11434/v1",
    "default_model": "hermes3:8b",
}
_DEEP_KWARGS = {
    "api_key": "sk-tier-0002",
    "base_url": "http://localhost:4000/v1",
    "default_model": "deep",
}


@pytest.fixture
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_TEST_PARENT_KEY", "sk-parent-0001")
    monkeypatch.setenv("TRELLIS_TEST_TIER_KEY", "sk-tier-0002")


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> type:
    class Recorder:
        calls: ClassVar[list[dict[str, Any]]] = []

        def __init__(self, **kwargs: Any) -> None:
            type(self).calls.append(kwargs)

    monkeypatch.setattr("trellis.llm.providers.openai.OpenAIClient", Recorder)
    return Recorder


class TestTheRulePremiseStillHolds:
    """The rule enforces a property of the code; pin the property too.

    Every reason this file prints says an unnamed consumer falls back to the
    *parent* ``llm:`` block. If that stopped being true — a member default, a
    positional parameter, a second definition of the builder — the rule would
    go on passing while enforcing something the code no longer does.
    """

    def test_consumer_is_keyword_only_and_defaults_to_none(self) -> None:
        signature = inspect.signature(StoreRegistry.build_llm_client)
        parameter = signature.parameters[_KWARG]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
            "the scan reads consumer= off every call by name. A positional "
            "consumer is a spelling it cannot see, so keyword-only is what "
            "makes the rule complete rather than approximate."
        )
        assert parameter.default is None, (
            "a member default would make an omitted consumer silently share "
            "that member's tier, which is worse than the parent block and "
            "would make every reason this rule prints a lie."
        )

    def test_src_defines_the_builder_once(self) -> None:
        base = _src_root()
        definitions = [
            f"{module.path.relative_to(base).as_posix()}:{node.lineno}"
            for module in _modules(base, {_SUBJECT})
            for node in ast.walk(module.tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == _SUBJECT
        ]
        assert len(definitions) == 1, (
            "the scan matches calls by bare name, so it treats every "
            f"{_SUBJECT}(...) as the registry's. A second definition would "
            f"make it check the wrong signature: {definitions}"
        )
        assert definitions[0].startswith("trellis/stores/registry.py:"), definitions

    def test_the_provider_classes_are_the_two_this_rule_sanctions(self) -> None:
        providers = _src_root() / "trellis" / "llm" / "providers"
        assert providers.is_dir(), providers
        found = {
            node.name
            for path in sorted(providers.rglob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ClassDef) and node.name.endswith("Client")
        }
        assert found <= set(_SANCTIONED), (
            "invariant D names the provider clients one at a time, so a new "
            f"one is unguarded until it is added to _SANCTIONED: {found}"
        )

    @pytest.mark.usefixtures("_keys")
    def test_an_omitted_consumer_builds_from_the_parent_block(
        self, recorder: type, tmp_path: Path
    ) -> None:
        """The defect itself: no error, no log, just the wrong block."""
        config_dir = tmp_path / ".trellis"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "stores": {},
                    "llm": {
                        **_PARENT,
                        "tiers": {"deep": _DEEP},
                        "routes": {"reconcile": "deep"},
                    },
                }
            )
        )
        registry = StoreRegistry.from_config_dir(
            config_dir=config_dir, data_dir=tmp_path / "data"
        )

        registry.build_llm_client()
        registry.build_llm_client(consumer=LLMConsumer.RECONCILE)

        assert recorder.calls == [_PARENT_KWARGS, _DEEP_KWARGS], (
            "an omitted consumer must build from the parent block and a "
            "named one from its tier. If these ever agree, forgetting "
            "consumer= stops being observable and this rule is the only "
            f"thing standing between a route and silence: {recorder.calls}"
        )
