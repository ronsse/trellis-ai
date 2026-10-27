"""Every production call of ``submit_learning_promotion`` passes ``event_log=`` (#996).

#983 made a re-run of a promotion file skip a row whose precedent already holds
it, and #995 shipped that. The skip needs the event log to check that the
``PRECEDENT_PROMOTED`` event is in place, and
:func:`trellis.learning.scoring.submit_learning_promotion` declares it
``EventLog | None = None``. With ``None`` the event cannot be checked, nothing is
skipped, and every row is written again: a second SCD-2 version at the id and a
second promote event, which is the defect #983 removed. A caller that leaves the
argument out raises nothing and reports ``promoted``, so the only thing that can
notice it is a scan. All four production calls pass it today.

One invariant, derived by AST over ``src/`` rather than from a list of known
call sites (this repo has shipped rosters that rotted):

    Every call of ``submit_learning_promotion`` in ``src/`` passes
    ``event_log=`` with a value that is statically not ``None``.

The predicate is the one :mod:`tests.unit.test_policy_gate_rule` uses for
``MutationExecutor(event_log=...)`` (#551), the same defect in a sibling
parameter, re-bound to this subject; it is not imported from there because a
rule's predicate is its own to change. Four shapes are rejected: no keyword, a
literal ``None``, any literal ``None`` inside the value (``event_log=log if
flag else None``), and ``**kwargs``, which a static scan cannot read. What it
cannot decide is whether a *bound name* is ``None`` at runtime, which is why the
one call that forwards a parameter (``candidate_auto_promote``) takes it as a
required ``EventLog``.

Guarding against vacuity follows :mod:`tests.ast_rules`: a hand-read floor, the
shared evasion roster run through the shipped predicate, a corpus pinning this
rule's own judgement, and a premise test that the parameter still exists and
still defaults to ``None``. ``src/`` only: the tests call the function without a
log on purpose, to prove what omitting it does
(``tests/unit/learning/test_submit_promotion_rerun.py``).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from tests.ast_rules import (
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    calls_to_any,
    construction_names,
    iter_modules,
    name_of,
)
from trellis.learning.scoring import submit_learning_promotion

#: The function whose calls the rule polices, matched on the bare name off
#: either a ``Name`` or an ``Attribute``.
_SUBJECT = "submit_learning_promotion"

#: The keyword that lets the function check the promote event.
_LOG_KWARG = "event_log"


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src not found at {root}"
    return root


def _log_is_wired(node: ast.Call) -> bool:
    """Is ``event_log=`` supplied here with a value that is statically not ``None``?"""
    if any(kw.arg is None for kw in node.keywords):
        return False
    for kw in node.keywords:
        if kw.arg != _LOG_KWARG:
            continue
        return not any(
            isinstance(sub, ast.Constant) and sub.value is None
            for sub in ast.walk(kw.value)
        )
    return False


def _snippet(node: ast.AST) -> str:
    return ast.unparse(node).replace("\n", " ")[:90]


def _promotion_calls(tree: ast.AST) -> list[ast.Call]:
    """Calls in *tree* under every name that reaches the function.

    Resolved once per module through :func:`~tests.ast_rules.construction_names`
    so an aliased import or a local rebinding is a call to it too. The
    definition is an ``ast.FunctionDef``, not a call, and is never reported.
    """
    return calls_to_any(construction_names(_SUBJECT, tree), tree)


def unlogged_promotions(root: Path | None = None) -> list[str]:
    """Calls under *root* that leave the re-run skip unable to check its event."""
    found: list[str] = []
    for py_file, tree in iter_modules(root if root is not None else _src_root()):
        found.extend(
            f"{py_file.name}:{node.lineno}: {_snippet(node)}"
            for node in _promotion_calls(tree)
            if not _log_is_wired(node)
        )
    return found


def bare_references(root: Path | None = None) -> list[str]:
    """Every use of the function's name that is not the target of a call.

    :func:`~tests.ast_rules.construction_names` follows an alias, a rebinding
    and a same-module subclass, but not ``functools.partial(f, ...)``, whose
    binding is a call, and a name handed on as a callback is called somewhere
    this scan never looks. Both appear in ``src/`` as a ``Name`` or
    ``Attribute`` that is not a call's ``func``. An import (an ``alias``, not a
    ``Name``), the definition and the ``__all__`` string are none of those, so
    the honest population is empty, and this is what closes the shapes the
    shared roster calls residue for this subject.
    """
    found: list[str] = []
    for py_file, tree in iter_modules(_src_root() if root is None else root):
        call_targets = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        found.extend(
            f"{py_file.name}:{node.lineno}: {_snippet(node)}"
            for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute))
            and name_of(node) == _SUBJECT
            and id(node) not in call_targets
        )
    return found


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_every_promotion_call_in_src_passes_the_event_log() -> None:
    violations = unlogged_promotions()
    assert not violations, (
        "submit_learning_promotion called without event_log= cannot check "
        "the PRECEDENT_PROMOTED event, so it skips nothing and writes every "
        "row again on a re-run (#983): a second SCD-2 version and a second "
        "promote event, and the outcome still reads `promoted`. Pass "
        "event_log=registry.operational.event_log.\n  " + "\n  ".join(violations)
    )


def test_nothing_in_src_hands_the_function_on_unreadably() -> None:
    references = bare_references()
    assert not references, (
        "submit_learning_promotion is referenced somewhere other than a "
        "call, so a call to it may pass or omit event_log= where the scan "
        "cannot see. Call it directly, or widen unlogged_promotions to follow "
        "the reference and re-argue the rule.\n  " + "\n  ".join(references)
    )


# ---------------------------------------------------------------------------
# Vacuity guards
# ---------------------------------------------------------------------------


def _population() -> int:
    return sum(len(_promotion_calls(tree)) for _p, tree in iter_modules(_src_root()))


#: Hand-read off ``src/`` on 2026-10-27: ``trellis_cli/curate.py`` calls it
#: twice (``promote-learning`` and ``review-ready --approve``),
#: ``trellis_api/routes/admin.py`` once (``POST /learning/promotions``) and
#: ``trellis/learning/candidate_auto_promote.py`` once. The definition in
#: ``scoring.py`` is not a call. A number a person counted is the only floor a
#: silently-narrowing scan cannot also satisfy.
_PROMOTION_FLOOR = 4


def test_the_scan_finds_the_sites_it_is_meant_to_police() -> None:
    """A floor, not an equality: a new compliant call must not turn this red."""
    assert_hand_read_floor(
        _population(),
        _PROMOTION_FLOOR,
        subject=f"{_SUBJECT} call",
        hint=(
            "trellis_cli/curate.py (2), trellis_api/routes/admin.py and "
            "trellis/learning/candidate_auto_promote.py."
        ),
    )


def test_the_scan_sees_every_shape_in_the_shared_evasion_roster(
    tmp_path: Path,
) -> None:
    """The shared half of the guard, run through the shipped predicate.

    ``exempt`` is only the two **residue** shapes (no scanner in
    ``tests.ast_rules`` reports them). Neither can hide a call here, and the
    reason is the closing test, not a claim: ``bare_references`` finds no
    ``partial`` and no re-export, because it finds no use of the name that is
    not a call.
    """
    assert_scan_is_not_vacuous(
        lambda root: [int(v.split(":")[1]) for v in unlogged_promotions(root=root)],
        subject=_SUBJECT,
        kwarg=_LOG_KWARG,
        tmp_path=tmp_path,
        live_population=_population(),
        floor=_PROMOTION_FLOOR,
        exempt={
            "partial_binding": (
                "residue: the binding's value is a call, so there is no name "
                "for construction_names to resolve. Closed by "
                "bare_references(), which fails on any use of the function's "
                "name that is not a call, so a partial of it cannot be added"
            ),
            "cross_module_subclass": (
                "residue: a subclass defined in another module is not in this "
                "tree, and a function cannot be subclassed at all. The "
                "cross-module spelling that could exist is a re-export "
                "binding, which bare_references() also fails on"
            ),
        },
    )


#: This rule's own judgement, hand-read. A line ending ``# FLAG`` must be
#: reported and every other call line must not.
_CORPUS = """
from trellis import learning
from trellis.learning import submit_learning_promotion
from trellis.learning import submit_learning_promotion as _submit


