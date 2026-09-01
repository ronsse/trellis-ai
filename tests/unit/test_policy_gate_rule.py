"""Enforcement for the Stage 2 wiring rule (#424).

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
Two invariants are enforced here, both derived by AST over ``src/`` rather
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

``src/`` only. ``tests/`` builds ungated executors on purpose — that is what
``test_policy_wiring.py`` compares the gated ones against — so scanning
tests would forbid the control arm of the transparency proof.

Guarding against vacuity is the other half. A structural scan that stops
matching keeps passing forever while enforcing nothing, so three separate
things pin it: the scan must find construction sites to reason about, it
must report a known set of violations in a synthetic tree *through the
shipped predicate* (not a copy of it), and the executor must still have the
parameter — and the ``None`` default — that makes the rule necessary at all.
Same model as ``test_machine_output_rule.py``.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from pathlib import Path

from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor

#: The class whose construction the rule polices. Matched on the bare name
#: off either a ``Name`` or an ``Attribute``, so ``MutationExecutor(...)``
#: and ``mutate.MutationExecutor(...)`` both land.
_EXECUTOR = "MutationExecutor"

#: The keyword that wires Stage 2.
_GATE_KWARG = "policy_gate"

#: The one supported way to build a gate. Invariant 2 is about *where* it
#: is called, not which callee — the name is matched, the module is not.
_GATE_BUILDER = "build_policy_gate"


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src not found at {root}"
    return root


def _name_of(node: ast.expr) -> str | None:
    """Bare name of a call target: ``f``, ``mod.f``, ``a.b.f`` all give ``f``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_call_to(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and _name_of(node.func) == name


def _gate_is_wired(node: ast.Call) -> bool:
    """Does this construction pass a gate that could actually gate anything?

    Three shapes are rejected and each was reachable:

    * no ``policy_gate=`` at all — the #424 defect verbatim;
    * ``policy_gate=None`` — satisfies a keyword-presence check while
      leaving ``execute``'s ``is not None`` guard false, which is the
      obvious way a future edit "fixes" the rule without fixing anything;
    * ``**kwargs`` — opaque to a static scan. Rejecting it is conservative
      by design: an executor whose wiring cannot be read is exactly the
      state this rule exists to prevent, and no call site in ``src/`` needs
      the splat.

    What it cannot decide is whether a *bound name* is ``None`` at runtime
    — ``policy_gate=gate`` passes the scan whatever ``gate`` holds. That is
    why the behavioural assertions live beside it: the worker suite proves
    a declared ``deny`` actually stops the write, and
    :class:`TestTheRulePremiseStillHolds` proves omission is still what
    disables Stage 2. A static rule bounds the shape; a test bounds the
    behaviour.
    """
    if any(kw.arg is None for kw in node.keywords):
        return False
    for kw in node.keywords:
        if kw.arg != _GATE_KWARG:
            continue
        return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    return False


def _iter_modules(root: Path) -> Iterator[tuple[Path, ast.Module]]:
    for py_file in sorted(root.rglob("*.py")):
        yield py_file, ast.parse(py_file.read_text(encoding="utf-8"))


def _executor_constructions(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if _is_call_to(n, _EXECUTOR)]


def _gate_builder_calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if _is_call_to(n, _GATE_BUILDER)]


def ungated_executors(root: Path | None = None) -> list[str]:
    """Invariant 1: construction sites that leave Stage 2 skipped."""
    found: list[str] = []
    for py_file, tree in _iter_modules(root if root is not None else _src_root()):
        found.extend(
            f"{py_file.name}:{node.lineno}: {_snippet(node)}"
            for node in _executor_constructions(tree)
            if not _gate_is_wired(node)
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
    for py_file, tree in _iter_modules(root if root is not None else _src_root()):
        owned: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _executor_constructions(node):
                continue
            owned.update(id(call) for call in _gate_builder_calls(node))
        found.extend(
            f"{py_file.name}:{call.lineno}: {_snippet(call)}"
            for call in _gate_builder_calls(tree)
            if id(call) not in owned
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


def test_the_scan_finds_the_sites_it_is_meant_to_police() -> None:
    """A scan that stops matching enforces nothing and stays green.

    Both counts are floors, not equalities: a new *gated* construction site
    is a normal thing to add and must not turn the suite red. What must
    turn it red is the scan finding nothing — the class renamed, the package
    moved, the executor replaced.
    """
    executors = 0
    gates = 0
    for _py_file, tree in _iter_modules(_src_root()):
        executors += len(_executor_constructions(tree))
        gates += len(_gate_builder_calls(tree))

    assert executors >= 2, (
        f"only {executors} MutationExecutor construction(s) found in src/; "
        f"the scan has drifted and is no longer policing anything"
    )
    assert gates >= 2, (
        f"only {gates} build_policy_gate call(s) found in src/; the scan has "
        f"drifted and is no longer policing anything"
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

def ok_inline(registry):
    return MutationExecutor(policy_gate=build_policy_gate(registry))  # 22 OK

def ok_hoisted(registry):
    gate = build_policy_gate(registry)         # 25 OK: same function
    return MutationExecutor(handlers={}, policy_gate=gate)  # 26 OK
"""

#: ``ungated_executors`` must report exactly these lines of ``_EVASIONS``.
_EXPECTED_UNGATED = [7, 10, 13, 16]

#: ``stray_gate_builds`` must report exactly these.
_EXPECTED_STRAY = [4, 19]


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

    stray = sorted(int(v.split(":")[1]) for v in stray_gate_builds(root=tmp_path))
    assert stray == _EXPECTED_STRAY, (
        f"stray_gate_builds reported {stray}, expected {_EXPECTED_STRAY}; "
        f"missing={sorted(set(_EXPECTED_STRAY) - set(stray))} "
        f"spurious={sorted(set(stray) - set(_EXPECTED_STRAY))}"
    )


class TestTheRulePremiseStillHolds:
    """The rule is only worth enforcing while omission is dangerous.

    If ``MutationExecutor`` ever grows a real default gate, or renames the
    parameter, the scans above would keep passing while policing a property
    the code no longer expresses. These pin the premise instead of assuming
    it, which is what makes the two rules above falsifiable.
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
