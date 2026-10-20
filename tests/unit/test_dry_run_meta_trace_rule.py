"""A ``--dry-run`` command states what its meta-trace does on a dry run.

``wrap_cli_meta_analysis`` enters :func:`trellis.meta.record_meta_analysis`,
which writes an Agent node, an Activity node and an edge to the live graph
**on entry**, before the wrapped command has looked at anything. A command
whose ``--dry-run`` promises to write nothing therefore wrote anyway unless
it told the wrapper so, and ``admin migrate-provenance`` shipped that way
(#922). Two more did, found only by reading the callers: ``admin
migrate-graph`` and ``analyze advisory-effectiveness``.

Most of the wrapped commands are read-only analyzers that record a meta-trace
on purpose, and have no dry run, so the rule does not touch them. The rule is
about the intersection: **a function that takes a ``dry_run`` parameter and
calls ``wrap_cli_meta_analysis`` must pass a ``dry_run=`` keyword** whose
value is either the parameter itself or the literal ``False``.

The literal ``False`` is the one sanctioned exception, and it is a *written*
one rather than an allow-list: ``worker curate --dry-run`` records each
stage's Activity on purpose (its help text, ``operations.md`` and
``test_dry_run_writes_no_finding_and_nothing_else`` all say so, #663), so its
stages pass ``dry_run=False`` and a comment beside it. A roster of exempt
function names would rot in both directions; a keyword at the call site is
where the next reader looks. What the rule refuses is the *silence* that
produced #922: the keyword missing, the dry-run decision folded into
``disabled=`` where nothing can see it, or a value that is neither.

``src/trellis_cli/`` only: that is where ``wrap_cli_meta_analysis`` lives and
where every ``--dry-run`` flag is declared.

Guarding against vacuity is the other half, in the shape the sibling rules
use (``tests/ast_rules.py``): two hand-read floors, so a scan that quietly
stops matching cannot satisfy the rule; and the shipped predicate is run over
a synthetic tree of deliberately defective subjects, which it must report,
beside compliant ones, which it must not.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from typing import NamedTuple

from tests.ast_rules import (
    assert_hand_read_floor,
    calls_to_any,
    construction_names,
    iter_modules,
)
from trellis_cli._meta_wiring import wrap_cli_meta_analysis

#: The helper whose call sites the rule polices.
_WRAPPER = "wrap_cli_meta_analysis"

#: The parameter that makes a function a dry-run command, and the keyword the
#: wrapper takes. One spelling on both sides, deliberately.
_DRY_RUN = "dry_run"

#: Hand-read: ``wrap_cli_meta_analysis(`` call sites under ``src/trellis_cli/``
#: at the commit this rule was written. 25 = 19 in ``analyze.py``, 2 in
#: ``admin.py``, 3 in ``worker.py`` and 1 in ``admin_migrate_provenance.py``.
#: A floor, not an equality: a new wrapped command is ordinary and must not
#: turn this red.
_WRAPPED_CALL_FLOOR = 25

#: Hand-read: those call sites that sit in a function taking ``dry_run``.
#: 6 = ``admin migrate-graph``, ``admin migrate-provenance``, ``analyze
#: advisory-effectiveness`` and the three ``worker curate`` stage functions.
#: This is the population the rule actually reasons over, so it is floored on
#: its own; the larger floor above guards file discovery.
_DRY_RUN_SITE_FLOOR = 6


class _Site(NamedTuple):
    """One ``wrap_cli_meta_analysis(...)`` call inside a ``dry_run`` function."""

    where: str
    function: str
    states_decision: bool


def _cli_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src" / "trellis_cli"
    assert root.is_dir(), f"src/trellis_cli not found at {root}"
    return root


def _takes_dry_run(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Does *function* declare a parameter called ``dry_run``, in any position?"""
    arguments = function.args
    return any(
        arg.arg == _DRY_RUN
        for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    )


