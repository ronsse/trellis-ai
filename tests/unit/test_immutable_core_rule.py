"""Enforcement for the two immutable-core rosters (K5).

:mod:`trellis.mutate.immutable_core` names two populations that a
self-tuning system may not touch: the parameter keys that gate *another*
learning loop, and the operations the writers that already run
unattended may issue. Both are constants, and a constant that names a
population is a roster.

**This repo does not get to ship a roster on trust.** #443 declared three
control keys against six actual ``pop`` sites. Three successive lists of
``updated_at`` readers were each wrong, and #417's prose had to retire a
false premise the third one had recorded. #464's two counters drifted
apart. So both rosters are checked here against a scan derived from
``src/``, and each scan is proved against a synthetic tree carrying the
evasions it must catch and the decoys it must not.

Two things the scans deliberately do *not* derive, stated here rather
than left to be discovered:

* **Which operations a given unattended writer can reach.** That needs
  interprocedural reachability from a ``requested_by`` to every
  ``Command`` it can end up on, which an AST scan cannot do honestly.
  The per-writer operation sets in
  :data:`~trellis.mutate.immutable_core.UNATTENDED_WRITERS` are hand-read
  and held by the behavioural tests in
  ``tests/unit/mutate/test_immutable_core.py``. The failure direction is
  the safe one: a writer that starts issuing an unrostered operation is
  *refused*, visibly, with ``reason="immutable_core"`` in the audit log
  and on the ``trellis analyze health`` capture banner — not silently
  permitted.
* **Whether a ``learning.``-prefixed constant is really a component id.**
  The scan collects every module-level string constant under
  ``src/trellis/learning/`` that starts with ``learning.``, which would
  also collect a metric or event name shaped that way — ``trellis_cli``
  has one today (``learning.parameter_registry.seeded_defaults``), which
  is why the scan is scoped by path. Over-collection here costs a
  spurious roster entry and nothing else: denying promotion for an id no
  proposal will ever name is free. Under-collection costs a learning loop
  whose stop is tunable by another learning loop, which is the whole
  defect.

Both scans and both seams were mutation-tested rather than assumed:
eighteen mutants — the ``AnnAssign`` branch dropped, module-level
scoping widened to a full ``walk``, constant resolution of
``requested_by`` dropped, unreadable sites swallowed, the scan pointed at
``source=``, ``startswith`` loosened to a substring test, each predicate
neutered, each roster entry corrupted, each seam's check and each seam's
emission removed, and the executor's rejection ``reason`` mislabelled —
and all eighteen die, each against a named test here or in
``tests/unit/mutate/test_immutable_core.py``. The substring mutant is
worth noting: it survived the first sweep, because no decoy carried
``learning.`` anywhere but at the start. The embedded-prefix decoy exists
to kill it, which is the difference between a tree that looks
adversarial and one that discriminates the predicate it is testing.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from tests.ast_rules import assert_hand_read_floor, iter_modules
from trellis.mutate.immutable_core import (
    DESTRUCTIVE_OPERATIONS,
    GOVERNING_KEYS,
    NON_DESTRUCTIVE_OPERATIONS,
    UNATTENDED_WRITERS,
)

# ---------------------------------------------------------------------------
# Hand-read floors
# ---------------------------------------------------------------------------
#
# Counted off the tree on 2026-09-20, not computed. A scan that quietly
# stops matching satisfies every guard that divides by its own output;
# only a number a person read can fail it. Both are floors and not
# equalities, because adding a compliant site is ordinary.

_GOVERNING_FLOOR = 4
_WRITER_FLOOR = 2
_AUTO_FLOOR = 2

_LEARNING_ROOT = Path("src/trellis/learning")
_WORKERS_ROOT = Path("src/trellis_workers")
_SRC_ROOT = Path("src")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The scans
# ---------------------------------------------------------------------------


def _module_level_strings(tree: ast.Module) -> list[tuple[str, str, int]]:
    """``(name, value, lineno)`` for each module-level ``NAME = "literal"``.

    Both binding shapes, because ``X = "s"`` and ``X: str = "s"`` are the
    same declaration and a scan that reads only :class:`ast.Assign` is
    blind to half of them — the ``annassign`` evasion the shared roster in
    ``tests/ast_rules.py`` carries for exactly this reason.

    Module level only. A component id bound inside a function is not a
    module's declared identity, and collecting one would make the scan
    depend on control flow it cannot evaluate.
    """
    found: list[tuple[str, str, int]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names = [stmt.target.id]
            value = stmt.value
        else:
            continue
        if value is None or not isinstance(value, ast.Constant):
            continue
        if not isinstance(value.value, str):
            continue
        found.extend((name, value.value, stmt.lineno) for name in names)
    return found


def governing_component_ids(root: Path) -> dict[str, str]:
    """``{component id: "path:line"}`` for every learning loop under *root*.

    The predicate is *declaration-shaped*, not name-shaped: any
    module-level string constant beginning with ``learning.``. Keying on
    the constant's **name** instead would have missed
    ``scoring.LEARNING_SCORING_COMPONENT``, which the other three spell
    ``PARAM_COMPONENT_ID`` — a three-against-four roster before the rule
    had even shipped.
    """
    found: dict[str, str] = {}
    for path, tree in iter_modules(root):
        for _name, value, lineno in _module_level_strings(tree):
            if value.startswith("learning."):
                found.setdefault(value, f"{path}:{lineno}")
    return found


def _resolve_simple(node: ast.expr | None, constants: Mapping[str, str]) -> str | None:
    """A literal string, or a ``Name`` already bound in *constants*."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    return None


