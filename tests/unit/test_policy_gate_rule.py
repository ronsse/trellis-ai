"""Enforcement for the executor wiring rules (#424, #551).

``CLAUDE.md`` states the pipeline as five stages, and
:func:`trellis.mutate.build_curate_executor` passes a gate
**unconditionally** — not only when policies exist — for one stated reason:

    so "is Stage 2 running?" has an observable answer instead of depending
    on file state.

That reasoning is defeated by a second construction site.
``MutationExecutor.__init__`` takes ``policy_gate: PolicyGate | None =
None`` and ``execute`` guards on ``if self._policy_gate is not None``, so an
executor built without one runs the documented five stages as four —
silently, and *regardless of what policies.json declares*. #370 wired the
factory; #424 is that defect surviving in
``trellis_workers/trace_embed/worker.py``, the one write path that did not
route through the factory. An operator who writes ``deny evidence.*`` and
confirms it with ``trellis policy list`` has every reason to believe it is
in force. It was, on five surfaces, and was not on that one.

**The one-line fix is not the deliverable; preventing the third site is.**
Three invariants are enforced here, all derived by AST over ``src/`` rather
than from a list of known call sites. This repo has shipped two rosters that
rotted — #443 declared three control keys against six actual ``pop`` sites,
and three successive lists of ``updated_at`` readers were each wrong — so a
roster is not an option:

1. **Every ``MutationExecutor(...)`` in ``src/`` passes a real
   ``policy_gate=``.** No exemption for the factory: the factory satisfies
   the rule on its own merits, and an exemption is a roster of one that
   turns into the hole the moment the factory is renamed or copied.
2. **Every ``build_policy_gate(...)`` in ``src/`` sits in a function that
   builds a ``MutationExecutor``.** This one guards #425's telemetry rather
   than #424's wiring: a gate-load failure is recorded as a
   ``WRITE_REJECTED`` event, and calling that a *write* rejection is honest
   only while the gate is built exclusively on write paths. A future read
   surface calling ``build_policy_gate`` would make the health signal lie,
   and nothing else would notice.
3. **Every ``MutationExecutor(...)`` in ``src/`` passes a real
   ``event_log=``.** Stage 5's sibling defect (#551): ``event_log`` is
   ``EventLog | None = None`` on the same constructor and ``_emit_event``
   returns early when it is ``None``, so an executor built without one runs
   the five stages as four at the *other* end — every governed mutation
   committed with no audit event, and nothing anywhere saying so. It is the
   #424 shape with the argument's name changed, which is why it is enforced
   by the same scan rather than by a second copy of it. A knowledge-plane-only
   deployment expresses "no audit log" as the ``null`` backend, so the store
   resolves to ``NullEventLog`` and the argument is still supplied (#196) —
   that is the intended way to say it, and it satisfies this rule.

The module keeps its name. The three invariants share one construction
scan, and splitting them across files by subject would have meant two
scans to keep in step — which is the failure mode this rule exists to
prevent. Whatever makes an executor visible to one of them makes it
visible to all three.

``src/`` only. ``tests/`` builds ungated executors on purpose — that is what
``test_policy_wiring.py`` compares the gated ones against — so scanning
tests would forbid the control arm of the transparency proof.

Guarding against vacuity is the other half. A structural scan that stops
matching keeps passing forever while enforcing nothing, so four separate
things pin it: the scan must find construction sites to reason about
(a **hand-read** floor — see :func:`tests.ast_rules.assert_hand_read_floor`),
it must report a known set of violations in a synthetic tree *through the
shipped predicate* (not a copy of it), it must report every shape in
:data:`tests.ast_rules.EVASIONS` that it has not explicitly exempted, and
the executor must still have the parameter — and the ``None`` default —
that makes the rule necessary at all. Same model as
``test_machine_output_rule.py``.

The third of those is new and is where this rule stopped being self-graded.
Its own corpus below encodes *this* rule's judgement, hand-read line by
line; the shared roster encodes every shape any rule's review has found,
so a spelling discovered while gating some other PR reaches here without
anyone re-deriving it. The two are complementary and neither replaces the
other.

Adopting it also **closed** three blind spots rather than documenting
them. :func:`_gate_is_wired`'s docstring used to say the scan could not
follow an import alias or a local rebinding, and that a subclass needed a
separate test — presented as a limit of static analysis and simply untrue,
since #488's own rule already resolved all three in one file. The scan
resolves them now. The two exemptions that remain are *residue*, which
``tests.ast_rules`` demonstrates by running every scanner it has against
them rather than by asserting it.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Collection
from pathlib import Path
from unittest.mock import MagicMock

from tests.ast_rules import (
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    calls_to_any,
    construction_names,
    iter_modules,
    name_of,
)
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor
from trellis.stores.base.event_log import EventLog

#: The class whose construction the rule polices. Matched on the bare name
#: off either a ``Name`` or an ``Attribute``, so ``MutationExecutor(...)``
#: and ``mutate.MutationExecutor(...)`` both land.
_EXECUTOR = "MutationExecutor"

#: The keyword that wires Stage 2.
_GATE_KWARG = "policy_gate"

#: The keyword that wires Stage 5.
_LOG_KWARG = "event_log"

#: The one supported way to build a gate. Invariant 2 is about *where* it
#: is called, not which callee — the name is matched, the module is not.
_GATE_BUILDER = "build_policy_gate"


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src not found at {root}"
    return root


def _gate_is_wired(node: ast.Call) -> bool:
    """Does this construction pass a gate that could actually gate anything?

    A thin binding of :func:`_kwarg_is_wired` to ``policy_gate``. The
    predicate is shared with invariant 3 rather than copied, because the two
    arguments fail in exactly the same four ways and a second copy is a
    second thing to keep in step.

    Four shapes are rejected and each was reachable:

    * no ``policy_gate=`` at all — the #424 defect verbatim;
    * ``policy_gate=None`` — satisfies a keyword-presence check while
      leaving ``execute``'s ``is not None`` guard false, which is the
      obvious way a future edit "fixes" the rule without fixing anything;
    * **any** literal ``None`` inside the value —
      ``policy_gate=build_policy_gate(r) if flag else None`` is the equally
      obvious *next* edit, and it is exactly as statically visible as the
      bare constant. Matching on the top-level node alone let it through;
      the whole subtree is walked instead;
    * ``**kwargs`` — opaque to a static scan. Rejecting it is conservative
      by design: an executor whose wiring cannot be read is exactly the
      state this rule exists to prevent, and no call site in ``src/`` needs
      the splat. It only ever *decides* the
      ``policy_gate=gate, **kwargs`` shape (a splat alone already falls
      through to ``return False``), so that shape is in the corpus below —
      otherwise the clause is unpinned and deleting it changes nothing.

    What it cannot decide is whether a *bound name* is ``None`` at runtime
    — ``policy_gate=gate`` passes the scan whatever ``gate`` holds, so
    hoisting the conditional above (``gate = ... if flag else None``)
    escapes it.

    Note what this paragraph used to claim and no longer does. It said the
    scan could not follow an alias or a rebinding either, and that a
    subclass was reachable only through a separate test. That was a
    statement about the *scan as written*, presented as a limit of static
    analysis, and it was false: :func:`~tests.ast_rules.construction_names`
    — lifted from #488's own rule — resolves all three within a module, and
    :func:`ungated_executors` uses it. What genuinely survives is a binding
    whose value is a call (``functools.partial(MutationExecutor)``) or a
    ``getattr``, and a subclass defined in *another* module; the latter
    stays closed by
    :meth:`TestTheRulePremiseStillHolds.test_nothing_in_src_subclasses_the_executor`,
    which looks for the class definition anywhere in ``src/`` rather than
    for its constructions.

    That is why the behavioural assertions live beside it: the worker suite
    proves a declared ``deny`` actually stops the write, and
    :class:`TestTheRulePremiseStillHolds` proves omission is still what
    disables Stage 2. A static rule bounds the shape; a test bounds the
    behaviour.
    """
    return _kwarg_is_wired(node, _GATE_KWARG)


def _kwarg_is_wired(node: ast.Call, kwarg: str) -> bool:
    """Is *kwarg* supplied here with a value that is statically not ``None``?"""
    if any(kw.arg is None for kw in node.keywords):
        return False
    for kw in node.keywords:
        if kw.arg != kwarg:
            continue
        return not any(
            isinstance(sub, ast.Constant) and sub.value is None
            for sub in ast.walk(kw.value)
        )
    return False


def _log_is_wired(node: ast.Call) -> bool:
    """Does this construction pass an event log Stage 5 could emit to?"""
    return _kwarg_is_wired(node, _LOG_KWARG)


def _executor_constructions(
    tree: ast.AST, names: Collection[str] | None = None
) -> list[ast.Call]:
    """Executor constructions in *tree*, under every name that reaches one.

    *names* is resolved once per **module** and passed down, because
    :func:`construction_names` reads module-level imports and assignments
    that a function subtree cannot see. Calling it per function would make
    an alias resolvable in one scope and invisible in another, which is a
    worse failure than not resolving at all.
    """
    resolved = names if names is not None else construction_names(_EXECUTOR, tree)
    return calls_to_any(resolved, tree)


def _gate_builder_calls(
    tree: ast.AST, names: Collection[str] | None = None
) -> list[ast.Call]:
    resolved = names if names is not None else construction_names(_GATE_BUILDER, tree)
    return calls_to_any(resolved, tree)


def ungated_executors(root: Path | None = None) -> list[str]:
    """Invariant 1: construction sites that leave Stage 2 skipped.

    Resolving through :func:`~tests.ast_rules.construction_names` rather
    than matching one literal name. #488 shipped the bare-name-only
    version of exactly this scan and it was flagged in review as
    reproducing *inside the rule* the defect the rule removes from the
    code; an aliased import, a local rebinding and a subclass each
    construct an executor under a name a literal match never sees.
    """
    found: list[str] = []
    for py_file, tree in iter_modules(root if root is not None else _src_root()):
        names = construction_names(_EXECUTOR, tree)
        found.extend(
            f"{py_file.name}:{node.lineno}: {_snippet(node)}"
            for node in _executor_constructions(tree, names)
            if not _gate_is_wired(node)
        )
    return found


def unlogged_executors(root: Path | None = None) -> list[str]:
    """Invariant 3: construction sites that leave Stage 5 silent.

    The same scan as :func:`ungated_executors` over the same population,
    reading the other keyword. Sharing the construction scan is the point:
    whatever makes an executor visible to one rule makes it visible to both,
    so a new alias or rebinding cannot close one eye.
    """
    found: list[str] = []
    for py_file, tree in iter_modules(root if root is not None else _src_root()):
        names = construction_names(_EXECUTOR, tree)
        found.extend(
            f"{py_file.name}:{node.lineno}: {_snippet(node)}"
            for node in _executor_constructions(tree, names)
            if not _log_is_wired(node)
        )
    return found


def stray_gate_builds(root: Path | None = None) -> list[str]:
    """Invariant 2: gate builds outside a function that builds an executor.

    Scoped to the *enclosing function*, and deliberately not to the module:
    a module-level helper that builds a gate for something other than an
    executor is precisely the case that would make the ``WRITE_REJECTED``
    label a lie, and a module-scoped check would pass it as long as any
    other function in the same file built an executor.
    """
    found: list[str] = []
    for py_file, tree in iter_modules(root if root is not None else _src_root()):
        executors = construction_names(_EXECUTOR, tree)
        builders = construction_names(_GATE_BUILDER, tree)
        owned: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _executor_constructions(node, executors):
                continue
            owned.update(id(call) for call in _gate_builder_calls(node, builders))
        found.extend(
            f"{py_file.name}:{call.lineno}: {_snippet(call)}"
            for call in _gate_builder_calls(tree, builders)
            if id(call) not in owned
        )
    return found


def executor_subclasses(root: Path | None = None) -> list[str]:
    """Classes inheriting ``MutationExecutor``, wherever they are built.

    ``ungated_executors`` now resolves a *same-module* subclass through
    :func:`~tests.ast_rules.construction_names`, so this is no longer the
    only thing between a subclass and a skipped Stage 2. It stays
    load-bearing for the case resolution cannot reach: a subclass defined
    in one module and constructed in another, where a per-module fixed
    point has nothing to resolve against. This function looks for the
    *class definition* anywhere in ``src/`` rather than for its
    constructions, which is exactly why it closes the cross-module shape —
    and why ``cross_module_subclass`` is one of the two shapes this rule
    exempts from the shared roster.

    There are none, so this is a floor to keep rather than a hole to
    close.
    """
    found: list[str] = []
    for py_file, tree in iter_modules(root if root is not None else _src_root()):
        found.extend(
            f"{py_file.name}:{node.lineno}: class {node.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(name_of(base) == _EXECUTOR for base in node.bases)
        )
    return found


def _snippet(node: ast.AST) -> str:
    return ast.unparse(node).replace("\n", " ")[:90]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def test_every_executor_in_src_wires_stage_two() -> None:
    violations = ungated_executors()
    assert not violations, (
        "A MutationExecutor built without policy_gate= skips Stage 2 "
        "entirely (#424): the deployment's declared policies are not "
        "enforced on that path, silently. Build it through "
        "trellis.mutate.build_curate_executor, or pass "
        "policy_gate=build_policy_gate(registry).\n  " + "\n  ".join(violations)
    )


def test_every_executor_in_src_wires_stage_five() -> None:
    violations = unlogged_executors()
    assert not violations, (
        "A MutationExecutor built without event_log= runs Stage 5 as a "
        "no-op (#551): every mutation it commits is absent from the audit "
        "log, and the CommandResult still reports SUCCESS, so nothing "
        "anywhere says the record is missing. Build it through "
        "trellis.mutate.build_curate_executor, or pass "
        "event_log=registry.operational.event_log — a deployment that "
        "wants no audit log configures the null backend and gets a "
        "NullEventLog (#196), which satisfies this rule and says so.\n  "
        + "\n  ".join(violations)
    )


def test_the_gate_is_only_built_where_a_write_is_about_to_happen() -> None:
    violations = stray_gate_builds()
    assert not violations, (
        "build_policy_gate() records a load failure as a WRITE_REJECTED "
        "event (#425). That is only honest while every call sits on a write "
        "path — i.e. in a function that builds a MutationExecutor. Move the "
        "call, or revisit trellis.mutate.policy_source."
        "_record_gate_load_failure before adding a reader that is not a "
        "writer.\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Vacuity guards
# ---------------------------------------------------------------------------


def _population() -> tuple[int, int]:
    """How many executor constructions and gate builds the scan can see."""
    executors = 0
    gates = 0
    for _py_file, tree in iter_modules(_src_root()):
        executors += len(_executor_constructions(tree))
        gates += len(_gate_builder_calls(tree))
    return executors, gates


#: Hand-read off ``src/`` on 2026-09-03: ``build_curate_executor`` and the
#: ``trace_embed`` worker construct one each, and each builds its own gate.
#: A number a person counted is the only floor a silently-narrowing scan
#: cannot also satisfy — #466 floored a roster at ``len(SITES) > 0`` and
#: three blind spots passed it.
_EXECUTOR_FLOOR = 2
_GATE_BUILD_FLOOR = 2


def test_the_scan_finds_the_sites_it_is_meant_to_police() -> None:
    """A scan that stops matching enforces nothing and stays green.

    Both counts are floors, not equalities: a new *gated* construction site
    is a normal thing to add and must not turn the suite red. What must
    turn it red is the scan finding nothing — the class renamed, the package
    moved, the executor replaced.
    """
    executors, gates = _population()
    assert_hand_read_floor(
        executors,
        _EXECUTOR_FLOOR,
        subject="MutationExecutor construction",
        hint="build_curate_executor and trellis_workers/trace_embed/worker.py.",
    )
    assert_hand_read_floor(
        gates,
        _GATE_BUILD_FLOOR,
        subject="build_policy_gate call",
        hint="one beside each executor construction.",
    )


def test_the_scan_sees_every_shape_in_the_shared_evasion_roster(
    tmp_path: Path,
) -> None:
    """The shared half of the guard, run through ``ungated_executors``.

    The corpus below is this rule's own, and it stays: it pins the
    *judgement* — which shapes count as ungated — against hand-read line
    numbers. What it cannot do is know about a shape nobody thought of
    here, which is how #488 shipped a rule whose synthetic tree carried
    only the spellings its scan already handled. ``tests.ast_rules.EVASIONS``
    is the cross-rule roster, so a shape found by *any* rule's review
    reaches this one without anyone re-deriving it.

    Two exemptions remain and both are **residue** — shapes no scanner
    in ``tests.ast_rules`` reports, the resolving one included, which the
    harness demonstrates rather than asserts. The first cut of this test
    exempted three *resolvable* shapes (alias, rebinding, subclass) on the
    strength of a docstring claim that turned out to be false: #488's
    ``_construction_names`` already resolved all three. The scan now
    resolves them too, and the harness refuses a non-residue exemption
    outright.
    """
    assert_scan_is_not_vacuous(
        lambda root: [int(v.split(":")[1]) for v in ungated_executors(root=root)],
        subject=_EXECUTOR,
        kwarg=_GATE_KWARG,
        tmp_path=tmp_path,
        live_population=_population()[0],
        floor=_EXECUTOR_FLOOR,
        exempt={
            "partial_binding": (
                "residue: the binding's value is a call, so there is no "
                "name for construction_names to resolve. Bounded instead "
                "by TestTheRulePremiseStillHolds, which proves omitting "
                "the gate really is what disables Stage 2, and by the "
                "worker suite proving a declared deny stops the write"
            ),
            "cross_module_subclass": (
                "residue: a subclass defined in another module is not in "
                "this tree. Closed by executor_subclasses(), which scans "
                "all of src/ for the class definition rather than for its "
                "constructions, and fails on the first one added"
            ),
        },
    )


def test_the_log_scan_sees_every_shape_in_the_shared_evasion_roster(
    tmp_path: Path,
) -> None:
    """The same roster, the same harness, the other keyword.

    Invariant 3 reuses :func:`_executor_constructions`, so it inherits
    whatever that resolves — but inheriting a scan is not evidence that
    *this* predicate reads it correctly, and ``kwarg=`` is what the
    harness substitutes into every shape. The two exemptions carry over
    for the same structural reason and neither is about the keyword: they
    are residue of the construction scan, which both invariants share.
    """
    assert_scan_is_not_vacuous(
        lambda root: [int(v.split(":")[1]) for v in unlogged_executors(root=root)],
        subject=_EXECUTOR,
        kwarg=_LOG_KWARG,
        tmp_path=tmp_path,
        live_population=_population()[0],
        floor=_EXECUTOR_FLOOR,
        exempt={
            "partial_binding": (
                "residue: the binding's value is a call, so there is no "
                "name for construction_names to resolve. Same shape the "
                "gate rule exempts, and bounded the same way — this is a "
                "property of the shared construction scan, not of which "
                "keyword is being read off it"
            ),
            "cross_module_subclass": (
                "residue: a subclass defined in another module is not in "
                "this tree. Closed by executor_subclasses(), which both "
                "invariants rest on because both are blind to a subclass "
                "for the same reason and are closed by the same scan"
            ),
        },
    )


#: Every shape that walks past a naive version of these rules, plus the
#: controls. The comment on each line is what makes it invisible.
_EVASIONS = """
from trellis import mutate
from trellis.mutate import MutationExecutor, build_policy_gate

