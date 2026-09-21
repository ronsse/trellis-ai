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


def _requested_by_sites(
    path: Path, tree: ast.Module
) -> tuple[dict[str, str], list[str]]:
    """``({identity: "path:line"}, [unreadable sites])`` for one module.

    Module-level constants are resolved, because **both** real worker
    identities are bound to one (``REQUESTED_BY`` / ``_REQUESTED_BY``) and
    a literal-only sweep of ``requested_by=`` finds neither. That is not
    hypothetical: it is the first version of this measurement, which
    enumerated all 22 ``cli:`` / ``api:`` / ``mcp:`` identities and missed
    the only two the rule is about.

    A site whose value is neither a literal nor a resolvable local
    constant is returned as *unreadable* rather than dropped. The scan
    cannot certify an identity it cannot read, and silently skipping one
    is under-collection wearing a clean result.
    """
    constants = {name: value for name, value, _ in _module_level_strings(tree)}
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
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.setdefault(value.value, where)
            elif isinstance(value, ast.Name) and value.id in constants:
                found.setdefault(constants[value.id], where)
            else:
                unreadable.append(f"{where}: requested_by={ast.unparse(value)}")
    return found, unreadable


def unattended_writer_identities(root: Path) -> tuple[dict[str, str], list[str]]:
    """Every ``requested_by=`` identity submitted from *root*."""
    found: dict[str, str] = {}
    unreadable: list[str] = []
    for path, tree in iter_modules(root):
        module_found, module_unreadable = _requested_by_sites(path, tree)
        for identity, where in module_found.items():
            found.setdefault(identity, where)
        unreadable.extend(module_unreadable)
    return found, unreadable


def worker_prefixed_identities(root: Path) -> dict[str, str]:
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
    found, _ = unattended_writer_identities(root)
    return {k: v for k, v in found.items() if k.startswith("worker:")}


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
    """The literals in ``immutable_core`` equal the ones the loops use.

    :data:`GOVERNING_KEYS` spells its four ids as literals rather than
    importing them, because ``trellis.mutate`` sits *below*
    ``trellis.learning`` in the import order and this module is not worth
    inverting that for. This is the other half of that trade: the
    duplication is checked by importing the real constants here, where a
    test may depend on anything.
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
    } == GOVERNING_KEYS


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
    discovered, unreadable = unattended_writer_identities(root / _WORKERS_ROOT)

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

    stale = set(UNATTENDED_WRITERS) - set(discovered)
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
    discovered = worker_prefixed_identities(_repo_root() / _SRC_ROOT)
    missing = set(discovered) - set(UNATTENDED_WRITERS)
    assert not missing, (
        "these worker identities submit commands from outside "
        f"{_WORKERS_ROOT} and are unbound: "
        + ", ".join(f"{k} ({discovered[k]})" for k in sorted(missing))
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
    writers, _ = unattended_writer_identities(root / _WORKERS_ROOT)
    assert_hand_read_floor(
        len(writers),
        _WRITER_FLOOR,
        subject="unattended-writer identity",
        hint="counted under src/trellis_workers on 2026-09-20.",
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


def _render(tmp_path: Path, name: str, tree: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "planted.py").write_text(tree.lstrip("\n"), encoding="utf-8")
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
