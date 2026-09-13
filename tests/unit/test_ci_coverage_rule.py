"""Every test file executes somewhere in CI, or is named here with a reason.

A test that runs nowhere is not coverage. It is a file that reads like
coverage, passes review like coverage, and reports nothing — the shape
[#345](https://github.com/ronsse/trellis-ai/issues/345) found when the
pgvector contract turned out never to have executed *anywhere*, and the
shape [#351](https://github.com/ronsse/trellis-ai/issues/351) found one
directory up. This rule derives the property rather than asserting
today's roster, because a declared roster of "what CI runs" is the thing
that rots: `tests/unit/test_postgres_live_infra_rule.py` carried an
exact-set assertion over `live-infra.yml`'s store targets, and the very
change that *fixed* a coverage hole broke it.

**Coverage is a join of two facts, and both halves have failed here.**
A file runs when some workflow leg's `pytest` invocation *selects its
path* **and** that leg's environment *includes the markers* its tests
carry. `live-infra.yml` names paths, not markers, so a path it never
names is dark no matter how many service containers the job starts; and
`tests.yml` selects all of `tests/` while including no marker at all, so
a marker-gated file is deselected there however broadly it is selected.
Reading either half alone gives the wrong answer — checking selection
alone calls every `live`-marked file covered by `tests.yml`, and
checking markers alone calls every unselected file covered by
`live-infra.yml`.

**What this found when it was first run.** Against `origin/main`, eleven
files were uncovered. Four of them — `test_arcadedb_graph.py`,
`test_arcadedb_vector.py`, `test_neo4j_graph.py` and
`test_neo4j_connectivity_live.py`, **68 tests** — were measured on
2026-09-12 to pass against containers started from `live-infra.yml`'s
own images, including the blessed ArcadeDB substrate's entire store
suite. They ran in no workflow because nothing named their paths. The
fix was to name them, not to declare them unwired: inventing a
justification for a file that passes is how the roster rots in the other
direction.

Three vacuity guards, because the rule is only as good as its scan.
A hand-read floor on every population it divides by; a synthetic tree
run through the *shipped* predicate, carrying the inherited-suite shape
that a naive `def test_` scan reports as zero nodes; and an independent
tokenizer scan of the same nodes, compared over line numbers so a
divergence names the line it missed. Each guard is itself proved by a
mutant: re-introducing the blind spot must make the guard fail.
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.ast_rules import assert_hand_read_floor
from tests.unit.test_importorskip_workflow_rule import (
    WorkflowLeg,
    _default_excluded_markers,
    _included_markers,
    _marker_names,
    _module_markers,
    _project_config,
    _workflow_legs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
CONFTEST = TESTS_ROOT / "conftest.py"
WORKFLOWS_ROOT = REPO_ROOT / ".github" / "workflows"

# Hand-read at 1ef5c9c: 390 files, 6533 collectable nodes, 6 legs across
# `live-infra.yml`, `tests.yml` and `publish.yml`, 7 gating markers.
# Floors sit below those so that adding or removing a test file stays an
# ordinary change; what must turn the suite red is a scan finding less
# than a person counted. The leg floor is deliberately above the four
# `tests.yml` supplies on its own, so losing `live-infra.yml` to a YAML
# shape this parser stops recognising fails here rather than silently
# re-classifying eight files as uncovered.
TEST_FILE_FLOOR = 350
TEST_NODE_FLOOR = 6000
WORKFLOW_LEG_FLOOR = 5
GATING_MARKER_FLOOR = 6

#: Files that run in no workflow leg, each with the measured reason.
#:
#: Asserted in **both** directions. A new dark file that is not named
#: here fails, and an entry whose file is now covered fails too — a
#: stale exemption is how a roster stops describing the tree it claims
#: to describe. Every reason below was measured on 2026-09-12 against
#: containers started from `live-infra.yml`'s own images, not inferred
#: from a docstring: the first draft of this map carried "needs a
#: managed Neon + AuraDB deployment" for the live integration suites,
#: copied from their module docstrings, and measuring refuted it for
#: every one of them.
DELIBERATELY_UNWIRED: dict[Path, str] = {
    Path("integration/api/test_smoke_parity.py"): (
        "the pytest mirror of deploy/smoke.sh — it probes an already "
        "deployed orchestrator at TRELLIS_BASE_URL and skips the whole "
        "module when nothing answers /healthz there. live-infra starts "
        "stores, not a server, so naming it would buy nine silent skips. "
        "Measured 2026-09-12 against a running instance: 8 of 9 pass, the "
        "ninth needs write credentials."
    ),
    Path("integration/cli/test_subprocess_serve.py"): (
        "needs no live infrastructure at all — measured 2026-09-12, it "
        "passes in 2.4s on a bare checkout. It is dark because it carries "
        "live + slow markers that tests.yml deselects, which is a marker "
        "defect rather than a CI capability gap; wiring it into live-infra "
        "would paper over that instead of fixing it."
    ),
    Path("unit/stores/test_neo4j_vector.py"): (
        "re-measured 2026-09-12 against neo4j:2025.12: 23 of 27 pass and "
        "the four TestQuery cases fail `Invalid input 'SEARCH'`, because "
        "the AuraDB-grade SEARCH ... IN (VECTOR INDEX ...) clause is "
        "absent from self-hosted Neo4j. Wiring it without a probe buys a "
        "red job; wiring it with tests/integration/conftest.py's existing "
        "probe (#356) buys four silently skipped tests wearing the "
        "appearance of coverage. It also provisions "
        "trellis_test_node_embeddings on the same (:Node, embedding) pair "
        "the live_api_server suites use, and Neo4j keeps one vector index "
        "per pair — measured here, running it first turned all 28 of "
        "those tests into a 30s VectorIndexNotOnlineError apiece."
    ),
}

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CollectedNode:
    """One node pytest would collect, with the markers deciding its fate."""

    path: Path
    name: str
    lineno: int
    markers: frozenset[str]
    kind: str

    def describe(self) -> str:
        marker_text = ",".join(sorted(self.markers)) or "unmarked"
        return f"{self.path}:{self.lineno}: {self.name} ({self.kind}; {marker_text})"


def _is_test_function(node: ast.AST) -> bool:
    """pytest's default ``python_functions = test*``, for either flavour.

    ``AsyncFunctionDef`` is a separate node type and is not a subclass of
    ``FunctionDef``; 351 of this tree's nodes are async, so a scan that
    checks only the sync type drops them and still looks plausible.
    """
    return isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) and node.name.startswith("test")


def _decorator_markers(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> set[str]:
    markers: set[str] = set()
    for decorator in node.decorator_list:
        markers |= _marker_names(decorator)
    return markers


def _class_test_nodes(
    cls: ast.ClassDef, inherited: frozenset[str], path: Path, prefix: str
) -> list[CollectedNode]:
    """Nodes a ``Test*`` class contributes, including the inherited case.

    **A contract file has no literal test nodes at all.** Every suite
    under ``tests/unit/stores/contracts/`` is a two-line subclass of a
    shared ABC — ``class TestArcadeDBGraph(GraphStoreContractTests)`` —
    so the ``def test_`` lines live in ``graph_store_contract.py`` and a
    scan matching only literal definitions computes **zero** nodes for
    each of the 14 of them. Zero nodes then reads as "nothing to cover",
    which is #488's failure exactly: a scan that stops matching satisfies
    every guard that divides by its own output.

    A class with bases and no test nodes of its own therefore counts as
    one inherited node carrying the class's own markers. That is a lower
    bound on what pytest collects (the ArcadeDB graph contract is 100
    tests, not 1), and a lower bound is the right side to err on — the
    question this rule asks is whether the file runs *at all*.
    """
    markers = frozenset(inherited | _decorator_markers(cls))
    own: list[CollectedNode] = []
    for statement in cls.body:
        if _is_test_function(statement):
            own.append(
                CollectedNode(
                    path=path,
                    name=f"{prefix}::{statement.name}",
                    lineno=statement.lineno,
                    markers=frozenset(markers | _decorator_markers(statement)),
                    kind="method",
                )
            )
        elif isinstance(statement, ast.ClassDef) and statement.name.startswith("Test"):
            own.extend(
                _class_test_nodes(
                    statement, markers, path, f"{prefix}::{statement.name}"
                )
            )
    if own:
        return own
    if cls.bases:
        return [
            CollectedNode(
                path=path,
                name=prefix,
                lineno=cls.lineno,
                markers=markers,
                kind="inherited",
            )
        ]
    return []


def _test_nodes(path: Path, relative_to: Path | None = None) -> list[CollectedNode]:
    """The shipped predicate: nodes pytest would collect from one file.

    Markers accumulate down the tree the way pytest resolves them —
    module ``pytestmark``, then class decorators, then the function's own
    — reusing ``_module_markers`` / ``_marker_names`` from
    ``test_importorskip_workflow_rule`` rather than re-deriving them, so
    the two rules cannot disagree about what a marker is. That shared
    reader is also what keeps ``pytest.param(..., marks=...)`` out: a
    per-parameter mark gates one case, not the node, and promoting it
    would report a whole file as deselected on the strength of one
    parametrised row.
    """
    root = relative_to or TESTS_ROOT
    relative = path.relative_to(root)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_markers = frozenset(_module_markers(tree))
    nodes: list[CollectedNode] = []
    for statement in tree.body:
        if _is_test_function(statement):
            nodes.append(
                CollectedNode(
                    path=relative,
                    name=statement.name,
                    lineno=statement.lineno,
                    markers=frozenset(module_markers | _decorator_markers(statement)),
                    kind="function",
                )
            )
        elif isinstance(statement, ast.ClassDef) and statement.name.startswith("Test"):
            nodes.extend(
                _class_test_nodes(statement, module_markers, relative, statement.name)
            )
    return nodes


def _test_files(root: Path = TESTS_ROOT) -> list[Path]:
    """Every file pytest's default ``python_files`` would collect from.

    ``*_test.py`` is the other default pattern and this tree holds none
    of it (checked, not assumed); it is matched anyway so a file added
    under that spelling cannot enter the tree unpoliced.
    """
    return sorted(
        path
        for pattern in ("test_*.py", "*_test.py")
        for path in root.rglob(pattern)
        if "__pycache__" not in path.parts
    )


def _gating_markers(config: dict[str, Any] | None = None) -> frozenset[str]:
    """Markers the default ``-m`` expression deselects.

    Intersecting with this set is load-bearing and was got wrong once:
    ``asyncio``, ``skipif`` and ``usefixtures`` are markers too, and
    treating every marker as gating reported **20** uncovered files
    instead of 11 — nine of them plain SQLite suites that run on every
    pull request.
    """
    return _default_excluded_markers(config or _project_config())


def _file_is_covered(
    path: Path,
    nodes: list[CollectedNode],
    legs: list[WorkflowLeg],
    gating: frozenset[str],
) -> bool:
    """Whether some leg both selects *path* and runs at least one of its nodes.

    A file counts as covered on **one** runnable node, not all of them.
    That is the weaker claim on purpose: this rule asks whether a file is
    dark, and `tests/unit/stores/test_neo4j_vector.py` — 23 of whose 27
    nodes pass against this workflow's own Neo4j image — is the standing
    reminder that "the file runs" and "the file is verified" are
    different questions. The second one belongs to the capability probe
    in [#356](https://github.com/ronsse/trellis-ai/issues/356), not here.
    """
    for leg in legs:
        if not leg.selects(path):
            continue
        for node in nodes:
            if not ((node.markers & gating) - leg.included_markers):
                return True
    return False


def _uncovered_files(
    root: Path = TESTS_ROOT,
    legs: list[WorkflowLeg] | None = None,
    gating: frozenset[str] | None = None,
) -> list[Path]:
    """Files no workflow leg runs, relative to ``tests/``.

    A file with no collectable nodes at all is reported as uncovered
    rather than skipped. There are none today, and that is the point:
    the alternative treats a scan that stopped finding nodes as good
    news.
    """
    resolved_legs = _workflow_legs() if legs is None else legs
    resolved_gating = _gating_markers() if gating is None else gating
    uncovered: list[Path] = []
    for path in _test_files(root):
        nodes = _test_nodes(path, relative_to=root)
        if not _file_is_covered(
            path.relative_to(root), nodes, resolved_legs, resolved_gating
        ):
            uncovered.append(path.relative_to(root))
    return uncovered


def _conftest_include_flags(path: Path = CONFTEST) -> dict[str, frozenset[str]]:
    """``{env var: markers}`` read out of ``tests/conftest.py`` by AST.

    The authoritative roster, parsed from the module that actually
    rewrites ``config.option.markexpr`` at collection time rather than
    from a copy of it. ``literal_eval`` on the assignment's value, so a
    roster built by a loop or a comprehension raises here instead of
    silently yielding an empty map that every downstream check would
    then agree with.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for statement in tree.body:
        target = None
        if isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            target = statement.target.id
        elif isinstance(statement, ast.Assign):
            names = [t for t in statement.targets if isinstance(t, ast.Name)]
            target = names[0].id if names else None
        if target != "_INCLUDE_FLAGS" or statement.value is None:
            continue
        rows = ast.literal_eval(statement.value)
        return {
            str(env_var): frozenset(str(marker) for marker in markers)
            for _flag, env_var, markers in rows
        }
    message = (
        f"{path} declares no module-level _INCLUDE_FLAGS. That tuple is the "
        "only thing translating a workflow's TRELLIS_TEST_* env into a "
        "relaxed -m expression; if it moved, every leg in this rule now "
        "reports zero included markers and every marked file reports as "
        "uncovered."
    )
    raise AssertionError(message)


def _declared_markers(config: dict[str, Any] | None = None) -> frozenset[str]:
    """Marker names from ``[tool.pytest.ini_options] markers``."""
    project = config or _project_config()
    declared = project["tool"]["pytest"]["ini_options"]["markers"]
    return frozenset(str(entry).split(":", 1)[0].strip() for entry in declared)


def _tokenized_test_lines(path: Path) -> set[int]:
    """Second method: lines carrying a collectable ``def test*``.

    Deliberately not an AST walk. A second expression of the same parse
    agrees with the first by construction; #464 shipped two "independent"
    counters that shared their file discovery, and #457's three guards all
    stayed green while the scanner they divided by dropped 148 branches to
    123. This one tracks ``class`` nesting by token column so that a
    ``def test_x`` inside a non-``Test`` helper class — which pytest does
    not collect — is excluded for a different reason than the AST scan
    excludes it, and compares over **line numbers** so a divergence names
    the site rather than a count.

    It covers the ``function`` and ``method`` kinds only. An inherited
    contract node has no ``def`` line anywhere in its file, so nothing a
    token stream can see would distinguish it; that shape is the
    synthetic tree's job.
    """
    reader = io.StringIO(path.read_text("utf-8")).readline
    tokens = list(tokenize.generate_tokens(reader))
    lines: set[int] = set()
    stack: list[tuple[int, bool]] = []
    for index, token in enumerate(tokens):
        if token.type != tokenize.NAME:
            continue
        if token.string == "class":
            following = tokens[index + 1]
            while stack and token.start[1] <= stack[-1][0]:
                stack.pop()
            stack.append((token.start[1], following.string.startswith("Test")))
            continue
        if token.string != "def":
            continue
        following = tokens[index + 1]
        if not following.string.startswith("test"):
            continue
        previous = tokens[index - 1] if index else token
        is_async = previous.type == tokenize.NAME and previous.string == "async"
        start = previous if is_async else token
        while stack and start.start[1] <= stack[-1][0]:
            stack.pop()
        if all(is_test_class for _column, is_test_class in stack):
            lines.add(start.start[0])
    return lines


def _written(reason: str) -> bool:
    """``tests.ast_rules``'s prose bar, applied to an unwired reason."""
    stripped = reason.strip()
    return len(stripped) >= 20 and len(stripped.split()) >= 4


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def test_every_test_file_runs_in_ci_or_is_named_with_a_reason() -> None:
    """No dark file goes unnamed, and no name outlives the darkness.

    Both directions, because each half fails differently and both have
    happened here. A dark file that nobody named is #345/#351 — coverage
    that was never coverage. A named file that is now covered is the
    roster rotting the other way, which is what
    ``test_postgres_live_infra_rule.py``'s exact-set assertion did when
    the fix for a real hole broke the test that was meant to protect it.
    """
    uncovered = set(_uncovered_files())
    named = set(DELIBERATELY_UNWIRED)

    unnamed = sorted(uncovered - named)
    assert not unnamed, (
        "these test files run in no workflow leg and are not named in "
        "DELIBERATELY_UNWIRED:\n  "
        + "\n  ".join(str(path) for path in unnamed)
        + "\n\nA file is covered when some leg's pytest invocation selects "
        "its path AND that leg's TRELLIS_TEST_* env includes every gating "
        "marker on at least one of its nodes. Measure the file against the "
        "service containers in .github/workflows/live-infra.yml before "
        "deciding: when it passes, name its path in that workflow's run "
        "block. Only add it here when running it would be dishonest, and "
        "write down what you measured."
    )

    stale = sorted(named - uncovered)
    assert not stale, (
        "these files are named in DELIBERATELY_UNWIRED but now run in CI:\n  "
        + "\n  ".join(str(path) for path in stale)
        + "\n\nDelete the entry. An exemption that no longer describes the "
        "tree is how the roster stops being readable as a statement about "
        "the tree."
    )


def test_every_unwired_reason_is_written() -> None:
    """An exemption costs a sentence, and the sentence must say something.

    The same prose floor ``tests.ast_rules`` applies to a sole-site
    reason, for the same reason: an exemption with ``"todo"`` next to it
    is an exemption nobody has to defend. The map's values are also
    required to name what was *measured* rather than what a docstring
    claims — that is not mechanically checkable, but the length bar at
    least makes the difference visible in review.
    """
    thin = sorted(
        str(path)
        for path, reason in DELIBERATELY_UNWIRED.items()
        if not _written(reason)
    )
    assert not thin, (
        f"DELIBERATELY_UNWIRED entries with no written reason: {thin}. "
        f"At least 20 characters and 4 words, saying what was measured "
        f"and why running the file anyway would be worse."
    )

    missing = sorted(
        str(path) for path in DELIBERATELY_UNWIRED if not (TESTS_ROOT / path).is_file()
    )
    assert not missing, (
        f"DELIBERATELY_UNWIRED names files that do not exist: {missing}. "
        f"A deleted file cannot be uncovered, so the entry is dead weight "
        f"that the both-directions check above cannot see."
    )


def test_the_population_the_rule_reasons_over_is_hand_read() -> None:
    """Four floors, because the rule divides by four different scans.

    Every other guard here is relative to one of these numbers, so a scan
    that merely *shrinks* satisfies all of them — #466's floor was
    ``len(SITES) > 0`` and three blind spots passed it. These are the one
    thing the scans cannot compute for themselves.
    """
    files = _test_files()
    assert_hand_read_floor(
        len(files),
        TEST_FILE_FLOOR,
        subject="test file",
        hint="tests/**/test_*.py; re-read the count before lowering this.",
    )

    nodes = [node for path in files for node in _test_nodes(path)]
    assert_hand_read_floor(
        len(nodes),
        TEST_NODE_FLOOR,
        subject="collectable test node",
        hint="a scan finding no nodes calls every file uncovered, loudly; "
        "a scan finding a third of them calls files covered on the wrong "
        "node's markers, silently.",
    )

    legs = _workflow_legs()
    assert_hand_read_floor(
        len(legs),
        WORKFLOW_LEG_FLOOR,
        subject="workflow test leg",
        hint="tests.yml supplies four on its own, so a floor at five is "
        "what notices live-infra.yml dropping out of the parse.",
    )

    assert_hand_read_floor(
        len(_gating_markers()),
        GATING_MARKER_FLOOR,
        subject="default-deselected marker",
        hint="derived from pyproject's addopts -m expression; an empty set "
        "makes every marker-gated file look runnable everywhere.",
    )


# --------------------------------------------------------------------------
# Guard 2 — a synthetic tree, run through the shipped predicate
# --------------------------------------------------------------------------

_SYNTHETIC_TREE: dict[str, str] = {
    "test_plain.py": """
def test_plain():
    assert True
""",
    "test_async.py": """
async def test_async_plain():
    assert True
""",
    "backends/test_module_marked.py": """
import pytest

pytestmark = pytest.mark.postgres


def test_needs_postgres():
    assert True
""",
    "backends/test_class_marked.py": """
import pytest


@pytest.mark.neo4j
class TestGroup:
    def test_one(self):
        assert True

    async def test_two(self):
        assert True


class Helper:
    def test_not_collected(self):
        assert True
""",
    "contracts/test_inherited.py": '''
import pytest

from shared import GraphStoreContractTests


@pytest.mark.arcadedb
class TestArcadeDBGraph(GraphStoreContractTests):
    """Every test node lives on the base class."""
''',
    "test_parametrised.py": """
import pytest


@pytest.mark.parametrize(
    "backend",
    [pytest.param("sqlite"), pytest.param("pg", marks=pytest.mark.postgres)],
)
def test_mixed(backend):
    assert backend
""",
    # Not matched by python_files, so its test must never be collected.
    "helpers.py": """
def test_decoy():
    raise AssertionError("this file is not a test module")
""",
}

#: ``(path, name, kind, sorted markers)`` for every node the shipped scan
#: must report over ``_SYNTHETIC_TREE``. An exact set, and legitimately a
#: roster: it describes a corpus written here rather than a tree that
#: changes underneath it, which is what makes it a control at all.
_SYNTHETIC_NODES = {
    ("test_plain.py", "test_plain", "function", ()),
    ("test_async.py", "test_async_plain", "function", ()),
    (
        "backends/test_module_marked.py",
        "test_needs_postgres",
        "function",
        ("postgres",),
    ),
    ("backends/test_class_marked.py", "TestGroup::test_one", "method", ("neo4j",)),
    ("backends/test_class_marked.py", "TestGroup::test_two", "method", ("neo4j",)),
    ("contracts/test_inherited.py", "TestArcadeDBGraph", "inherited", ("arcadedb",)),
    ("test_parametrised.py", "test_mixed", "function", ("parametrize",)),
}


def _write_synthetic_tree(root: Path) -> None:
    for name, source in _SYNTHETIC_TREE.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.lstrip("\n"), encoding="utf-8")