gate = build_policy_gate(registry)             # 4  stray: module level

def bare(registry):
    return MutationExecutor(handlers={})       # 7  the control: no gate

def explicit_none(registry):
    return MutationExecutor(policy_gate=None)  # 10 keyword present, gate off

def qualified(registry):
    return mutate.MutationExecutor(handlers={})  # 13 attribute access

def splatted(registry, **kwargs):
    return MutationExecutor(**kwargs)          # 16 opaque to a static scan

def gate_without_executor(registry):
    return build_policy_gate(registry)         # 19 stray: no executor here

def conditional_none(registry, flag):
    return MutationExecutor(                   # 22 literal None on a branch
        policy_gate=build_policy_gate(registry) if flag else None,
    )

def splat_beside_a_real_gate(registry, **kwargs):
    gate = build_policy_gate(registry)
    return MutationExecutor(policy_gate=gate, **kwargs)  # 28 unreadable wiring

class Sneaky(MutationExecutor):                # 30 constructed under its own name
    pass

def ok_inline(registry):
    return MutationExecutor(policy_gate=build_policy_gate(registry))  # 34 OK

def ok_hoisted(registry):
    gate = build_policy_gate(registry)         # 37 OK: same function
    return MutationExecutor(handlers={}, policy_gate=gate)  # 38 OK