def _states_decision(call: ast.Call) -> bool:
    """Does this call pass ``dry_run=dry_run`` or ``dry_run=False``?

    ``dry_run=False`` is the written "this command records on a dry run".
    Anything else is rejected: ``dry_run=True`` would silence a live run, and
    a ``**kwargs`` splat cannot be read, so it does not count as stating the
    decision (nothing under ``src/trellis_cli/`` uses one).
    """
    for keyword in call.keywords:
        if keyword.arg != _DRY_RUN:
            continue
        value = keyword.value
        if isinstance(value, ast.Name) and value.id == _DRY_RUN:
            return True
        if isinstance(value, ast.Constant) and value.value is False:
            return True
    return False


def dry_run_sites(root: Path) -> list[_Site]:
    """Every wrapper call under *root* that sits in a function taking ``dry_run``.

    ``ast.walk`` over each function reaches every scope inside it (a nested
    function, an ``except`` handler, a ``with`` body), and
    :func:`~tests.ast_rules.construction_names` resolves an aliased import, so
    ``from ... import wrap_cli_meta_analysis as meta`` does not walk past. A
    call inside a nested function is reported against the outermost function
    that takes ``dry_run`` too, which is right: the parameter is in scope for
    the closure. The ``(path, line)`` key de-duplicates a call that two
    nested functions both enclose.
    """
    seen: set[tuple[str, int]] = set()
    sites: list[_Site] = []
    for path, tree in iter_modules(root):
        names = construction_names(_WRAPPER, tree)
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _takes_dry_run(function):
                continue
            for call in calls_to_any(names, function):
                key = (str(path), call.lineno)
                if key in seen:
                    continue
                seen.add(key)
                sites.append(
                    _Site(
                        where=f"{path.relative_to(root)}:{call.lineno}",
                        function=function.name,
                        states_decision=_states_decision(call),
                    )
                )
    return sites


def silent_dry_run_sites(root: Path) -> list[_Site]:
    """The sites that do not state their dry-run decision: what the rule forbids."""
    return [site for site in dry_run_sites(root) if not site.states_decision]


def _wrapper_call_count(root: Path) -> int:
    """Every wrapper call under *root*, in a ``dry_run`` function or not."""
    total = 0
    for _path, tree in iter_modules(root):
        total += len(calls_to_any(construction_names(_WRAPPER, tree), tree))
    return total


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_every_dry_run_command_states_its_meta_trace_decision() -> None:
    silent = silent_dry_run_sites(_cli_root())
    assert not silent, (
        "a function that takes dry_run calls wrap_cli_meta_analysis without "
        "dry_run=dry_run, so entering the meta-trace recorder writes an Agent "
        "node, an Activity node and an edge on a dry run (#922):\n"
        + "\n".join(f"  {site.where}  in {site.function}()" for site in silent)
        + "\n\nPass dry_run=dry_run. A command that records on a dry run on "
        "purpose passes dry_run=False and says why beside it (worker curate, "
        "#663)."
    )


def test_the_scan_still_sees_the_tree_it_polices() -> None:
    root = _cli_root()
    assert_hand_read_floor(
        _wrapper_call_count(root),
        _WRAPPED_CALL_FLOOR,
        subject=f"{_WRAPPER} call",
        hint="File discovery has narrowed, or the wrapper was renamed.",
    )
    assert_hand_read_floor(
        len(dry_run_sites(root)),
        _DRY_RUN_SITE_FLOOR,
        subject=f"{_WRAPPER} call in a function taking {_DRY_RUN}",
        hint="Re-read the callers by hand before lowering this.",
    )


class TestTheRulePremiseStillHolds:
    """The rule is only worth having while the wrapper can honour the keyword."""

    def test_the_wrapper_takes_a_keyword_only_dry_run_defaulting_to_false(
        self,
    ) -> None:
        parameter = inspect.signature(wrap_cli_meta_analysis).parameters[_DRY_RUN]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is False


# ---------------------------------------------------------------------------
# A deliberately defective subject
#
# Every ``# BAD:`` line must be reported and no other line may be; every
# ``# OK:`` line must not be. The names are what a failure prints.
# ---------------------------------------------------------------------------

