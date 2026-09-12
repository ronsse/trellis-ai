"""Enforcement for the outcome-store bridge rule (#557).

``trellis.feedback.recording.record_feedback`` guards its only bridge to
the ops tier on a keyword nobody passed::

    if outcome_store is not None:
        _emit_outcome(...)

``_emit_outcome`` is the sole producer of an
:class:`~trellis.schemas.outcome.OutcomeEvent` from pack feedback, and
``OutcomeEvent`` is the sole input of :class:`RuleTuner`. Both agent-facing
surfaces — the MCP ``record_feedback`` tool and
``POST /packs/{pack_id}/feedback`` — called this function without the
kwarg, so the parameter-tuning subsystem had **zero rows** since the day
its database was created, against 72 graded feedback events in the trailing
30 days. Nothing raised, nothing logged, and ``trellis metrics outcomes``
reported an empty table that reads exactly like an idle deployment.

**The two-line fix is not the deliverable; preventing the third site is.**
This is the shape #424 already paid for once — a factory that wires a
subsystem correctly, one call path that does not, and no way to tell from
the outside. There the surviving site was a ``MutationExecutor`` built
outside ``build_curate_executor``; here it is a bridge kwarg that is
*optional by design*. The default stays ``None``: the bridge is
deliberately fail-soft (``recording.py``'s GRACEFUL-DEGRADATION comment)
and a core module must not reach into ``trellis.stores.registry`` to find
a store for itself. What is left is a keyword that is easy to forget and
whose omission is silent, which is exactly the condition an AST rule is
for.

So one invariant, derived by AST over ``src/`` rather than from a list of
call sites — this repo has shipped two rosters that rotted (#443's three
control keys against six ``pop`` sites; three successive wrong lists of
``updated_at`` readers):

    **Every call to** ``trellis.feedback.recording.record_feedback`` **in**
    ``src/`` **passes a real** ``outcome_store=``.

``src/`` only. ``tests/`` calls it without one constantly and must keep
being able to — the whole point of the ``None`` default is that the JSONL
append works with no ops tier at all.

**The name is the hard part of this rule, and it is why the scan is not
just** :func:`~tests.ast_rules.calls_named`. ``record_feedback`` names five
different callables in ``src/``: the core function, ``TrellisClient``'s
method, its async twin, the REST route function, and the MCP tool. A
trailing-name match — the correct default for a class, and what
:func:`~tests.ast_rules.name_of` is documented to do — reports **4** sites
here, two of them ``self._client.record_feedback(...)`` in
``trellis_sdk/hooks.py``, which post to the REST route and could not pass
an ops-tier store if they wanted to. Over-collection is normally the cheap
direction, but a rule that demands an impossible kwarg from an SDK method
is a rule someone deletes. Two resolution steps fix it without narrowing
to a literal name:

* an **attribute-spelled** call counts only when the root of its dotted
  chain is a name the module bound with ``import`` — so ``pkg.sub.f(...)``
  lands and ``self._client.f(...)`` does not;
* the **bare name** is dropped when the module defines its own
  module-level ``def``/``class`` of that name and does not also import it
  bare. That is Python's own scoping, and it is live here: both modules
  that call the core function also define their own ``record_feedback``
  surface one screen away, each importing the core one as
  ``record_pack_feedback``.

Both live sites are reached **only** through the alias, so
:func:`~tests.ast_rules.construction_names` is load-bearing rather than
decorative in this rule — a literal-name scan finds zero call sites and
passes.

Guarding against vacuity is the other half, on the same four axes as
``test_policy_gate_rule.py``: the scan must find sites to reason about (a
**hand-read** floor), it must report every shape in
:data:`tests.ast_rules.EVASIONS` it has not exempted *through the shipped
predicate*, it must report the known-evasion corpus below (this rule's own
judgement, hand-read line by line), and the parameter must still default to
``None`` — a real default would make the rule enforce a property the code
no longer has.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Collection
from pathlib import Path

from tests.ast_rules import (
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    construction_names,
    iter_modules,
)
from trellis.feedback.recording import record_feedback

#: The function whose calls the rule polices.
_SUBJECT = "record_feedback"

#: The keyword that wires the ops-tier bridge.
_STORE_KWARG = "outcome_store"


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src not found at {root}"
    return root


def _import_roots(tree: ast.AST) -> set[str]:
    """Names this module bound with ``import`` — the roots of dotted calls.

    ``import pkg`` and ``import pkg.sub`` both bind ``pkg``; ``import
    pkg.sub as ps`` binds ``ps``. A dotted call is only the policed
    function when its chain starts at one of these, which is what keeps
    ``self._client.record_feedback(...)`` out while ``pkg.sub.f(...)``
    stays in.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
    return roots