def _node_keys(nodes: list[CollectedNode]) -> set[tuple[object, ...]]:
    return {
        (node.path.as_posix(), node.name, node.kind, tuple(sorted(node.markers)))
        for node in nodes
    }


def _scan(root: Path) -> list[CollectedNode]:
    return [
        node
        for path in _test_files(root)
        for node in _test_nodes(path, relative_to=root)
    ]


def test_the_shipped_scan_reaches_the_synthetic_population(tmp_path: Path) -> None:
    """Five placements and two decoys, through the function the rule calls.

    Running a *copy* of the predicate here would leave the shipped one
    free to regress with the suite green — the reason
    ``assert_scan_is_not_vacuous`` takes the shipped callable rather than
    a re-implementation. ``helpers.py`` proves the population walk is
    doing the excluding, and ``Helper.test_not_collected`` proves the node
    walk is: both are ``def test_`` lines that pytest would not collect,
    and a scan reporting either is reporting things that do not run.
    """
    _write_synthetic_tree(tmp_path)
    assert _node_keys(_scan(tmp_path)) == _SYNTHETIC_NODES


def test_the_corpus_carries_every_shape_a_naive_scan_drops(tmp_path: Path) -> None:
    """Before asking whether the scan is right, ask whether this can tell.

    #488's three guards all passed while its scan matched only
    ``ast.Name``, because the synthetic tree carried nothing but spellings
    the scan already handled — every guard divided by the scan's own
    output, so a scan that merely *shrank* satisfied all of them. The
    exact-set assertion above has the same weakness on its own: it pins
    whatever this corpus happens to hold, and a corpus holding six plain
    ``def test_`` functions would pin six plain functions perfectly.

    So the corpus is asserted to contain each shape that has actually
    cost this repo a scan, by name. Deleting a file from
    ``_SYNTHETIC_TREE`` fails *here*, saying which blind spot went
    unguarded, instead of silently narrowing the exact set above.
    """
    _write_synthetic_tree(tmp_path)
    nodes = _scan(tmp_path)
    by_name = {node.name: node for node in nodes}

    assert any(node.kind == "inherited" for node in nodes), (
        "no contract-shaped file — a Test* class whose test nodes all live "
        "on an ABC base. The real tree has 14 of them and a literal "
        "`def test_` scan computes 0 nodes for each (#488's shape)."
    )
    assert "test_async_plain" in by_name, (
        "no async test — AsyncFunctionDef is not a subclass of "
        "FunctionDef, and 351 of this tree's nodes are async."
    )
    assert not any(node.name.startswith("Helper") for node in nodes), (
        "no non-Test class holding a `def test_` line; without one, a scan "
        "descending every class looks correct here."
    )
    assert not any(node.path.name == "helpers.py" for node in nodes), (
        "no non-test module holding a `def test_` line; without one, a "
        "population walk globbing '*.py' looks correct here."
    )
    assert "postgres" in by_name["test_needs_postgres"].markers, (
        "no module-level pytestmark — one of the three places a gating "
        "marker can be declared."
    )
    assert "neo4j" in by_name["TestGroup::test_one"].markers, (
        "no class-level marker — the placement that reaches a method "
        "through no decorator of its own."
    )