_SUBJECT = """
from trellis_cli import _meta_wiring
from trellis_cli._meta_wiring import wrap_cli_meta_analysis
from trellis_cli._meta_wiring import wrap_cli_meta_analysis as meta


def missing_keyword(dry_run, no_meta_trace):
    with wrap_cli_meta_analysis(disabled=no_meta_trace):  # BAD:missing_keyword
        pass


def folded_into_disabled(dry_run, no_meta_trace):
    with wrap_cli_meta_analysis(disabled=no_meta_trace or dry_run):  # BAD:folded
        pass


def wrong_value(dry_run, no_meta_trace):
    with wrap_cli_meta_analysis(dry_run=no_meta_trace):  # BAD:wrong_value
        pass


def always_true(dry_run):
    with wrap_cli_meta_analysis(dry_run=True):  # BAD:always_true
        pass


def splat_only(dry_run, **opts):
    with wrap_cli_meta_analysis(**opts):  # BAD:splat_only
        pass


def aliased_import(dry_run):
    with meta():  # BAD:aliased_import
        pass


def module_attribute(dry_run):
    with _meta_wiring.wrap_cli_meta_analysis():  # BAD:module_attribute
        pass


def keyword_only_param(*, dry_run):
    with wrap_cli_meta_analysis():  # BAD:keyword_only_param
        pass


def positional_only_param(dry_run, /):
    with wrap_cli_meta_analysis():  # BAD:positional_only_param
        pass


async def async_command(dry_run):
    with wrap_cli_meta_analysis():  # BAD:async_command
        pass


def nested_function(dry_run):
    def inner():
        with wrap_cli_meta_analysis():  # BAD:nested_function
            pass

    inner()


def inside_except(dry_run):
    try:
        pass
    except Exception:
        with wrap_cli_meta_analysis():  # BAD:inside_except
            pass


def passes_the_parameter(dry_run, no_meta_trace):
    with wrap_cli_meta_analysis(disabled=no_meta_trace, dry_run=dry_run):  # OK:passes
        pass


def records_on_purpose(dry_run):
    with wrap_cli_meta_analysis(dry_run=False):  # OK:records_on_purpose
        pass


def aliased_and_compliant(dry_run):
    with meta(dry_run=dry_run):  # OK:aliased_and_compliant
        pass


def read_only_analyzer(no_meta_trace):
    with wrap_cli_meta_analysis(disabled=no_meta_trace):  # OK:read_only_analyzer
        pass


def near_miss_parameter(dry_run_mode):
    with wrap_cli_meta_analysis():  # OK:near_miss_parameter
        pass
"""

_MARKED = re.compile(r"#\s*(BAD|OK):(\w+)\s*$")


def _marked_lines(source: str) -> tuple[dict[str, int], dict[str, int]]:
    bad: dict[str, int] = {}
    ok: dict[str, int] = {}
    for number, line in enumerate(source.splitlines(), start=1):
        match = _MARKED.search(line)
        if match:
            (bad if match.group(1) == "BAD" else ok)[match.group(2)] = number
    return bad, ok


class TestTheShippedPredicateAgainstADefectiveSubject:
    def _scan(self, tmp_path: Path) -> set[int]:
        (tmp_path / "subject.py").write_text(_SUBJECT, encoding="utf-8")
        return {
            int(site.where.rsplit(":", 1)[1]) for site in silent_dry_run_sites(tmp_path)
        }

    def test_every_defective_shape_is_reported(self, tmp_path: Path) -> None:
        bad, _ok = _marked_lines(_SUBJECT)
        reported = self._scan(tmp_path)
        missed = sorted(name for name, line in bad.items() if line not in reported)
        assert not missed, f"the shipped scan walked past {missed}"

    def test_no_compliant_shape_is_reported(self, tmp_path: Path) -> None:
        _bad, ok = _marked_lines(_SUBJECT)
        reported = self._scan(tmp_path)
        hit = sorted(name for name, line in ok.items() if line in reported)
        assert not hit, f"the shipped scan reported compliant {hit}"

    def test_nothing_unmarked_is_reported(self, tmp_path: Path) -> None:
        bad, ok = _marked_lines(_SUBJECT)
        reported = self._scan(tmp_path)
        assert reported <= set(bad.values()) | set(ok.values())

    def test_the_subject_is_not_trivially_small(self) -> None:
        # The subject itself is hand-written, so its size is a hand-read
        # number too: 12 defective and 5 compliant shapes.
        bad, ok = _marked_lines(_SUBJECT)
        assert len(bad) == 12
        assert len(ok) == 5