def _chain_root(node: ast.AST) -> str | None:
    """Leading ``Name`` of a dotted chain — ``a`` in ``a.b.c``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _shadows_the_subject(tree: ast.Module) -> bool:
    """Does this module bind ``record_feedback`` to something of its own?

    True when it defines a module-level ``def``/``async def``/``class`` of
    that name **and** does not also import the bare name. Both conditions
    matter: the definition is what rebinds it, and a module that imports it
    bare has the import win, so dropping the name there would open a hole
    instead of closing a false positive.

    Live in two modules — ``mcp/server.py`` and ``routes/curate.py`` each
    define their own ``record_feedback`` surface and import the core
    function as ``record_pack_feedback``. Neither calls its own under the
    bare name today, so this currently removes nothing; it is a floor to
    keep rather than a hole to close, and
    :func:`test_a_modules_own_definition_is_not_the_policed_function` pins
    that it works rather than asserting today's empty result.
    """
    defines = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == _SUBJECT
        for node in tree.body
    )
    if not defines:
        return False
    imports_bare = any(
        alias.name == _SUBJECT and alias.asname is None
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    return not imports_bare


def subject_names(tree: ast.Module) -> set[str]:
    """Every local name in *tree* that reaches the core function.

    :func:`~tests.ast_rules.construction_names` resolves the alias, local
    rebindings, the walrus and annotated forms, and a same-module subclass;
    the shadow rule above removes the bare name when the module has
    rebound it to something of its own.
    """
    names = construction_names(_SUBJECT, tree)
    return names - {_SUBJECT} if _shadows_the_subject(tree) else names


def subject_calls(
    tree: ast.Module, names: Collection[str] | None = None
) -> list[ast.Call]:
    """Calls to the core function in *tree*, under every name that reaches it.

    *names* is resolved once per **module** and passed down, because the
    resolver reads module-level imports and assignments that a function
    subtree cannot see. ``ast.walk`` rather than a descent over statements,
    so an ``except`` handler, a ``with`` item, a decorator and a keyword
    argument are all reachable — the roster pins each.
    """
    resolved = names if names is not None else subject_names(tree)
    import_roots = _import_roots(tree)
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            matched = func.id in resolved
        elif isinstance(func, ast.Attribute):
            matched = func.attr in resolved and _chain_root(func) in import_roots
        else:
            matched = False
        if matched:
            found.append(node)
    return found


def _store_is_wired(node: ast.Call) -> bool:
    """Does this call pass a store that could actually receive anything?

    Four shapes are rejected, and each is a way the rule gets satisfied
    without the bridge being wired:

    * no ``outcome_store=`` at all — the #557 defect verbatim, on both
      shipped surfaces;
    * ``outcome_store=None`` — clears a keyword-presence check while
      leaving ``record_feedback``'s own ``is not None`` guard false;
    * **any** literal ``None`` inside the value —
      ``outcome_store=registry.operational.outcome_store if flag else
      None`` is the next edit after that one, and just as visible;
    * ``**kwargs`` — opaque to a static scan. Rejecting it is conservative
      by design and no call site in ``src/`` needs the splat. It only ever
      *decides* the ``outcome_store=store, **kwargs`` shape (a splat alone
      already falls through to ``return False``), so that shape is in the
      corpus below — otherwise the clause is unpinned and deleting it
      changes nothing.

    What it cannot decide is whether a *bound name* is ``None`` at runtime:
    ``outcome_store=store`` passes whatever ``store`` holds, so hoisting
    the conditional above it escapes the scan. That is bounded
    behaviourally instead, by
    :meth:`TestTheRulePremiseStillHolds.test_omitting_the_store_really_does_skip_the_bridge`.
    """
    if any(kw.arg is None for kw in node.keywords):
        return False
    for kw in node.keywords:
        if kw.arg != _STORE_KWARG:
            continue
        return not any(
            isinstance(sub, ast.Constant) and sub.value is None
            for sub in ast.walk(kw.value)
        )
    return False


def unbridged_feedback_calls(root: Path | None = None) -> list[str]:
    """The rule: call sites that leave the ops tier with no input."""
    found: list[str] = []
    for py_file, tree in iter_modules(root if root is not None else _src_root()):
        names = subject_names(tree)
        found.extend(
            f"{py_file.name}:{node.lineno}: {_snippet(node)}"
            for node in subject_calls(tree, names)
            if not _store_is_wired(node)
        )
    return found


def _snippet(node: ast.AST) -> str:
    return ast.unparse(node).replace("\n", " ")[:90]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_every_feedback_recording_call_in_src_feeds_the_ops_tier() -> None:
    violations = unbridged_feedback_calls()
    assert not violations, (
        "record_feedback() without outcome_store= writes the JSONL row and "
        "the FEEDBACK_RECORDED event and then drops the signal on the "
        "floor (#557): _emit_outcome never runs, so RuleTuner has no input "
        "and every tuning proposal it could make is silently unreachable. "
        "Pass outcome_store=registry.operational.outcome_store.\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Vacuity guards
# ---------------------------------------------------------------------------


def _population() -> int:
    """How many calls to the core function the scan can see in ``src/``."""
    return sum(len(subject_calls(tree)) for _py_file, tree in iter_modules(_src_root()))


#: Hand-read off ``src/`` on 2026-09-12: the MCP ``record_feedback`` tool
#: and ``POST /packs/{pack_id}/feedback`` are the only two callers, and both
#: reach it as ``record_pack_feedback``. A number a person counted is the
#: only floor a silently-narrowing scan cannot also satisfy — #466 floored a
#: roster at ``len(SITES) > 0`` and three blind spots passed it.
_CALL_SITE_FLOOR = 2


def test_the_scan_finds_the_sites_it_is_meant_to_police() -> None:
    """A scan that stops matching enforces nothing and stays green.

    A floor, not an equality: a new *wired* caller is a normal thing to add.
    What must turn this red is the scan finding nothing — the function
    renamed, the module moved, the alias changed to a spelling the resolver
    does not follow.
    """
    assert_hand_read_floor(
        _population(),
        _CALL_SITE_FLOOR,
        subject=f"{_SUBJECT} call",
        hint="the MCP record_feedback tool and POST /packs/{pack_id}/feedback.",
    )


def test_the_scan_sees_every_shape_in_the_shared_evasion_roster(
    tmp_path: Path,
) -> None:
    """The shared half of the guard, run through the shipped predicate.

    The corpus below is this rule's own and pins its *judgement* — which
    shapes count as unwired — against hand-read line numbers. What a local
    corpus cannot do is know about a shape nobody thought of here, which is
    how #488 shipped a rule whose synthetic tree carried only the spellings
    its scan already handled. The shared roster carries every shape any
    rule's review has found.

    Two exemptions, both **residue**: shapes no scanner in
    ``tests.ast_rules`` reports, which the harness demonstrates rather than
    asserts. The harness refuses a non-residue exemption outright.
    """
    assert_scan_is_not_vacuous(
        lambda root: [
            int(v.split(":")[1]) for v in unbridged_feedback_calls(root=root)
        ],
        subject=_SUBJECT,
        kwarg=_STORE_KWARG,
        tmp_path=tmp_path,
        live_population=_population(),
        floor=_CALL_SITE_FLOOR,
        exempt={
            "partial_binding": (
                "residue: the binding's value is a call, so there is no "
                "name for construction_names to resolve. Bounded instead "
                "by TestTheRulePremiseStillHolds, which proves omitting "
                "the kwarg really is what silences the bridge"
            ),
            "cross_module_subclass": (
                "residue: a subclass defined in another module is not in "
                "this tree, and a per-module fixed point has nothing to "
                "resolve against. Closing it needs a whole-tree pass, "
                "which is a different rule rather than a wider predicate"
            ),
        },
    )


#: Every shape that walks past a naive version of this rule, plus the
#: controls. The comment on each line is what makes it invisible — and the
#: last three are the collision this rule exists to navigate.
_EVASIONS = """
import trellis
from trellis.feedback.recording import record_feedback as record_pack_feedback