def ok_both_ends(registry):
    return MutationExecutor(                   # 41 OK: stages 2 and 5 wired
        policy_gate=build_policy_gate(registry),
        event_log=registry.operational.event_log,
    )

def stage_five_off(registry):
    return MutationExecutor(                   # 47 gated, but the log is off
        policy_gate=build_policy_gate(registry),
        event_log=None,
    )
"""

#: ``ungated_executors`` must report exactly these lines of ``_EVASIONS``.
_EXPECTED_UNGATED = [7, 10, 13, 16, 22, 28]

#: ``unlogged_executors`` must report exactly these. Every construction
#: above predates invariant 3 and names no ``event_log``, so the two lists
#: overlap without being derivable from each other — line 47 is gated and
#: unlogged, lines 34 and 38 are logged by neither and gated by both, and
#: :data:`_EXPECTED_UNGATED` is unchanged by this rule's arrival. That is
#: the property worth having: one keyword's wiring says nothing about the
#: other's, which is exactly why #424's fix did not close #551.
_EXPECTED_UNLOGGED = [7, 10, 13, 16, 22, 28, 34, 38, 47]

#: ``stray_gate_builds`` must report exactly these.
_EXPECTED_STRAY = [4, 19]

#: ``executor_subclasses`` must report exactly these.
_EXPECTED_SUBCLASSES = [30]


def test_the_scan_catches_every_known_evasion(tmp_path: Path) -> None:
    """Mutation guard, run through the shipped scanners rather than a copy.

    The point is that these functions detect a **newly added** ungated
    construction site, which is the whole claim the rule makes. Running the
    guard against a re-implementation of the predicate would leave the
    shipped one free to regress with the suite still green — the exact
    failure ``test_machine_output_rule`` recorded and fixed.
    """
    # ``lstrip`` so the line numbers in the comments above are the real ones.
    (tmp_path / "evasions.py").write_text(_EVASIONS.lstrip("\n"), encoding="utf-8")

    ungated = sorted(int(v.split(":")[1]) for v in ungated_executors(root=tmp_path))
    assert ungated == _EXPECTED_UNGATED, (
        f"ungated_executors reported {ungated}, expected {_EXPECTED_UNGATED}; "
        f"missing={sorted(set(_EXPECTED_UNGATED) - set(ungated))} "
        f"spurious={sorted(set(ungated) - set(_EXPECTED_UNGATED))}"
    )

    unlogged = sorted(int(v.split(":")[1]) for v in unlogged_executors(root=tmp_path))
    assert unlogged == _EXPECTED_UNLOGGED, (
        f"unlogged_executors reported {unlogged}, expected "
        f"{_EXPECTED_UNLOGGED}; "
        f"missing={sorted(set(_EXPECTED_UNLOGGED) - set(unlogged))} "
        f"spurious={sorted(set(unlogged) - set(_EXPECTED_UNLOGGED))}"
    )

    stray = sorted(int(v.split(":")[1]) for v in stray_gate_builds(root=tmp_path))
    assert stray == _EXPECTED_STRAY, (
        f"stray_gate_builds reported {stray}, expected {_EXPECTED_STRAY}; "
        f"missing={sorted(set(_EXPECTED_STRAY) - set(stray))} "
        f"spurious={sorted(set(stray) - set(_EXPECTED_STRAY))}"
    )

    subclasses = sorted(
        int(v.split(":")[1]) for v in executor_subclasses(root=tmp_path)
    )
    assert subclasses == _EXPECTED_SUBCLASSES, (
        f"executor_subclasses reported {subclasses}, expected {_EXPECTED_SUBCLASSES}"
    )


class TestTheRulePremiseStillHolds:
    """The rule is only worth enforcing while omission is dangerous.

    If ``MutationExecutor`` ever grows a real default gate or event log, or
    renames either parameter, the scans above would keep passing while
    policing a property the code no longer expresses. These pin the premise
    instead of assuming it, which is what makes the rules above falsifiable.

    Both keywords get a signature test *and* a behavioural one, because a
    signature says the argument exists and only the behaviour says omitting
    it is dangerous. The two behavioural tests are deliberately shaped the
    same and deliberately assert different things: omitting the gate changes
    what the caller is told (REJECTED becomes SUCCESS), and omitting the log
    changes nothing the caller can see at all. The second is the harder
    defect and the reason invariant 3 has to be structural — there is no
    result to inspect, so no test of an individual write path could have
    noticed.
    """

    def test_the_parameter_is_still_called_policy_gate_and_defaults_to_none(
        self,
    ) -> None:
        parameter = inspect.signature(MutationExecutor.__init__).parameters.get(
            _GATE_KWARG
        )
        assert parameter is not None, (
            f"MutationExecutor.__init__ has no {_GATE_KWARG} parameter; the "
            f"AST rule is matching a keyword that no longer exists"
        )
        assert parameter.default is None, (
            "MutationExecutor now defaults to a gate. If that default is a "
            "real gate rather than None, omission is no longer dangerous and "
            "this rule should be re-argued rather than kept out of habit."
        )

    def test_the_parameter_is_still_called_event_log_and_defaults_to_none(
        self,
    ) -> None:
        parameter = inspect.signature(MutationExecutor.__init__).parameters.get(
            _LOG_KWARG
        )
        assert parameter is not None, (
            f"MutationExecutor.__init__ has no {_LOG_KWARG} parameter; the "
            f"AST rule is matching a keyword that no longer exists"
        )
        assert parameter.default is None, (
            "MutationExecutor now defaults to an event log. If that default "
            "is a real log rather than None, omission is no longer dangerous "
            "and this rule should be re-argued rather than kept out of "
            "habit. Note which direction would be an improvement: a "
            "NullEventLog default would keep Stage 5 a no-op while making "
            "the rule unenforceable, so it is not one."
        )

    def test_nothing_in_src_subclasses_the_executor(self) -> None:
        """The construction scan matches one bare name, so a subclass hides.

        ``ungated_executors`` looks for ``MutationExecutor(...)``. A
        subclass is constructed as ``MySubclass(...)`` and forwards through
        ``super().__init__(...)``, so all three invariants stay green while
        Stage 2 and Stage 5 are skipped on every write it makes — the #424
        and #551 shapes, one indirection further out. There are none today;
        if one is added, the rules above have to be re-derived rather than
        the scanner given another special case.
        """
        subclasses = executor_subclasses()
        assert not subclasses, (
            "A MutationExecutor subclass is invisible to the AST rules "
            "above: it is constructed under its own name and reaches "
            "__init__ through super(). Either drop the subclass, or widen "
            "ungated_executors to follow the inheritance and re-argue the "
            "rule.\n  " + "\n  ".join(subclasses)
        )

    def test_omitting_the_gate_really_does_skip_stage_two(self) -> None:
        """Behavioural, not signature-shaped: the same command, twice.

        A denying gate rejects it; no gate at all lets it through to the
        handler. That difference *is* #424 — on the worker path it was the
        difference between a declared ``deny evidence.*`` being enforced and
        being ignored.
        """
        from trellis.mutate.policy_gate import DefaultPolicyGate
        from trellis.schemas.enums import Enforcement, PolicyType
        from trellis.schemas.policy import Policy, PolicyRule, PolicyScope

        class _EchoHandler:
            def handle(self, command: Command) -> tuple[str | None, str]:
                return "created-1", "ok"

        deny_all = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="global", value=None),
            rules=[PolicyRule(operation="*", action="deny")],
            enforcement=Enforcement.ENFORCE,
        )
        command = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "auth"},
        )
        handlers = {Operation.ENTITY_CREATE: _EchoHandler()}

        gated = MutationExecutor(
            handlers=handlers, policy_gate=DefaultPolicyGate(policies=[deny_all])
        )
        ungated = MutationExecutor(handlers=handlers)

        assert gated.execute(command).status is CommandStatus.REJECTED
        assert ungated.execute(command).status is CommandStatus.SUCCESS

    def test_omitting_the_event_log_really_does_skip_stage_five(self) -> None:
        """The same shape as the gate test, and the opposite result.

        A missing gate is visible in what the caller is handed back. A
        missing event log is not: both executors commit the write and both
        report SUCCESS, and the only difference is a row that does not
        exist in a store the caller never reads. That is #551's second
        consequence, and it is why this one is closed by construction — a
        behavioural test can demonstrate the defect but no behavioural test
        could ever *find* it on a path that shipped without a log.
        """

        class _EchoHandler:
            def handle(self, command: Command) -> tuple[str | None, str]:
                return "created-1", "ok"

        command = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "auth"},
        )
        handlers = {Operation.ENTITY_CREATE: _EchoHandler()}
        event_log = MagicMock(spec=EventLog)
        event_log.has_idempotency_key.return_value = False

        logged = MutationExecutor(handlers=handlers, event_log=event_log)
        unlogged = MutationExecutor(handlers=handlers)

        logged_result = logged.execute(command)
        unlogged_result = unlogged.execute(command)

        assert logged_result.status is CommandStatus.SUCCESS
        assert unlogged_result.status is CommandStatus.SUCCESS
        assert unlogged_result.warnings == logged_result.warnings == []
        assert event_log.emit.call_count == 1, (
            "Stage 5 did not emit for the executor that was given a log; "
            "this test can no longer tell the two apart"
        )