def omitted(ex, p, e, g):
    return submit_learning_promotion(ex, p, e, graph_store=g)  # FLAG


def explicit_none(ex, p, e, g):
    return submit_learning_promotion(ex, p, e, graph_store=g, event_log=None)  # FLAG


def conditional_none(ex, p, e, g, log, flag):
    return submit_learning_promotion(  # FLAG
        ex, p, e, graph_store=g, event_log=log if flag else None
    )


def splat_beside_a_log(ex, p, e, g, log, **kw):
    return submit_learning_promotion(ex, p, e, event_log=log, **kw)  # FLAG


def qualified(ex, p, e, g):
    return learning.submit_learning_promotion(ex, p, e, graph_store=g)  # FLAG


def aliased(ex, p, e, g):
    return _submit(ex, p, e, graph_store=g)  # FLAG


def ok_inline(ex, p, e, g, reg):
    return submit_learning_promotion(
        ex, p, e, graph_store=g, event_log=reg.operational.event_log
    )


def ok_bound_name(ex, p, e, g, log):
    return submit_learning_promotion(ex, p, e, graph_store=g, event_log=log)


def not_a_call():
    return [submit_learning_promotion]  # REF
"""


def test_the_scan_catches_every_known_evasion(tmp_path: Path) -> None:
    """Mutation guard run through the shipped scanners, not a copy of them."""
    text = _CORPUS.lstrip("\n")
    (tmp_path / "corpus.py").write_text(text, encoding="utf-8")
    lines = text.splitlines()
    expected = [i for i, line in enumerate(lines, 1) if line.endswith("# FLAG")]
    refs = [i for i, line in enumerate(lines, 1) if line.endswith("# REF")]
    assert len(expected) == 6, "the corpus lost a flagged line"

    reported = sorted(int(v.split(":")[1]) for v in unlogged_promotions(root=tmp_path))
    assert reported == expected, (
        f"unlogged_promotions reported {reported}, expected {expected}; "
        f"missing={sorted(set(expected) - set(reported))} "
        f"spurious={sorted(set(reported) - set(expected))}"
    )

    found = sorted(int(v.split(":")[1]) for v in bare_references(root=tmp_path))
    assert found == refs, f"bare_references reported {found}, expected {refs}"


class TestTheRulePremiseStillHolds:
    """The rule is only worth enforcing while omission is dangerous."""

    def test_the_parameter_is_still_called_event_log_and_defaults_to_none(
        self,
    ) -> None:
        parameter = inspect.signature(submit_learning_promotion).parameters.get(
            _LOG_KWARG
        )
        assert parameter is not None, (
            f"submit_learning_promotion has no {_LOG_KWARG} parameter; the AST "
            f"rule is matching a keyword that no longer exists"
        )
        assert parameter.default is None, (
            "submit_learning_promotion now defaults event_log. If the default "
            "is a real log, omission is no longer dangerous and this rule "
            "should be re-argued rather than kept out of habit; if the "
            "parameter became required, the type checker has taken the rule's "
            "job and it can go."
        )
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
            "event_log is no longer keyword-only, so a caller can pass it "
            "positionally and this scan reads it as omitted"
        )