class _Surface:
    def __init__(self, client):
        self._client = client

    def grade(self, pack_id):
        return self._client.record_feedback(pack_id)   # 10 not the function

def bare(feedback, log_dir):
    return record_pack_feedback(feedback, log_dir=log_dir)  # 13 the control

def explicit_none(feedback, log_dir, store):
    return record_pack_feedback(feedback, outcome_store=None)  # 16 keyword off

def qualified(feedback, store):
    return trellis.record_feedback(feedback, outcome_store=store)  # 19 OK

def qualified_bare(feedback):
    return trellis.record_feedback(feedback)           # 22 dotted, unwired

def splatted(feedback, **kwargs):
    return record_pack_feedback(feedback, **kwargs)    # 25 opaque wiring

def conditional_none(feedback, registry, flag):
    return record_pack_feedback(                       # 28 literal None on a branch
        feedback,
        outcome_store=registry.operational.outcome_store if flag else None,
    )

def splat_beside_a_real_store(feedback, store, **kwargs):
    return record_pack_feedback(feedback, outcome_store=store, **kwargs)  # 34

def rebound(feedback, store):
    _local = record_pack_feedback
    return _local(feedback)                            # 38 rebound, unwired

def ok_inline(feedback, registry):
    return record_pack_feedback(                       # 41 OK
        feedback, outcome_store=registry.operational.outcome_store
    )
