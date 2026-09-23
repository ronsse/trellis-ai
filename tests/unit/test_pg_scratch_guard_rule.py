"""Every state-wiping Postgres fixture goes through the scratch-database guard.

``tests/pg_scratch.py`` refuses to let a fixture ``TRUNCATE`` or ``DROP``
against a database that holds data. That is worth nothing if the *next*
Postgres suite reads ``os.environ["TRELLIS_TEST_PG_DSN"]`` for itself, which
is exactly what all nine existing ones did before this change and what an
author copying one of them would naturally write.

Two invariants, derived rather than listed, because a roster of destructive
modules is the shape this repo has watched rot four times (#443's three
control keys against six ``pop`` sites; #457, #464, #466 and #488 in
``tests/ast_rules.py``'s own docstring):

**R1 — the seam.** The literal ``TRELLIS_TEST_PG_DSN`` reaches
``os.environ`` in exactly one module. Every other consumer calls
:func:`tests.pg_scratch.configured_dsn` (reads, destroys nothing — safe for
a module-level ``skipif``) or :func:`tests.pg_scratch.scratch_dsn` (checked;
what a fixture about to write must call). A module that reads the env var
directly has routed around the guard, and R1 is what makes that a failing
test rather than a code-review catch.

**R2 — the destroyers.** Every function under ``tests/`` that runs
``TRUNCATE`` or ``DROP`` through a DB-API ``execute`` calls a guard
function. R2 covers the case R1 cannot: a DSN that arrives some other way —
``_live_wipe._truncate_postgres`` reaches it through ``store._dsn``, never
through the environment, and is the single widest-blast-radius call in the
suite.

Neither invariant is checked against a hand-written list of files. R2's
predicate is the scan, and the floor it must clear is hand-read off the
tree (:func:`assert_hand_read_floor`), because a floor the scan computes for
itself is #466 verbatim.

**Why ``execute``-scoped and not "any destructive-looking string".** A bare
regex over string literals matches this repo's prose — ``"Drop the memoized
write-provenance stamp"``, ``"Drop ``not <marker>`` clauses"`` — and, worse,
matches ``event_log_contract.py``'s SQL-*injection* probe, which passes
``"DROP TABLE events"`` to ``get_events(order=...)`` precisely to assert it
is **not** executed. Flagging that one would have taught the next author
that the rule cries wolf. Scoping to what reaches ``execute`` is what makes
the 9 hits exactly the 9 functions that destroy.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.ast_rules import (
    assert_hand_read_floor,
    construction_names,
    iter_modules,
    name_of,
)

TESTS = Path(__file__).resolve().parents[1]

#: The one module allowed to read the env var and to define the guard.
GUARD_MODULE = TESTS / "pg_scratch.py"

#: The env var the whole Postgres suite is keyed on.
DSN_ENV = "TRELLIS_TEST_PG_DSN"

#: Names that satisfy R2. ``scratch_dsn`` is the guarded handout;
#: ``require_scratch_database`` is the check on a DSN obtained elsewhere.
GUARD_CALLS = frozenset({"scratch_dsn", "require_scratch_database"})

#: A statement whose first SQL keyword destroys.
DESTRUCTIVE = re.compile(r"^\s*(TRUNCATE|DROP)\b", re.IGNORECASE)

#: DB-API cursor methods that run a statement.
EXECUTORS = frozenset({"execute", "executemany"})

#: Hand-read off the tree on 2026-09-23, by opening each file. Five
#: Postgres contract fixtures, ``test_pgvector.store``,
#: ``test_postgres_stores._clean_tables``, ``test_api_key_store._setup``
#: and ``_live_wipe._truncate_postgres``. A floor, not an equality: adding
#: a compliant destroyer is ordinary and must not turn this red. What must
#: turn it red is the scan finding fewer than a person counted.
DESTROYER_FLOOR = 9

#: Hand-read the same way: the eight modules that read the env var through
#: ``configured_dsn`` plus this rule. A scan that stops seeing the name
#: would otherwise satisfy R1 by finding nothing to complain about.
DSN_REFERENCE_FLOOR = 8


# ---------------------------------------------------------------------------
# Predicates — these are what ships, and what the vacuity checks run.
# ---------------------------------------------------------------------------


def _leading_literal(node: ast.AST) -> str | None:
    """The literal text a string expression starts with, or ``None``.

    An f-string counts by its first *constant* segment: ``f"TRUNCATE
    {', '.join(tables)}"`` is a TRUNCATE however the rest is built, which
    is the shape ``_live_wipe`` uses. A string that opens with the
    interpolation — ``f"{verb} TABLE x"`` — is deliberately not resolved:
    guessing at a value the AST does not carry is how a predicate starts
    over-collecting, and R1 already covers the DSN side of that module.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _executed_names(fn: ast.AST) -> set[str]:
    """Local names handed to an ``execute``-like call inside *fn*."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and name_of(node.func) in EXECUTORS:
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                if isinstance(arg, ast.Name):
                    names.add(arg.id)
    return names


def destructive_functions(tree: ast.AST) -> list[tuple[str, int]]:
    """Functions in *tree* that run destructive SQL. ``(name, lineno)``.

    Two routes to ``execute``, and both are needed: the literal passed
    straight in (the eight fixtures) and the literal bound to a local that
    is passed in (``_live_wipe``, which builds its statement first). One
    level of indirection is deliberate — a dataflow analysis deep enough to
    chase a statement through a helper is a different tool, and R1 is the
    invariant that covers a module clever enough to need one.
    """
    hits: list[tuple[str, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        executed = _executed_names(fn)
        destroys = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and name_of(node.func) in EXECUTORS:
                for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                    text = _leading_literal(arg)
                    if text and DESTRUCTIVE.match(text):
                        destroys = True
            elif isinstance(node, ast.Assign):
                text = _leading_literal(node.value)
                if (
                    text
                    and DESTRUCTIVE.match(text)
                    and any(
                        isinstance(t, ast.Name) and t.id in executed
                        for t in node.targets
                    )
                ):
                    destroys = True
        if destroys:
            hits.append((fn.name, fn.lineno))
    return hits


def calls_a_guard(fn: ast.AST) -> bool:
    """*fn* calls one of :data:`GUARD_CALLS`, under any import spelling."""
    return any(
        isinstance(node, ast.Call) and name_of(node.func) in GUARD_CALLS
        for node in ast.walk(fn)
    )


def _function_by_line(tree: ast.AST, lineno: int) -> ast.AST | None:
    for fn in ast.walk(tree):
        if (
            isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            and fn.lineno == lineno
        ):
            return fn
    return None


#: How the process environment is spelled. A closed stdlib surface, not a
#: roster of subjects: it does not grow as the test tree does, which is the
#: property that makes enumerating it safe where enumerating *modules* is
#: not. Aliases of each (``_ENV = os.environ``) are resolved.
ENVIRON_NAMES = frozenset({"environ", "environb", "getenv"})


def _dsn_name_aliases(tree: ast.AST) -> set[str]:
    """Names bound to the :data:`DSN_ENV` literal, plus the literal itself.

    ``pg_scratch`` itself reads ``os.environ.get(TEST_DSN_ENV, "")``, so a
    predicate that insisted on the literal *at* the access site would miss
    the one module it most needs to see — and would then pass vacuously.
    Iterated to a fixed point for the same reason
    :func:`construction_names` is, which cannot be reused here because it
    resolves names to a *name* and this resolves them to a string constant.
    """
    aliases = {DSN_ENV}
    changed = True
    while changed:
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = node.value
                if (isinstance(value, ast.Constant) and value.value in aliases) or (
                    isinstance(value, ast.Name) and value.id in aliases
                ):
                    found.update(t.id for t in node.targets if isinstance(t, ast.Name))
        changed = not found <= aliases
        aliases |= found
    return aliases


def env_reads_of_dsn(tree: ast.AST) -> list[int]:
    """Lines where *tree* pulls :data:`DSN_ENV` out of the process environment.

    Both halves are resolved rather than matched literally, and the *first*
    cut of this rule got the second half wrong in the direction that cost:
    it flagged any string constant equal to the name, which made
    ``test_postgres_live_infra_rule.py`` an offender for asserting that the
    **workflow** sets the variable — ``env["TRELLIS_TEST_PG_DSN"]`` over a
    dict parsed out of YAML. That is a correct mention and the opposite of
    a bypass. Exempting the file would have put a roster here on day one;
    asking *whose* mapping is being read is what actually separates the two.

    Reads the receiver, not the access shape, so ``os.environ[NAME]``,
    ``os.environ.get(NAME)``, ``os.getenv(NAME)`` and
    ``environ.get(NAME, "")`` are one rule rather than four.
    """
    environ_aliases: set[str] = set()
    for spelling in ENVIRON_NAMES:
        environ_aliases |= construction_names(spelling, tree)
    names = _dsn_name_aliases(tree)

    def _is_dsn(node: ast.AST) -> bool:
        return (isinstance(node, ast.Constant) and node.value in names) or (
            isinstance(node, ast.Name) and node.id in names
        )

    hits: list[int] = []
    for node in ast.walk(tree):
        # Subscript form, e.g. the mapping indexed by the name.
        if isinstance(node, ast.Subscript) and name_of(node.value) in environ_aliases:
            if _is_dsn(node.slice):
                hits.append(node.lineno)
        # Call forms: the getenv function, or the mapping's get method.
        elif isinstance(node, ast.Call):
            func = node.func
            receiver = (
                name_of(func.value)
                if isinstance(func, ast.Attribute) and func.attr == "get"
                else name_of(func)
            )
            if receiver in environ_aliases and any(_is_dsn(arg) for arg in node.args):
                hits.append(node.lineno)
    return sorted(hits)


# ---------------------------------------------------------------------------
# R1 — the seam
# ---------------------------------------------------------------------------


def test_only_the_guard_module_reads_the_dsn_env_var() -> None:
    """No test module goes around the guard to the environment."""
    offenders: dict[str, list[int]] = {}
    for path, tree in iter_modules(TESTS):
        if path in (GUARD_MODULE, Path(__file__).resolve()):
            continue
        lines = env_reads_of_dsn(tree)
        if lines:
            offenders[str(path.relative_to(TESTS))] = lines

    assert not offenders, (
        f"{DSN_ENV} is read outside tests/pg_scratch.py: {offenders}. "
        "The Postgres suites TRUNCATE and DROP whatever that DSN names, so "
        "the env var is handed out by the guard and nowhere else — call "
        "configured_dsn() for a skipif and scratch_dsn() for anything that "
        "writes."
    )


def test_the_guard_module_is_the_one_that_reads_it() -> None:
    """R1 is not satisfied by nobody using the variable at all.

    The assertion above passes trivially against a tree where the name has
    been renamed, misspelled or deleted — that is #466's failure with a
    different subject. This pins the other side: the guard module really
    does read it, and something really does call the guard.
    """
    guard_tree = ast.parse(GUARD_MODULE.read_text(encoding="utf-8"))
    assert env_reads_of_dsn(guard_tree), (
        f"tests/pg_scratch.py no longer mentions {DSN_ENV}; R1 above is now "
        "vacuous, since it can only report modules that use a name nothing "
        "defines."
    )

    users = [
        str(path.relative_to(TESTS))
        for path, tree in iter_modules(TESTS)
        if path != GUARD_MODULE
        and any(
            isinstance(node, ast.Call) and name_of(node.func) in GUARD_CALLS
            for node in ast.walk(tree)
        )
    ]
    assert_hand_read_floor(
        len(users),
        DSN_REFERENCE_FLOOR,
        subject="modules calling the scratch-database guard",
        hint=(
            "R1 reports modules that bypass the guard. If nothing calls the "
            "guard at all, R1 is green and the guard is dead code."
        ),
    )


# ---------------------------------------------------------------------------
# R2 — the destroyers
# ---------------------------------------------------------------------------


def test_every_destructive_function_calls_the_guard() -> None:
    offenders: dict[str, list[str]] = {}
    population = 0
    for path, tree in iter_modules(TESTS):
        if path == GUARD_MODULE:
            continue
        for fn_name, lineno in destructive_functions(tree):
            population += 1
            fn = _function_by_line(tree, lineno)
            if fn is None or not calls_a_guard(fn):
                offenders.setdefault(str(path.relative_to(TESTS)), []).append(
                    f"{fn_name}() @ {lineno}"
                )

    assert not offenders, (
        f"destructive SQL with no scratch-database check: {offenders}. "
        "Call require_scratch_database(dsn) before the statement runs, or "
        "take the DSN from scratch_dsn()."
    )
    assert_hand_read_floor(
        population,
        DESTROYER_FLOOR,
        subject="functions under tests/ that TRUNCATE or DROP",
        hint=(
            "The assertion above only reports what this scan finds, so a "
            "scan that stops matching reports nothing and passes. The floor "
            "was read off the tree by hand on 2026-09-23."
        ),
    )


# ---------------------------------------------------------------------------
# Both predicates, run against a subject built to defeat them
# ---------------------------------------------------------------------------
#
# Every assertion above divides by what these scans find, so a scan that
# quietly stops matching reports nothing and passes — #457, #464, #466 and
# #488 in four different rules. The floors are one half of the answer; this
# is the other. Each shape below is a way a real author would write the
# defect, and the negative controls are the two shapes that already tripped
# this rule during development.

_BYPASSES = {
    "bare_subscript": 'import os\nD = os.environ["TRELLIS_TEST_PG_DSN"]\n',
    "environ_get": 'import os\nD = os.environ.get("TRELLIS_TEST_PG_DSN", "")\n',
    "getenv": 'import os\nD = os.getenv("TRELLIS_TEST_PG_DSN")\n',
    "from_import": 'from os import environ\nD = environ.get("TRELLIS_TEST_PG_DSN")\n',
    "aliased_environ": 'import os\n_E = os.environ\nD = _E["TRELLIS_TEST_PG_DSN"]\n',
    "constant_alias": 'import os\nNAME = "TRELLIS_TEST_PG_DSN"\nD = os.getenv(NAME)\n',
    "alias_chain": ('import os\nA = "TRELLIS_TEST_PG_DSN"\nB = A\nD = os.environ[B]\n'),
}

_NOT_BYPASSES = {
    # The shape that made this rule fail on its first run: a workflow's
    # own env block, parsed out of YAML, asserted by the CI-coverage rule.
    "yaml_env_block": 'env = step["env"]\nassert env["TRELLIS_TEST_PG_DSN"]\n',
    # Prose. Every skipif reason in the suite names the variable.
    "docstring": '"""Skipped unless TRELLIS_TEST_PG_DSN is set."""\n',
    # A neighbouring variable this rule has no opinion about.
    "other_env_var": 'import os\nD = os.environ["TRELLIS_TEST_PGVECTOR"]\n',
}


def test_r1_predicate_catches_every_way_around_the_seam() -> None:
    missed = [
        name for name, src in _BYPASSES.items() if not env_reads_of_dsn(ast.parse(src))
    ]
    assert not missed, (
        f"env_reads_of_dsn walked past {missed}. R1 divides by this "
        "predicate, so a shape it cannot see is a module that can read the "
        "DSN directly with the rule still green."
    )


def test_r1_predicate_does_not_flag_legitimate_mentions() -> None:
    """The negative half, without which the positives are satisfiable by ``True``."""
    flagged = [
        name for name, src in _NOT_BYPASSES.items() if env_reads_of_dsn(ast.parse(src))
    ]
    assert not flagged, (
        f"env_reads_of_dsn flagged {flagged}, none of which reads the "
        "process environment. A rule that cries wolf gets an exemption "
        "list bolted to it, and the list is what rots."
    )


_DESTROYERS = {
    "direct_literal": (
        'def store():\n    cur.execute("TRUNCATE TABLE nodes, edges")\n'
    ),
    "assigned_literal": (
        "def wipe():\n"
        '    sql = f"TRUNCATE {tables} RESTART IDENTITY"\n'
        "    cur.execute(sql)\n"
    ),
    "drop_cascade": (
        'def _clean():\n    cur.execute("DROP TABLE IF EXISTS nodes CASCADE")\n'
    ),
    "executemany": ('def wipe():\n    cur.executemany("TRUNCATE TABLE nodes", rows)\n'),
    "lowercase": ("def store():\n    cur.execute('truncate table nodes')\n"),
}

_NOT_DESTROYERS = {
    # event_log_contract.py:510 — the SQL-injection probe, which passes
    # a destructive literal precisely to assert it is NOT executed.
    "injection_probe": (
        "def test_order_is_not_interpolated():\n"
        '    store.get_events(order="DROP TABLE events")\n'
    ),
    # Real docstrings in this tree: "Drop the memoized stamp", etc.
    "prose": 'def helper():\n    """Drop the memoized write-provenance stamp."""\n',
    # A SELECT is not a destroyer, however alarming the table name.
    "select": 'def probe():\n    cur.execute("SELECT * FROM nodes")\n',
}


def test_r2_predicate_catches_every_destructive_shape() -> None:
    missed = [
        name
        for name, src in _DESTROYERS.items()
        if not destructive_functions(ast.parse(src))
    ]
    assert not missed, (
        f"destructive_functions walked past {missed}. The floor below R2 "
        "is read off this scan's output, so a shape it misses is a fixture "
        "that can TRUNCATE production with the rule still green."
    )


def test_r2_predicate_does_not_flag_prose_or_probes() -> None:
    flagged = [
        name
        for name, src in _NOT_DESTROYERS.items()
        if destructive_functions(ast.parse(src))
    ]
    assert not flagged, f"destructive_functions over-collected: {flagged}"


def test_r2_reports_a_destroyer_that_skips_the_guard() -> None:
    """The end-to-end shape: destructive, and no guard call in sight."""
    tree = ast.parse(
        "def store():\n"
        '    s = PostgresGraphStore(dsn=os.environ["TRELLIS_TEST_PG_DSN"])\n'
        '    cur.execute("TRUNCATE TABLE nodes")\n'
    )
    (found,) = destructive_functions(tree)
    fn = _function_by_line(tree, found[1])
    assert fn is not None
    assert not calls_a_guard(fn)

    guarded = ast.parse(
        "def store():\n"
        "    s = PostgresGraphStore(dsn=scratch_dsn())\n"
        '    cur.execute("TRUNCATE TABLE nodes")\n'
    )
    (found,) = destructive_functions(guarded)
    fn = _function_by_line(guarded, found[1])
    assert fn is not None
    assert calls_a_guard(fn)