def _module_file(repo_root: Path, dotted: str) -> Path | None:
    """The ``src/`` file *dotted* (``"trellis.mutate.handlers"``) names."""
    candidate = (repo_root / "src" / Path(*dotted.split("."))).with_suffix(".py")
    return candidate if candidate.is_file() else None


def _one_hop_imported_constants(tree: ast.Module, repo_root: Path) -> dict[str, str]:
    """``{local name: value}`` for every ``from <module> import <name>``
    whose target has a matching module-level string constant.

    Module-level or nested in a function body — a lazily imported
    constant, bound inside the function rather than at the top of the
    file to dodge a real import cycle, is exactly the shape
    ``precedent_auto_demote.run_precedent_auto_demotion`` uses for
    ``AUTO_DEMOTE_REQUESTED_BY``, and :func:`ast.walk` does not
    distinguish the two. One hop only: a name that is itself imported
    from a third module stays unresolved, the same trade every other
    resolver here makes rather than guess.
    """
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None or node.level:
            continue
        target_file = _module_file(repo_root, node.module)
        if target_file is None:
            continue
        try:
            target_tree = ast.parse(target_file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        target_constants = {
            name: value for name, value, _ in _module_level_strings(target_tree)
        }
        for alias in node.names:
            bound = alias.asname or alias.name
            if alias.name in target_constants:
                found.setdefault(bound, target_constants[alias.name])
    return found


def _own_descendants(node: ast.AST) -> Iterator[ast.AST]:
    """*node*'s descendants, not crossing into a nested function or lambda.

    A plain :func:`ast.walk` does not stop at a nested scope, so a
    reassignment inside a closure defined in this function would be
    misread as this function's own local — not a shape either real
    ``auto:``-identity site uses, but worth not getting wrong by
    accident rather than by design.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _own_descendants(child)


def _function_local_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef, constants: Mapping[str, str]
) -> dict[str, str]:
    """``{local name: value}`` for one function's own parameter defaults
    and body-level reassignments, resolved against *constants*.

    The second local indirection the real ``auto:``-identity sites route
    a ``requested_by=`` keyword through, beyond the module-level one
    *constants* already carries: a keyword-only parameter defaulted to a
    module constant
    (``run_candidate_auto_promotion``'s
    ``requested_by: str = AUTO_PROMOTE_REQUESTED_BY``), and a body-level
    ``if x is None: x = CONST`` fallback
    (``run_precedent_auto_demotion``'s, after
    :func:`_one_hop_imported_constants` resolves the lazily-imported
    ``CONST`` itself). Not full data flow: a name assigned more than
    once to two different resolvable values keeps only the last one this
    walk visits, and a name resolved through a second hop of indirection
    is left out rather than guessed — the caller reports it unreadable.
    """
    local: dict[str, str] = {}
    args = func.args
    positional = list(args.posonlyargs) + list(args.args)
    for arg, default in zip(
        reversed(positional), reversed(args.defaults), strict=False
    ):
        resolved = _resolve_simple(default, constants)
        if resolved is not None:
            local[arg.arg] = resolved
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        resolved = _resolve_simple(default, constants)
        if resolved is not None:
            local[arg.arg] = resolved
    for node in _own_descendants(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        resolved = _resolve_simple(node.value, {**constants, **local})
        if resolved is not None:
            local[target.id] = resolved
    return local


def _parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """``{child: parent}`` over every node in *tree*."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_function(
    node: ast.AST, parents: Mapping[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The nearest ``def``/``async def`` containing *node*, if any."""
    current: ast.AST | None = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _requested_by_sites(
    path: Path, tree: ast.Module, repo_root: Path
) -> tuple[dict[str, str], list[str]]:
    """``({identity: "path:line"}, [unreadable sites])`` for one module.

    Module-level constants are resolved, because **both** real worker
    identities are bound to one (``REQUESTED_BY`` / ``_REQUESTED_BY``) and
    a literal-only sweep of ``requested_by=`` finds neither. That is not
    hypothetical: it is the first version of this measurement, which
    enumerated all 22 ``cli:`` / ``api:`` / ``mcp:`` identities and missed
    the only two the rule is about.

    Two more hops, added for the ``auto:``-prefixed identities neither
    real worker needed: one hop of ``from <module> import <NAME>``
    (:func:`_one_hop_imported_constants`, including a lazily-imported
    name), and a function's own parameter defaults and body-level
    reassignments (:func:`_function_local_names`), consulted only when
    a bare module-level lookup misses. Still not interprocedural — a
    name threaded through a second function call is unreadable here,
    same as before *this* call's own locals are exhausted.

    A site whose value is neither a literal nor a resolvable local
    constant is returned as *unreadable* rather than dropped. The scan
    cannot certify an identity it cannot read, and silently skipping one
    is under-collection wearing a clean result.
    """
    constants = {name: value for name, value, _ in _module_level_strings(tree)}
    constants.update(_one_hop_imported_constants(tree, repo_root))
    parents = _parent_map(tree)
    found: dict[str, str] = {}
    unreadable: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "requested_by":
                continue
            value = keyword.value
            where = f"{path}:{node.lineno}"
            resolved = _resolve_simple(value, constants)
            if resolved is None and isinstance(value, ast.Name):
                func = _enclosing_function(node, parents)
                if func is not None:
                    resolved = _function_local_names(func, constants).get(value.id)
            if resolved is not None:
                found.setdefault(resolved, where)
            else:
                unreadable.append(f"{where}: requested_by={ast.unparse(value)}")
    return found, unreadable


def unattended_writer_identities(
    root: Path, repo_root: Path | None = None
) -> tuple[dict[str, str], list[str]]:
    """Every ``requested_by=`` identity submitted from *root*.

    *repo_root* anchors the one-hop import resolution in
    :func:`_requested_by_sites` and defaults to *root* itself, which is
    correct for every real call in this module (``root`` already *is*
    the repo root there) and only needs overriding by a synthetic-tree
    test whose ``root`` is a narrower directory than the fixture's own
    repo root.
    """
    if repo_root is None:
        repo_root = root
    found: dict[str, str] = {}
    unreadable: list[str] = []
    for path, tree in iter_modules(root):
        module_found, module_unreadable = _requested_by_sites(path, tree, repo_root)
        for identity, where in module_found.items():
            found.setdefault(identity, where)
        unreadable.extend(module_unreadable)
    return found, unreadable


def worker_prefixed_identities(
    root: Path, repo_root: Path | None = None
) -> dict[str, str]:
    """Every ``requested_by=`` identity under *root* spelled ``worker:*``.

    The companion sweep to :func:`unattended_writer_identities`, over all
    of ``src/`` rather than one package, so a worker identity declared
    outside ``trellis_workers`` cannot submit unrostered. It reads
    ``requested_by=`` and not the bare ``"worker:"`` literal, which is the
    distinction that matters: ``src/`` carries two ``worker:`` literals
    that are :class:`~trellis.stores.base.event_log.Event` *sources* on
    ``MEMORY_OP_JUDGED`` telemetry (``worker:enrich``,
    ``worker:session-capture.distill``) and never reach a
    :class:`~trellis.mutate.commands.Command`. A literal grep over-collects
    both and would demand roster entries for identities that submit
    nothing.
    """
    found, _ = unattended_writer_identities(root, repo_root)
    return {k: v for k, v in found.items() if k.startswith("worker:")}


def auto_prefixed_identities(
    root: Path, repo_root: Path | None = None
) -> dict[str, str]:
    """Every ``requested_by=`` identity under *root* spelled ``auto:*``.

    ``auto:`` identities have no single home package the way
    ``worker:`` has ``trellis_workers`` — ``auto:learning_candidate``'s
    own command construction sits in
    ``trellis.learning.scoring.submit_learning_promotion``, a module
    away from the policy that names the identity — so there is no
    narrower-root completeness check to pair this with the way
    :func:`test_every_unattended_writer_identity_is_in_the_allow_list`
    pairs with :func:`test_no_worker_identity_anywhere_in_src_escapes_the_allow_list`.
    This one sweep, over all of ``src/``, is checked in both directions.
    """
    found, _ = unattended_writer_identities(root, repo_root)
    return {k: v for k, v in found.items() if k.startswith("auto:")}


# ---------------------------------------------------------------------------
# Rule 1 — every learning loop's component id is immutable
# ---------------------------------------------------------------------------


def test_every_learning_component_id_is_in_the_governing_roster() -> None:
    """A new learning loop cannot be silently tunable by another one.

    Equality, not containment, and in both directions on purpose. A
    missing entry is a loop whose stop another loop can move. A *stale*
    entry is a key denied on behalf of a module that no longer exists,
    which is how a roster comes to be trusted while describing a tree
    that has moved on.
    """
    discovered = governing_component_ids(_repo_root() / _LEARNING_ROOT)

    missing = set(discovered) - GOVERNING_KEYS
    assert not missing, (
        "these learning components declare a parameter component id that "
        "GOVERNING_KEYS does not deny, so a tuner proposal naming one "
        "would retune another learning loop's own gate: "
        + ", ".join(f"{k} ({discovered[k]})" for k in sorted(missing))
    )

    stale = GOVERNING_KEYS - set(discovered)
    assert not stale, (
        "GOVERNING_KEYS names component ids that no module under "
        f"{_LEARNING_ROOT} declares any more — re-read the tree rather "
        "than trusting the constant: " + ", ".join(sorted(stale))
    )


def test_the_governing_key_roster_matches_the_live_constants() -> None:
    """The literals in ``immutable_core`` include the ones the loops use.

    :data:`GOVERNING_KEYS` spells its four whole-loop ids as literals
    rather than importing them, because ``trellis.mutate`` sits *below*
    ``trellis.learning`` in the import order and this module is not worth
    inverting that for. This is the other half of that trade: the
    duplication is checked by importing the real constants here, where a
    test may depend on anything.

    A subset check, not equality: :data:`GOVERNING_KEYS` also carries
    settings-registry leaf keys that the AST scan collects by
    shape but that have no ``PARAM_COMPONENT_ID``-style live constant to
    import and compare against. Equality would make every such addition
    fail this test alongside the one it is meant to police; the roster's
    completeness in the *other* direction — nothing stale, nothing
    missing versus the scan — is
    :func:`test_every_learning_component_id_is_in_the_governing_roster`'s
    job, not this one's. This test's only claim is that these four never
    drift from the modules that own them.
    """
    from trellis.learning.domain_normalization import (
        PARAM_COMPONENT_ID as DOMAIN_NORMALIZATION,
    )
    from trellis.learning.schema_evolution import (
        PARAM_COMPONENT_ID as SCHEMA_EVOLUTION,
    )
    from trellis.learning.scoring import LEARNING_SCORING_COMPONENT
    from trellis.learning.tag_evolution import (
        PARAM_COMPONENT_ID as TAG_EVOLUTION,
    )

    assert {
        DOMAIN_NORMALIZATION,
        SCHEMA_EVOLUTION,
        LEARNING_SCORING_COMPONENT,
        TAG_EVOLUTION,
    } <= GOVERNING_KEYS


# ---------------------------------------------------------------------------
# Rule 2 — every unattended writer is bound by the allow-list
# ---------------------------------------------------------------------------


def test_every_unattended_writer_identity_is_in_the_allow_list() -> None:
    """A worker cannot submit under an identity the allow-list never saw.

    An unrostered identity is not refused anything —
    :func:`~trellis.mutate.immutable_core.unattended_writer_refusal`
    returns ``None`` for a ``requested_by`` it does not recognise, which
    is correct at runtime (an unknown identity is not evidence of
    unattendedness) and is exactly why the roster has to be complete here
    instead.
    """
    root = _repo_root()
    discovered, unreadable = unattended_writer_identities(
        root / _WORKERS_ROOT, repo_root=root
    )

    assert not unreadable, (
        "the scan cannot read the identity these sites submit under, so it "
        "cannot certify the allow-list is complete. Bind the value to a "
        "module-level constant, or widen the resolver deliberately: "
        + "; ".join(unreadable)
    )

    missing = set(discovered) - set(UNATTENDED_WRITERS)
    assert not missing, (
        "these writers run unattended and submit commands under an identity "
        "UNATTENDED_WRITERS does not bind, so every operation is permitted "
        "to them: " + ", ".join(f"{k} ({discovered[k]})" for k in sorted(missing))
    )

    # Scoped to worker:-prefixed entries: UNATTENDED_WRITERS also carries
    # auto:-prefixed entries now, which this narrow, trellis_workers-only
    # scan never discovers — that is
    # test_every_auto_writer_identity_is_in_the_allow_list's population,
    # not a staleness this test can see.
    worker_entries = {k for k in UNATTENDED_WRITERS if k.startswith("worker:")}
    stale = worker_entries - set(discovered)
    assert not stale, (
        "UNATTENDED_WRITERS binds identities that nothing under "
        f"{_WORKERS_ROOT} submits under any more: " + ", ".join(sorted(stale))
    )


def test_no_worker_identity_anywhere_in_src_escapes_the_allow_list() -> None:
    """The package boundary is not the rule; the ``worker:`` prefix is.

    ``trellis_workers`` is where unattended writers live *today*. A
    ``worker:``-prefixed identity submitted from anywhere else in ``src/``
    is the same thing in a different directory, and the allow-list has to
    bind it too.
    """
    root = _repo_root()
    discovered = worker_prefixed_identities(root / _SRC_ROOT, repo_root=root)
    missing = set(discovered) - set(UNATTENDED_WRITERS)
    assert not missing, (
        "these worker identities submit commands from outside "
        f"{_WORKERS_ROOT} and are unbound: "
        + ", ".join(f"{k} ({discovered[k]})" for k in sorted(missing))
    )


def test_every_auto_writer_identity_is_in_the_allow_list() -> None:
    """The ``auto:`` learning-loop writers, checked both ways at once.

    Unlike ``worker:`` there is no narrower home package to pair this
    with — ``auto:learning_candidate``'s command construction sits a
    module away from the policy that names it
    (:func:`trellis.learning.scoring.submit_learning_promotion`,
    called from
    :func:`trellis.learning.candidate_auto_promote.run_candidate_auto_promotion`)
    — so completeness in *both* directions is checked in one sweep over
    all of ``src/`` instead of the missing-half / stale-half split
    :func:`test_every_unattended_writer_identity_is_in_the_allow_list`
    and :func:`test_no_worker_identity_anywhere_in_src_escapes_the_allow_list`
    use for workers.
    """
    root = _repo_root()
    discovered = auto_prefixed_identities(root / _SRC_ROOT, repo_root=root)

    # Unlike the worker scans, this one does not also assert on
    # ``unreadable``: widened to all of ``src/`` that list can carry
    # sites with nothing to do with an auto-loop identity (a CLI or API
    # surface passing a runtime-determined principal, say), and the scan
    # cannot tell which of those would have read ``auto:`` without
    # reading them — the same reason
    # :func:`test_no_worker_identity_anywhere_in_src_escapes_the_allow_list`
    # does not check it either.
    auto_entries = {k for k in UNATTENDED_WRITERS if k.startswith("auto:")}

    missing = set(discovered) - auto_entries
    assert not missing, (
        "these auto-loop writers submit commands under an identity "
        "UNATTENDED_WRITERS does not bind, so every operation is permitted "
        "to them: " + ", ".join(f"{k} ({discovered[k]})" for k in sorted(missing))
    )

    stale = auto_entries - set(discovered)
    assert not stale, (
        "UNATTENDED_WRITERS binds auto: identities that nothing under "
        f"{_SRC_ROOT} submits under any more: " + ", ".join(sorted(stale))
    )


def test_no_unattended_writer_may_issue_a_destructive_operation() -> None:
    """The claim the corpus makes, checked rather than asserted in prose.

    :data:`DESTRUCTIVE_OPERATIONS` exists for this test and for nothing
    else — it gates no traffic, so its incompleteness cannot fail open the
    way a deny-list in the enforcement path would.
    """
    for identity, permitted in UNATTENDED_WRITERS.items():
        overlap = permitted & DESTRUCTIVE_OPERATIONS
        assert not overlap, (
            f"{identity} runs without a human in the call and is permitted "
            f"{sorted(op.value for op in overlap)}"
        )


def test_every_operation_is_classified_by_hand() -> None:
    """A new ``Operation`` member must be classified, not defaulted.

    The two sets partition :class:`~trellis.mutate.commands.Operation`
    rather than one being the other's complement, so adding a verb turns
    this red instead of silently landing it on the safe side of a
    subtraction.
    """
    from trellis.mutate.commands import Operation

    everything = set(Operation)
    assert everything == DESTRUCTIVE_OPERATIONS | NON_DESTRUCTIVE_OPERATIONS, (
        "unclassified operations: "
        + ", ".join(
            sorted(
                op.value
                for op in everything
                - DESTRUCTIVE_OPERATIONS
                - NON_DESTRUCTIVE_OPERATIONS
            )
        )
    )
    assert not DESTRUCTIVE_OPERATIONS & NON_DESTRUCTIVE_OPERATIONS


# ---------------------------------------------------------------------------
# Vacuity: the scans find the sites they are meant to police
# ---------------------------------------------------------------------------


def test_the_scans_find_the_sites_they_are_meant_to_police() -> None:
    """Floors hand-read off the tree, so a narrowing scan cannot pass.

    Every other guard in this module divides by a scan's own output and
    is therefore satisfied by a scan that has stopped matching. These two
    numbers are the only things that are not.
    """
    root = _repo_root()
    assert_hand_read_floor(
        len(governing_component_ids(root / _LEARNING_ROOT)),
        _GOVERNING_FLOOR,
        subject="learning-component-id declaration",
        hint="counted under src/trellis/learning on 2026-09-20.",
    )
    writers, _ = unattended_writer_identities(root / _WORKERS_ROOT, repo_root=root)
    assert_hand_read_floor(
        len(writers),
        _WRITER_FLOOR,
        subject="unattended-writer identity",
        hint="counted under src/trellis_workers on 2026-09-20.",
    )
    auto_writers = auto_prefixed_identities(root / _SRC_ROOT, repo_root=root)
    assert_hand_read_floor(
        len(auto_writers),
        _AUTO_FLOOR,
        subject="auto-loop writer identity",
        hint="counted under src/trellis/learning on 2026-10-17: "
        "auto:learning_candidate (candidate_auto_promote.py) and "
        "auto:learning_candidate_demote (precedent_auto_demote.py).",
    )


# ---------------------------------------------------------------------------
# Vacuity: the scans are proved against synthetic violations
# ---------------------------------------------------------------------------
#
# Line numbers below are hand-read off the rendered source, which is why
# each tree is written with a leading newline that is stripped before the
# file is written: the comment on a line then states that line's real
# number.

_GOVERNING_TREE = '''
"""Synthetic learning module. Mentions learning.in_a_docstring."""
PARAM_COMPONENT_ID = "learning.planted_plain"            # 2  FOUND
SCOPED: str = "learning.planted_annotated"               # 3  FOUND
NOT_A_LEARNING_ID = "retrieve.pack_builder"              # 4  decoy: prefix
_TUPLE = ("learning.in_a_tuple",)                        # 5  decoy: not a str
_METRIC = "analyze.learning.registry_seeded"             # 6  decoy: embedded


def _inner() -> str:
    local = "learning.function_local"                    # 10 decoy: not module
    return local
'''

_EXPECTED_GOVERNING = {
    "learning.planted_plain": 2,
    "learning.planted_annotated": 3,
}

_WORKER_TREE = '''
"""Synthetic worker module."""
_REQUESTED_BY = "worker:planted-constant"

emit_judged(source="worker:planted-source-decoy")        # 4  decoy: not a cmd
execute(Command(requested_by=_REQUESTED_BY))             # 5  FOUND via const
execute(Command(requested_by="worker:planted-literal"))  # 6  FOUND literal
'''

_EXPECTED_WORKERS = {
    "worker:planted-constant": 5,
    "worker:planted-literal": 6,
}

_UNREADABLE_TREE = '''
"""A worker whose identity the scan cannot read."""


def submit(who: str) -> None:
    execute(Command(requested_by=who))                   # 5  UNREADABLE
'''

_AUTO_PROMOTE_TREE = '''
"""Synthetic auto-identity module: a keyword-only default."""
AUTO_PROMOTE_REQUESTED_BY = "auto:planted-promote"


def run_promote(*, requested_by: str = AUTO_PROMOTE_REQUESTED_BY) -> None:
    execute(Command(requested_by=requested_by))           # 6  FOUND via default
'''

_AUTO_DEMOTE_OTHER_MODULE = '''
"""Synthetic sibling module a lazy, function-body import reaches."""
AUTO_DEMOTE_REQUESTED_BY = "auto:planted-demote"
'''

_AUTO_DEMOTE_TREE = '''
"""Synthetic auto-identity module: a lazy cross-module import."""


def run_demote(*, requested_by: str | None = None) -> None:
    if requested_by is None:
        from auto_fake.handlers import AUTO_DEMOTE_REQUESTED_BY

        requested_by = AUTO_DEMOTE_REQUESTED_BY
    execute(Command(requested_by=requested_by))           # 9  FOUND via lazy import


def run_unresolvable(who: str) -> None:
    execute(Command(requested_by=who))                    # 13 UNREADABLE
'''


def _render(tmp_path: Path, name: str, tree: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "planted.py").write_text(tree.lstrip("\n"), encoding="utf-8")
    return root


def _render_package(tmp_path: Path, name: str, files: Mapping[str, str]) -> Path:
    """A synthetic repo root with *files* written under ``<root>/src/``.

    :func:`_render`'s multi-file counterpart, needed once: proving
    :func:`_one_hop_imported_constants` requires two real files, one
    importing the other, which a single ``planted.py`` cannot model.
    """
    root = tmp_path / name
    for rel_path, content in files.items():
        full = root / "src" / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content.lstrip("\n"), encoding="utf-8")
    return root


def test_the_governing_scan_catches_a_planted_loop(tmp_path: Path) -> None:
    """Run the *shipped* predicate over a tree that violates it.

    Two of the five declarations must be collected and three must not.
    The annotated one is the evasion that matters — a scan reading only
    :class:`ast.Assign` returns one of two and still passes every guard
    that divides by its own output.
    """
    root = _render(tmp_path, "governing", _GOVERNING_TREE)
    found = governing_component_ids(root)

    assert set(found) == set(_EXPECTED_GOVERNING), (
        f"scan returned {sorted(found)}; expected {sorted(_EXPECTED_GOVERNING)}"
    )
    for identity, line in _EXPECTED_GOVERNING.items():
        assert found[identity].endswith(f":{line}"), (
            f"{identity} reported at {found[identity]}, hand-read line {line}"
        )


def test_the_governing_rule_fails_on_the_planted_tree(tmp_path: Path) -> None:
    """The rule itself, not just its scan, goes red on a violation."""
    root = _render(tmp_path, "governing_rule", _GOVERNING_TREE)
    discovered = governing_component_ids(root)
    assert set(discovered) - GOVERNING_KEYS, (
        "the planted loops are absent from GOVERNING_KEYS, so the rule's "
        "own assertion must have something to fail on"
    )


def test_the_worker_scan_catches_a_planted_identity(tmp_path: Path) -> None:
    """Both binding shapes, and the ``source=`` decoy stays out.

    The constant-bound site is the one that bit the first measurement;
    the ``source=`` line is the shape ``src/`` really carries twice today
    (``worker:enrich``, ``worker:session-capture.distill``), so a rule
    written against a ``"worker:"`` literal grep would demand roster
    entries for two identities that submit no command at all.
    """
    root = _render(tmp_path, "workers", _WORKER_TREE)
    found, unreadable = unattended_writer_identities(root)

    assert not unreadable
    assert set(found) == set(_EXPECTED_WORKERS), (
        f"scan returned {sorted(found)}; expected {sorted(_EXPECTED_WORKERS)}"
    )
    for identity, line in _EXPECTED_WORKERS.items():
        assert found[identity].endswith(f":{line}"), (
            f"{identity} reported at {found[identity]}, hand-read line {line}"
        )
    assert "worker:planted-source-decoy" not in found


def test_the_worker_scan_reports_an_identity_it_cannot_read(tmp_path: Path) -> None:
    """An unreadable ``requested_by`` is surfaced, never dropped."""
    root = _render(tmp_path, "unreadable", _UNREADABLE_TREE)
    found, unreadable = unattended_writer_identities(root)

    assert not found
    assert len(unreadable) == 1
    assert unreadable[0].endswith(":5: requested_by=who"), unreadable


def test_the_scan_resolves_a_keyword_default_and_a_lazy_import(
    tmp_path: Path,
) -> None:
    """The two local hops the real ``auto:`` sites need, proved against a
    planted pair of files rather than assumed from the real tree.

    ``run_promote`` models
    ``candidate_auto_promote.run_candidate_auto_promotion``'s own shape
    (a keyword-only parameter defaulted to a module constant);
    ``run_demote`` models
    ``precedent_auto_demote.run_precedent_auto_demotion``'s (a constant
    lazily imported from a *different* file, inside an
    ``if x is None:`` guard, then assigned to the parameter). Both
    sites are nothing like a bare module-level constant, which is why
    neither was found before this rule's extension.
    ``run_unresolvable`` is the control: a parameter with no resolvable
    binding at all must still come back unreadable, proving the new
    hops have not gone on to guess.
    """
    root = _render_package(
        tmp_path,
        "auto_identity",
        {
            "auto_fake/handlers.py": _AUTO_DEMOTE_OTHER_MODULE,
            "auto_fake/promote_planted.py": _AUTO_PROMOTE_TREE,
            "auto_fake/demote_planted.py": _AUTO_DEMOTE_TREE,
        },
    )
    found, unreadable = unattended_writer_identities(root / "src", repo_root=root)

    assert found["auto:planted-promote"].endswith("promote_planted.py:6")
    assert found["auto:planted-demote"].endswith("demote_planted.py:9")
    assert len(unreadable) == 1
    assert unreadable[0].endswith("demote_planted.py:13: requested_by=who"), unreadable


@pytest.mark.parametrize(
    ("tree_name", "tree", "expected"),
    [
        ("governing", _GOVERNING_TREE, _EXPECTED_GOVERNING),
        ("workers", _WORKER_TREE, _EXPECTED_WORKERS),
    ],
    ids=["governing", "workers"],
)
def test_the_hand_read_line_numbers_are_real(
    tree_name: str, tree: str, expected: dict[str, int]
) -> None:
    """The ``# N FOUND`` comments state their own true line numbers.

    A tree whose comments drift is a tree whose evasions can be silently
    renumbered, after which the scan's "found it at line N" assertions
    check nothing. Read back by splitting the rendered text and matching
    a comment — which shares no code with the AST scan under test, so the
    two cannot be wrong in the same way.

    The marker, not the identity string, because one planted site
    resolves its identity through a module constant bound on a *different*
    line: that indirection is the evasion the scan exists to catch, so it
    is also the one whose text is not on the line being pinned.
    """
    lines = tree.lstrip("\n").splitlines()
    marked: set[int] = set()
    for number, line in enumerate(lines, 1):
        match = re.search(r"#\s*(\d+)\s+FOUND", line)
        if match is None:
            continue
        marked.add(number)
        assert int(match.group(1)) == number, (
            f"{tree_name}: the comment on line {number} claims to be on "
            f"line {match.group(1)}"
        )
    assert marked == set(expected.values()), (
        f"{tree_name}: lines marked FOUND are {sorted(marked)}, "
        f"but the expected mapping names {sorted(set(expected.values()))}"
    )