"""

#: ``unbridged_feedback_calls`` must report exactly these lines.
#:
#: Line 10 is the one that matters most: it is the shape a trailing-name
#: match reports and this scan must not, because ``self._client`` is not a
#: module import. Line 19 is its mirror — the same spelling rooted at a
#: real import, which the scan must see.
_EXPECTED_UNBRIDGED = [13, 16, 22, 25, 28, 34, 38]


def test_the_scan_catches_every_known_evasion(tmp_path: Path) -> None:
    """Mutation guard, run through the shipped scanner rather than a copy.

    Running it against a re-implementation of the predicate would leave the
    shipped one free to regress with the suite still green — the failure
    ``test_machine_output_rule`` recorded and fixed.
    """
    # ``lstrip`` so the line numbers in the comments above are the real ones.
    (tmp_path / "evasions.py").write_text(_EVASIONS.lstrip("\n"), encoding="utf-8")

    reported = sorted(
        int(v.split(":")[1]) for v in unbridged_feedback_calls(root=tmp_path)
    )
    assert reported == _EXPECTED_UNBRIDGED, (
        f"unbridged_feedback_calls reported {reported}, expected "
        f"{_EXPECTED_UNBRIDGED}; "
        f"missing={sorted(set(_EXPECTED_UNBRIDGED) - set(reported))} "
        f"spurious={sorted(set(reported) - set(_EXPECTED_UNBRIDGED))}"
    )


def test_an_sdk_method_of_the_same_name_is_not_reported(tmp_path: Path) -> None:
    """The over-collection this rule's resolution exists to prevent.

    ``trellis_sdk/hooks.py`` calls ``self._client.record_feedback(...)``
    twice. Those post to the REST route; they have no registry and no ops
    tier, and demanding the kwarg from them would make the rule wrong
    rather than strict. A trailing-name match reports 4 sites in ``src/``;
    this scan reports 2.
    """
    (tmp_path / "sdk_like.py").write_text(
        "class _Hook:\n"
        "    def grade(self, pack_id):\n"
        "        return self._client.record_feedback(pack_id)\n"
        "    async def grade_async(self, pack_id):\n"
        "        return await self._client.record_feedback(pack_id)\n",
        encoding="utf-8",
    )
    assert unbridged_feedback_calls(root=tmp_path) == []


def test_a_modules_own_definition_is_not_the_policed_function(
    tmp_path: Path,
) -> None:
    """A module that defines its own ``record_feedback`` has rebound the name.

    Both live callers do exactly this — the MCP tool and the REST route are
    each named ``record_feedback`` in the module that calls the core one
    under an alias. Neither calls its own surface internally today, so the
    shadow rule removes nothing from the live scan; this proves it works
    rather than asserting that it is unused.
    """
    (tmp_path / "surface.py").write_text(
        "from trellis.feedback.recording import record_feedback as _core\n"
        "\n"
        "def record_feedback(req):\n"
        "    return _core(req, outcome_store=req.store)\n"
        "\n"
        "def batch(reqs):\n"
        "    return [record_feedback(r) for r in reqs]\n",
        encoding="utf-8",
    )
    assert unbridged_feedback_calls(root=tmp_path) == []

    # ...and the same module without its own definition is reported, so the
    # emptiness above is the shadow rule rather than the scan going blind.
    (tmp_path / "surface.py").write_text(
        "from trellis.feedback.recording import record_feedback\n"
        "\n"
        "def batch(reqs):\n"
        "    return [record_feedback(r) for r in reqs]\n",
        encoding="utf-8",
    )
    assert len(unbridged_feedback_calls(root=tmp_path)) == 1


class TestTheRulePremiseStillHolds:
    """The rule is only worth enforcing while omission is silent.

    If ``record_feedback`` grows a real default store, or renames the
    parameter, the scan above keeps passing while policing a property the
    code no longer expresses.
    """

    def test_the_parameter_is_still_called_outcome_store_and_defaults_to_none(
        self,
    ) -> None:
        parameter = inspect.signature(record_feedback).parameters.get(_STORE_KWARG)
        assert parameter is not None, (
            f"record_feedback has no {_STORE_KWARG} parameter; the AST rule "
            f"is matching a keyword that no longer exists"
        )
        assert parameter.default is None, (
            "record_feedback now defaults to a store. If that default is a "
            "real store rather than None, omission is no longer silent and "
            "this rule should be re-argued rather than kept out of habit. "
            "Note that the None default is deliberate: the bridge is "
            "fail-soft and core must not import trellis.stores.registry."
        )

    def test_omitting_the_store_really_does_skip_the_bridge(
        self, tmp_path: Path
    ) -> None:
        """Behavioural, not signature-shaped: the same feedback, twice.

        A static rule bounds the shape; this bounds the behaviour. The
        difference between these two calls *is* #557 — 72 graded feedback
        events a month against an outcomes table that has never held a row.
        """
        from trellis.feedback.models import PackFeedback
        from trellis.stores.sqlite.outcome import SQLiteOutcomeStore

        store = SQLiteOutcomeStore(db_path=tmp_path / "outcomes.db")

        without = record_feedback(
            PackFeedback.from_agent_signal(
                run_id="run-a", success=True, pack_id="pack-a"
            ),
            log_dir=tmp_path / "without",
        )
        assert without.outcome_emitted is False
        assert store.query(limit=10) == []

        with_store = record_feedback(
            PackFeedback.from_agent_signal(
                run_id="run-b", success=True, pack_id="pack-b"
            ),
            log_dir=tmp_path / "with",
            outcome_store=store,
        )
        assert with_store.outcome_emitted is True
        emitted = store.query(limit=10)
        assert len(emitted) == 1
        # And the row is the honest one #557's third defect was about: an
        # agent signal carries no serving count, so the denominator is
        # unknown rather than a measured zero.
        assert emitted[0].outcome.items_served is None