def _sync_only(node: ast.AST) -> bool:
    """``_is_test_function`` with ``AsyncFunctionDef`` dropped."""
    return isinstance(node, ast.FunctionDef) and node.name.startswith("test")


def test_a_sync_only_predicate_is_caught_by_this_corpus(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The mutant runs through the shipped call path, not a copy of it.

    ``_test_nodes`` and ``_class_test_nodes`` both dispatch on
    ``_is_test_function``, so patching that one name breaks the real scan
    the rule calls rather than a re-implementation written to be broken —
    which is the difference between proving the corpus separates a good
    scan from a bad one and proving it separates two things the test
    author wrote.
    """
    _write_synthetic_tree(tmp_path)
    # The module object itself, not a re-import of it: the mutant has to
    # land on the same `_is_test_function` the shipped `_scan` resolves.
    monkeypatch.setattr(sys.modules[__name__], "_is_test_function", _sync_only)
    mutated = _node_keys(_scan(tmp_path))

    assert mutated != _SYNTHETIC_NODES
    lost = _SYNTHETIC_NODES - mutated
    assert ("test_async.py", "test_async_plain", "function", ()) in lost
    async_method = (
        "backends/test_class_marked.py",
        "TestGroup::test_two",
        "method",
        ("neo4j",),
    )
    assert async_method in lost, (
        "an async *method* must be lost too; the class walk and the module "
        "walk are separate branches and both have to honour async."
    )


def test_a_per_parameter_mark_does_not_gate_the_whole_node(tmp_path: Path) -> None:
    """``pytest.param(..., marks=...)`` gates one case, not the file.

    Inherited from ``_marker_names``, and worth pinning from this side
    too: promoting a per-parameter mark would report ``test_mixed`` as
    ``postgres``-gated, which would make a file that runs on every pull
    request read as dark — and the fix someone would then reach for is to
    add it to the map.
    """
    _write_synthetic_tree(tmp_path)
    nodes = {node.name: node for node in _scan(tmp_path)}
    assert "postgres" not in nodes["test_mixed"].markers
    assert not (nodes["test_mixed"].markers & _gating_markers())


# --------------------------------------------------------------------------
# Guard 3 — a second method, compared over line numbers
# --------------------------------------------------------------------------


def _ast_test_lines(path: Path) -> set[int]:
    """The shipped scan's ``def``-backed nodes, as line numbers."""
    return {
        node.lineno for node in _test_nodes(path) if node.kind in {"function", "method"}
    }


def test_the_node_scan_agrees_with_an_independent_tokenizer_scan() -> None:
    """Two methods over the same tree, compared site by site.

    The floor says the population is large and the synthetic tree says
    the predicate handles the shapes that have bitten before. Neither
    catches a scan that is subtly wrong on the *real* tree in a shape
    nobody thought to synthesize — #457's three guards were all green
    while its scanner reported 123 of 148 branches, because every one of
    them divided by the scanner's own output.

    So the same question is asked a second way. Equality is asserted,
    not a tolerance: a tolerance is a place for a drift to live. The
    comparison is over line numbers rather than counts so a divergence
    names the file and the line it lost, which is the difference between
    a failure someone can act on and a number someone will re-baseline.
    """
    divergent: list[str] = []
    ast_total = 0
    token_total = 0
    for path in _test_files():
        ast_lines = _ast_test_lines(path)
        token_lines = _tokenized_test_lines(path)
        ast_total += len(ast_lines)
        token_total += len(token_lines)
        if ast_lines != token_lines:
            relative = path.relative_to(TESTS_ROOT)
            only_ast = sorted(ast_lines - token_lines)
            only_token = sorted(token_lines - ast_lines)
            divergent.append(
                f"{relative}: ast-only {only_ast}, token-only {only_token}"
            )

    assert not divergent, (
        "the AST scan and the tokenizer scan disagree about which lines "
        "carry a collectable test:\n  " + "\n  ".join(divergent)
    )
    assert ast_total == token_total
    assert ast_total >= TEST_NODE_FLOOR - 200, (
        f"both methods agree on only {ast_total} def-backed nodes, which is "
        f"far below the hand-read floor. Two scans agreeing on almost "
        f"nothing is the #464 shape, not a passing cross-check."
    )


def test_the_tokenizer_cross_check_names_a_dropped_line(monkeypatch: Any) -> None:
    """Proof the cross-check above can fail, on the real tree.

    #457's cross-check was itself validated this way: re-introduce the
    under-collection bug and require the guard to fail *naming the site*.
    A cross-check nobody has ever seen fail is a cross-check nobody has
    evidence about.
    """
    monkeypatch.setattr(sys.modules[__name__], "_is_test_function", _sync_only)

    dropped: list[tuple[Path, list[int]]] = []
    for path in _test_files():
        only_token = sorted(_tokenized_test_lines(path) - _ast_test_lines(path))
        if only_token:
            dropped.append((path.relative_to(TESTS_ROOT), only_token))

    assert dropped, (
        "dropping async test nodes from the scan produced no divergence at "
        "all, so the tokenizer scan is not independent of the AST scan — it "
        "is reading the same predicate by another route."
    )
    total = sum(len(lines) for _path, lines in dropped)
    assert total >= 100, (
        f"only {total} lines diverged; this tree has several hundred async "
        f"tests, so a cross-check that notices a handful is noticing "
        f"something other than the bug."
    )


# --------------------------------------------------------------------------
# The marker roster is declared four times, and all four have to agree
# --------------------------------------------------------------------------


def test_the_conftest_roster_and_the_rule_that_copies_it_agree() -> None:
    """C1 hand-copies ``_INCLUDE_FLAGS``; this is the join that pins it.

    ``_included_markers`` carries a literal ``names`` dict duplicating
    ``tests/conftest.py``. A copy is defensible — the rule must keep
    working if conftest is the thing that breaks — but an *unchecked*
    copy is how a rule ends up enforcing the world as it was when
    somebody typed it. Asserted in both directions: an env var conftest
    declares that the copy does not honour would make every leg under-
    report its included markers and every marked file read as dark,
    while one the copy honours that conftest does not would make a leg
    claim to run tests pytest deselects.
    """
    conftest_roster = _conftest_include_flags()

    for variable, markers in sorted(conftest_roster.items()):
        assert _included_markers({variable: "1"}) == markers, (
            f"tests/conftest.py maps {variable} to {sorted(markers)}, but "
            f"_included_markers in test_importorskip_workflow_rule.py maps "
            f"it to {sorted(_included_markers({variable: '1'}))}. That "
            f"module's `names` dict is a hand-copy of _INCLUDE_FLAGS and "
            f"has fallen behind it."
        )

    unknown = _included_markers(dict.fromkeys(conftest_roster, "0"))
    assert unknown == frozenset(), (
        "_included_markers returns markers for env vars that are all set "
        "to a falsy value, so the copy is not reading the values at all."
    )

    every_on = _included_markers(dict.fromkeys(conftest_roster, "1"))
    union = frozenset().union(*conftest_roster.values())
    assert every_on == union, (
        f"with every conftest flag on, the copy includes {sorted(every_on)} "
        f"against conftest's {sorted(union)}."
    )


def test_the_four_marker_rosters_agree() -> None:
    """addopts, pyproject markers, conftest flags, and C1's copy.

    A marker name lives in four places and nothing makes them agree.
    Each disagreement has its own failure mode, so they are asserted
    separately rather than as one set identity: a marker in addopts but
    not in `markers` deselects tests nothing documents, a marker in
    `markers` but not in addopts documents a gate that does not gate,
    and a marker in addopts with no conftest flag is a permanently dark
    gate no workflow can open — which is the one that matters most here,
    because this rule's whole coverage predicate reads
    ``markers & gating - leg.included_markers``.
    """
    config = _project_config()
    excluded = _default_excluded_markers(config)
    declared = _declared_markers(config)
    openable = frozenset().union(*_conftest_include_flags().values())

    assert excluded <= declared, (
        f"addopts deselects {sorted(excluded - declared)}, which "
        f"[tool.pytest.ini_options] markers does not declare. There is no "
        f"--strict-markers here, so an undeclared marker is a silent typo "
        f"that deselects nothing."
    )
    assert declared <= excluded, (
        f"{sorted(declared - excluded)} is declared as a marker but is not "
        f"in the addopts -m expression, so it gates nothing. If that is "
        f"deliberate, this rule's _gating_markers is the wrong reader for "
        f"it — see the asyncio/skipif/usefixtures case, which are markers "
        f"that correctly do not gate and are correctly absent from both."
    )
    assert excluded == openable, (
        f"addopts deselects {sorted(excluded)} but tests/conftest.py can "
        f"only re-include {sorted(openable)}. A marker in the first set "
        f"and not the second can never be run by any workflow, whatever "
        f"env it sets."
    )


# --------------------------------------------------------------------------
# Guard 4 — a positive control on the *other* half of the join
# --------------------------------------------------------------------------

_SYNTHETIC_WORKFLOW_UNMARKED = """
name: synthetic-unmarked
on:
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v
"""

_SYNTHETIC_WORKFLOW_POSTGRES = """
name: synthetic-postgres
on:
  pull_request:
    branches: [main]
jobs:
  live:
    runs-on: ubuntu-latest
    steps:
      - run: pip install -e ".[dev,cloud]"
      - name: run the postgres file only
        env:
          TRELLIS_TEST_POSTGRES: "1"
        run: pytest tests/backends/test_module_marked.py -v
"""


def _synthetic_legs(tmp_path: Path, *workflows: str) -> list[WorkflowLeg]:
    root = tmp_path / "workflows"
    root.mkdir(exist_ok=True)
    for index, source in enumerate(workflows):
        workflow = root / f"synthetic-{index}.yml"
        workflow.write_text(source.lstrip("\n"), encoding="utf-8")
    return _workflow_legs(root=root)


def test_coverage_is_computed_from_the_workflows_not_assumed(tmp_path: Path) -> None:
    """The leg half of the join has to be able to say *no*.

    Every guard above interrogates the node scan. None of them would
    notice if ``_file_is_covered`` were wired to ``True`` — the rule
    would pass on any tree, the map would empty itself, and the whole
    thing would report perfect coverage forever. That is the #466 shape
    (a floor of ``len(SITES) > 0``) moved one seam over.

    So the same corpus is run against workflows written here, where the
    right answer is known: no workflow covers nothing, an unmarked
    sweep covers only the unmarked files, and adding a leg that opens
    one marker moves exactly that file.
    """
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    _write_synthetic_tree(tests_root)
    gating = frozenset({"postgres", "neo4j", "arcadedb", "live", "slow"})
    every_file = {path.relative_to(tests_root) for path in _test_files(tests_root)}
    assert len(every_file) == 6, "the corpus lost a file; the arithmetic below is stale"

    none_covered = set(_uncovered_files(tests_root, [], gating))
    assert none_covered == every_file, (
        "with no workflow legs at all, every file must be uncovered. If it "
        "is not, coverage is not being computed from the workflows."
    )

    unmarked_only = set(
        _uncovered_files(
            tests_root, _synthetic_legs(tmp_path, _SYNTHETIC_WORKFLOW_UNMARKED), gating
        )
    )
    assert unmarked_only == {
        Path("backends/test_module_marked.py"),
        Path("backends/test_class_marked.py"),
        Path("contracts/test_inherited.py"),
    }, (
        "a leg that selects all of tests/ but opens no marker must cover "
        "the unmarked files and leave the marked ones dark. The "
        "parametrised file counts as covered: its postgres mark is on one "
        "pytest.param, so the node itself is not gated."
    )

    both = set(
        _uncovered_files(
            tmp_path / "tests",
            _synthetic_legs(
                tmp_path, _SYNTHETIC_WORKFLOW_UNMARKED, _SYNTHETIC_WORKFLOW_POSTGRES
            ),
            gating,
        )
    )
    assert both == unmarked_only - {Path("backends/test_module_marked.py")}, (
        "adding a leg that names one file and sets TRELLIS_TEST_POSTGRES=1 "
        "must move exactly that file and nothing else."
    )


def test_a_leg_that_names_a_path_does_not_cover_its_siblings(tmp_path: Path) -> None:
    """``selects`` is a prefix test, and a prefix is not a sibling.

    ``live-infra.yml`` names files, and this rule's entire value rests on
    naming a file being *narrower* than naming its directory. If
    ``WorkflowLeg.selects`` matched on the string rather than the path,
    ``tests/unit/stores/test_neo4j_graph.py`` would appear to cover
    ``test_neo4j_graph_extra.py``, and the four suites this workflow
    edit just wired in would have looked covered all along.
    """
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    _write_synthetic_tree(tests_root)
    (tests_root / "backends" / "test_module_marked_extra.py").write_text(
        "import pytest\n\npytestmark = pytest.mark.postgres\n\n\n"
        "def test_sibling():\n    assert True\n",
        encoding="utf-8",
    )

    legs = _synthetic_legs(tmp_path, _SYNTHETIC_WORKFLOW_POSTGRES)
    uncovered = set(_uncovered_files(tests_root, legs, frozenset({"postgres"})))
    assert Path("backends/test_module_marked.py") not in uncovered
    assert Path("backends/test_module_marked_extra.py") in uncovered, (
        "a leg naming test_module_marked.py must not cover a sibling whose "
        "name merely starts with it."
    )
