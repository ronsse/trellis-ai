"""CI runs every store contract, or this file says in writing which it does not.

``tests/unit/stores/contracts/`` is where the cross-backend semantics live:
a contract suite is the authoritative spec for a store ABC, and a backend
is "conformant" exactly insofar as its contract has *run*. So a contract
file that no workflow executes is not a gap in coverage, it is a spec that
has never been checked — and this repo has shipped that state twice, both
times while a paragraph of prose claimed otherwise.

* **#345** — the pgvector *vector* contract had never executed anywhere.
  Its fixture called ``_conn`` as an attribute, which it stopped being when
  #84 pooled connections, so the single env combination that would have run
  it errored instead and nobody ran that combination.
* **#351** — the ArcadeDB *graph* contract, for the blessed graph substrate,
  had no service container in any workflow until #543 added one. ``CLAUDE.md``
  still said it ran "nowhere at all" four days after it started running.

Both were found by a person reading a workflow, which is the part that does
not scale. The durable half is a rule that **derives** the answer: parse the
``pytest`` invocations out of every workflow, compute what CI actually
executes, and require every file under ``contracts/`` to be either executed
or named in :data:`DELIBERATELY_UNWIRED` with a reason. A file in neither is
a failure.

**This rule deliberately asserts no roster.** #443 declared three control
keys against six real sites and passed; asserting today's fourteen contract
modules would reproduce exactly that failure one directory over. What is
pinned is the *relation* — executed, or declared — so adding a contract is
ordinary and adding an **unwired** one is not.

Three vacuity guards, because a scan that silently finds nothing satisfies
every assertion above it, and this repo has burned itself on each:

1. **A hand-read floor** on files and legs discovered
   (:func:`tests.ast_rules.assert_hand_read_floor`). #457's scanner dropped
   148 branches to 123 with all three of its own guards green, because every
   guard divided by the scan's own output.
2. **A synthetic tree through the shipped parser.** A miniature repo with
   four contract modules and three workflows, two of which leave a contract
   unwired in the two ways that have actually happened — a marker whose
   toggle nobody set (#351's shape) and a workflow that only runs *after*
   merge. The shipped functions run over it and must report both; four
   under-collecting mutants of those same functions must fail to.
3. **A cross-check by a second method.** The workflow scan reads YAML
   through ``yaml.compose``; a second, hand-rolled indentation sweep reads
   the same files as raw text. They are compared over **line numbers**, so a
   divergence names the line one of them missed rather than reporting a
   count that is merely different.

Residue, stated rather than left to be discovered. Neither workflow method
honours shell comments inside a ``run:`` block, so ``# tests/foo.py`` would
be read as a target by both — a shared blind spot, which is the one class a
cross-check cannot catch. And a block that runs ``pytest`` and *then*
another command naming ``tests/`` would over-report; no such block exists.
"""

from __future__ import annotations

import ast
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from tests.ast_rules import assert_hand_read_floor

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_ROOT = REPO_ROOT / ".github" / "workflows"
CONTRACTS_ROOT = REPO_ROOT / "tests" / "unit" / "stores" / "contracts"
CONFTEST = REPO_ROOT / "tests" / "conftest.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Hand-read at ``origin/main`` 1ef5c9c: ``ls tests/unit/stores/contracts/*.py``
#: is 21 — fourteen ``test_*.py`` modules, six shared suite modules and
#: ``__init__.py``. A floor, not an equality: a new backend contract is
#: ordinary and must not turn this suite red.
CONTRACT_FILE_FLOOR = 21

#: ``tests.yml``, ``publish.yml`` and ``live-infra.yml`` are the three
#: workflow steps that invoke ``pytest``; ``lint.yml`` runs ruff over
#: ``tests/`` and is correctly not one of them.
PYTEST_LEG_FLOOR = 3

#: ``tests/conftest.py``'s ``_INCLUDE_FLAGS`` declares six toggles.
MARKER_TOGGLE_FLOOR = 6

#: Contract files CI deliberately does not execute, mapped to why.
#:
#: Keys are paths relative to ``tests/unit/stores/contracts/``. Values are
#: prose, and are read by a person deciding whether the reason still holds —
#: so "not wired yet" is not one. Name the blocker and, where there is one,
#: the issue.
#:
#: **It is empty, and that is a claim about today rather than a placeholder.**
#: Every contract file under that directory is executed by a pull-request
#: workflow, which has only been true since #543 gave the ArcadeDB graph
#: contract a service container. The gap that remains is one directory over
#: and cannot be expressed here: ``ArcadeDBVectorStore`` has no
#: ``VectorStoreContractTests`` subclass at all (#579), so there is no file
#: to declare — an absent contract is invisible to a rule about contract
#: files, which is why #579 is tracked as an issue and not as an entry.
DELIBERATELY_UNWIRED: dict[str, str] = {}

#: Mirrors ``tests/ast_rules.py``'s thresholds for the same reason: a reason
#: shorter than a sentence is a label, and a label rots without saying so.
_MIN_REASON_CHARS = 20
_MIN_REASON_WORDS = 4


# ---------------------------------------------------------------------------
# What CI runs: the shipped workflow scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetSite:
    """One ``pytest`` path argument, and the workflow line it sits on.

    The line number is what makes the cross-check in this module able to
    name a site rather than report a count, so it is carried on the record
    rather than recomputed.
    """

    workflow: str
    lineno: int
    target: Path

    def describe(self) -> str:
        return f"{self.workflow}:{self.lineno} {self.target}"


@dataclass(frozen=True)
class PytestLeg:
    """One ``pytest`` invocation in one workflow step.

    ``included_markers`` is what this leg's environment opts back in from
    ``pyproject.toml``'s default deselection — the distinction that made
    #351 invisible, since ``live-infra.yml`` already *named* the contracts
    directory while the ArcadeDB toggle was absent, so the path selected a
    file every one of whose tests was then deselected.
    """

    workflow: str
    job: str
    style: str | None
    targets: tuple[Path, ...]
    included_markers: frozenset[str]
    pull_request: bool

    def selects(self, path: Path) -> bool:
        """Whether this leg names *path* (repo-root-relative) or a parent."""
        return any(target == path or target in path.parents for target in self.targets)


def _mapping_get(node: yaml.Node, key: str) -> yaml.Node | None:
    """Value node for *key*, reading key scalars literally.

    Reading the composed node tree rather than ``safe_load`` also sidesteps
    YAML 1.1's ``on:`` → ``True`` coercion, which every consumer of a GitHub
    workflow has to special-case exactly once.
    """
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value_node
    return None


def _plain(node: yaml.Node | None) -> object:
    """Composed node tree back to plain data (scalars stay strings)."""
    if isinstance(node, yaml.MappingNode):
        return {_plain(k): _plain(v) for k, v in node.value}
    if isinstance(node, yaml.SequenceNode):
        return [_plain(v) for v in node.value]
    if isinstance(node, yaml.ScalarNode):
        return node.value
    return None


def _scalar_env(node: yaml.Node | None) -> dict[str, str]:
    plain = _plain(node)
    if not isinstance(plain, dict):
        return {}
    return {str(key): str(value) for key, value in plain.items()}


def _run_target_sites(
    run_node: yaml.ScalarNode, workflow: str
) -> tuple[TargetSite, ...]:
    """``pytest`` path arguments inside one ``run:`` scalar, with line numbers.

    A literal block scalar (``|``) preserves the source's line structure
    one-for-one, so value line *i* is file line ``start_mark.line + 1 + i``;
    a plain single-line scalar sits on ``start_mark.line`` itself. A folded
    scalar would preserve neither, which is why
    :func:`test_every_pytest_leg_preserves_its_line_structure` refuses one
    rather than letting this arithmetic quietly mis-attribute a line.
    """
    offset = run_node.start_mark.line + (1 if run_node.style == "|" else 0)
    sites: list[TargetSite] = []
    seen_pytest = False
    for index, raw in enumerate(run_node.value.splitlines()):
        line = raw.rstrip().removesuffix("\\")
        try:
            words = shlex.split(line)
        except ValueError:
            words = line.split()
        for word in words:
            if word == "pytest":
                seen_pytest = True
                continue
            if not seen_pytest or word.startswith("-"):
                continue
            path = Path(word.rstrip("/"))
            if path.parts and path.parts[0] == "tests":
                sites.append(
                    TargetSite(
                        workflow=workflow,
                        lineno=offset + index + 1,
                        target=path,
                    )
                )
    return tuple(sites)


def _pytest_legs(root: Path, toggles: dict[str, frozenset[str]]) -> list[PytestLeg]:
    """Every workflow step that invokes ``pytest`` on a path under ``tests/``."""
    legs: list[PytestLeg] = []
    for path in sorted(root.glob("*.yml")):
        document = yaml.compose(path.read_text(encoding="utf-8"))
        if not isinstance(document, yaml.MappingNode):
            continue
        triggers = _plain(_mapping_get(document, "on")) or {}
        pull_request = "pull_request" in triggers
        jobs = _mapping_get(document, "jobs")
        if not isinstance(jobs, yaml.MappingNode):
            continue
        for job_key, job in jobs.value:
            steps = _mapping_get(job, "steps")
            if not isinstance(steps, yaml.SequenceNode):
                continue
            job_env = _scalar_env(_mapping_get(job, "env"))
            for step in steps.value:
                run_node = _mapping_get(step, "run")
                if not isinstance(run_node, yaml.ScalarNode):
                    continue
                sites = _run_target_sites(run_node, path.name)
                if not sites:
                    continue
                env = {**job_env, **_scalar_env(_mapping_get(step, "env"))}
                legs.append(
                    PytestLeg(
                        workflow=path.name,
                        job=str(job_key.value),
                        style=run_node.style,
                        targets=tuple(site.target for site in sites),
                        included_markers=_included_markers(env, toggles),
                        pull_request=pull_request,
                    )
                )
    return legs


def _target_sites(root: Path, toggles: dict[str, frozenset[str]]) -> set[TargetSite]:
    """Every ``pytest`` target site the YAML scan reaches, for the cross-check."""
    sites: set[TargetSite] = set()
    for path in sorted(root.glob("*.yml")):
        document = yaml.compose(path.read_text(encoding="utf-8"))
        if not isinstance(document, yaml.MappingNode):
            continue
        jobs = _mapping_get(document, "jobs")
        if not isinstance(jobs, yaml.MappingNode):
            continue
        for _job_key, job in jobs.value:
            steps = _mapping_get(job, "steps")
            if not isinstance(steps, yaml.SequenceNode):
                continue
            for step in steps.value:
                run_node = _mapping_get(step, "run")
                if isinstance(run_node, yaml.ScalarNode):
                    sites.update(_run_target_sites(run_node, path.name))
    del toggles
    return sites


# ---------------------------------------------------------------------------
# The second method: raw text, no YAML parser
# ---------------------------------------------------------------------------


def _target_sites_by_text(root: Path) -> set[TargetSite]:
    """The same sites, found by a hand-rolled indentation sweep.

    Deliberately shares no code with :func:`_target_sites`: it never parses
    YAML, and re-derives block scalars from indentation alone. A scan and a
    second expression of the same scan agree by construction; a scan and a
    different *method* do not, which is the only kind of cross-check worth
    having (#457 shipped three guards that all divided by the scan it was
    supposed to check).
    """
    sites: set[TargetSite] = set()
    for path in sorted(root.glob("*.yml")):
        lines = path.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            stripped = line.lstrip()
            if not stripped.startswith(("run:", "- run:")):
                index += 1
                continue
            key_indent = len(line) - len(stripped)
            block = [(index, stripped.split("run:", 1)[1])]
            cursor = index + 1
            while cursor < len(lines):
                following = lines[cursor]
                if following.strip() and (
                    len(following) - len(following.lstrip()) <= key_indent
                ):
                    break
                block.append((cursor, following))
                cursor += 1
            sites.update(_text_block_sites(block, path.name))
            index = cursor
    return sites


def _text_block_sites(block: list[tuple[int, str]], workflow: str) -> set[TargetSite]:
    seen_pytest = False
    found: set[TargetSite] = set()
    for lineno, raw in block:
        if raw.lstrip().startswith("#"):
            continue
        for raw_word in raw.rstrip().removesuffix("\\").split():
            word = raw_word.strip("\"'")
            if word == "pytest":
                seen_pytest = True
                continue
            if not seen_pytest or word.startswith("-"):
                continue
            if word.startswith("tests/") or word == "tests":
                found.add(
                    TargetSite(
                        workflow=workflow,
                        lineno=lineno + 1,
                        target=Path(word.rstrip("/")),
                    )
                )
    return found


def describe_divergence(
    yaml_sites: set[TargetSite], text_sites: set[TargetSite]
) -> str:
    """Line-for-line account of what one method saw and the other did not."""
    missing = sorted(yaml_sites - text_sites, key=lambda site: site.describe())
    extra = sorted(text_sites - yaml_sites, key=lambda site: site.describe())
    parts = []
    if missing:
        parts.append(
            "the text sweep missed:\n  "
            + "\n  ".join(site.describe() for site in missing)
        )
    if extra:
        parts.append(
            "the YAML scan missed:\n  " + "\n  ".join(site.describe() for site in extra)
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Markers: what the default deselection removes, and what a leg opts back in
# ---------------------------------------------------------------------------


def _default_excluded_markers(pyproject: Path = PYPROJECT) -> frozenset[str]:
    """Markers ``addopts`` deselects, read from ``pyproject.toml`` itself."""
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    words = addopts.replace('"', " ").split()
    return frozenset(
        words[index + 1] for index, word in enumerate(words[:-1]) if word == "not"
    )


def _marker_toggles(conftest: Path = CONFTEST) -> dict[str, frozenset[str]]:
    """``TRELLIS_TEST_*`` env var → markers, read from ``tests/conftest.py``.

    Derived rather than transcribed: ``_INCLUDE_FLAGS`` is the table pytest
    itself reads at ``pytest_configure``, so a seventh toggle added there is
    honoured here without an edit, and a renamed one fails loudly instead of
    silently making a leg look narrower than it is.
    """
    tree = ast.parse(conftest.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        named = any(
            isinstance(target, ast.Name) and target.id == "_INCLUDE_FLAGS"
            for target in targets
        )
        if not named or node.value is None:
            continue
        rows = ast.literal_eval(node.value)
        return {str(env): frozenset(markers) for _flag, env, markers in rows}
    return {}


def _included_markers(
    env: dict[str, str], toggles: dict[str, frozenset[str]]
) -> frozenset[str]:
    truthy = {"1", "true", "yes", "on"}
    return frozenset(
        marker
        for variable, markers in toggles.items()
        if env.get(variable, "").strip().lower() in truthy
        for marker in markers
    )


# ---------------------------------------------------------------------------
# What lives under contracts/
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectedNode:
    """A node pytest would collect, and the markers gating it.

    A contract subclass inherits its test methods from the suite base, so
    the ``Test*`` **class** is the node — its body is usually just a
    fixture. Method-level markers can only narrow a class further, never
    make an already-deselected class run, so they are not read here.
    """

    name: str
    lineno: int
    markers: frozenset[str]


@dataclass(frozen=True)
class ContractFile:
    """One file under ``contracts/``, classified by how it can run.

    ``kind`` is ``module`` for a file pytest collects, ``suite`` for a
    shared base class that only runs through a subclass, and ``package``
    for ``__init__.py``. The three are checked by different rules because
    they fail differently: a module goes unwired, a suite goes unsubclassed
    (#579's shape), and a package should hold nothing at all.
    """

    path: Path
    kind: str
    nodes: tuple[CollectedNode, ...]
    suites: frozenset[str]
    bases: frozenset[str]

    def describe(self) -> str:
        markers = sorted({m for node in self.nodes for m in node.markers})
        suffix = f" [{', '.join(markers)}]" if markers else ""
        return f"{self.path}{suffix}"


def _marker_names(node: ast.AST) -> set[str]:
    """``pytest.mark.<name>`` spellings inside a decorator or ``pytestmark``."""
    if isinstance(node, ast.Call):
        return _marker_names(node.func)
    if isinstance(node, (ast.List, ast.Tuple)):
        return {name for element in node.elts for name in _marker_names(element)}
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    ):
        return {node.attr}
    return set()


def _module_markers(tree: ast.Module) -> set[str]:
    markers: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if (
                any(
                    isinstance(target, ast.Name) and target.id == "pytestmark"
                    for target in targets
                )
                and statement.value is not None
            ):
                markers |= _marker_names(statement.value)
    return markers


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _contract_files(contracts_root: Path, repo_root: Path) -> list[ContractFile]:
    """Every ``.py`` under ``contracts/``, with its nodes, suites and bases."""
    files: list[ContractFile] = []
    for path in sorted(contracts_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_markers = _module_markers(tree)
        nodes: list[CollectedNode] = []
        suites: set[str] = set()
        bases: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, ast.ClassDef):
                if statement.name.startswith("Test"):
                    decorated = set(module_markers)
                    for decorator in statement.decorator_list:
                        decorated |= _marker_names(decorator)
                    nodes.append(
                        CollectedNode(
                            name=statement.name,
                            lineno=statement.lineno,
                            markers=frozenset(decorated),
                        )
                    )
                    bases |= {
                        name
                        for name in (_base_name(base) for base in statement.bases)
                        if name
                    }
                elif not statement.name.startswith("_"):
                    suites.add(statement.name)
            elif isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and statement.name.startswith("test"):
                decorated = set(module_markers)
                for decorator in statement.decorator_list:
                    decorated |= _marker_names(decorator)
                nodes.append(
                    CollectedNode(
                        name=statement.name,
                        lineno=statement.lineno,
                        markers=frozenset(decorated),
                    )
                )
        if path.name == "__init__.py":
            kind = "package"
        elif path.name.startswith("test_"):
            kind = "module"
        else:
            kind = "suite"
        files.append(
            ContractFile(
                path=path.relative_to(repo_root),
                kind=kind,
                nodes=tuple(nodes),
                suites=frozenset(suites),
                bases=frozenset(bases),
            )
        )
    return files


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _executes(leg: PytestLeg, file: ContractFile, excluded: frozenset[str]) -> bool:
    """Whether *leg* runs at least one node of *file*.

    Both halves are load-bearing and #351 is what proves it: the leg named
    the contracts directory (path ✓) while nothing set
    ``TRELLIS_TEST_ARCADEDB`` (markers ✗), so a path-only reading would have
    reported the blessed substrate's contract as covered for the whole time
    it ran nowhere.
    """
    if not leg.selects(file.path):
        return False
    return any(
        not ((node.markers & excluded) - leg.included_markers) for node in file.nodes
    )


def _executed_modules(
    files: list[ContractFile], legs: list[PytestLeg], excluded: frozenset[str]
) -> set[Path]:
    """Contract modules a *pull-request* leg runs.

    Pull-request legs only, and that is the merge gate rather than a
    preference: a workflow that runs after merge cannot keep a regression
    out, which is the whole argument #401 made when it widened
    ``live-infra.yml`` from push-to-main to ``pull_request``.
    """
    pr_legs = [leg for leg in legs if leg.pull_request]
    return {
        file.path
        for file in files
        if file.kind == "module"
        and any(_executes(leg, file, excluded) for leg in pr_legs)
    }


def _unwired_modules(
    files: list[ContractFile], legs: list[PytestLeg], excluded: frozenset[str]
) -> list[ContractFile]:
    executed = _executed_modules(files, legs, excluded)
    return [
        file for file in files if file.kind == "module" and file.path not in executed
    ]


def _unreached_suites(
    files: list[ContractFile], legs: list[PytestLeg], excluded: frozenset[str]
) -> list[ContractFile]:
    """Shared suites no executed module subclasses.

    A suite module defines the spec and collects nothing itself, so it runs
    exactly insofar as some executed subclass inherits it. This is #579's
    shape read from the other side: ``VectorStoreContractTests`` has two
    subclasses where the registry has three vector backends.
    """
    executed = _executed_modules(files, legs, excluded)
    inherited = {
        base
        for file in files
        if file.kind == "module" and file.path in executed
        for base in file.bases
    }
    return [
        file for file in files if file.kind == "suite" and not (file.suites & inherited)
    ]


def _reason_is_written(reason: str) -> bool:
    stripped = reason.strip()
    return (
        len(stripped) >= _MIN_REASON_CHARS
        and len(stripped.split()) >= _MIN_REASON_WORDS
    )


def _declared(file: ContractFile) -> bool:
    return file.path.name in DELIBERATELY_UNWIRED


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _live_inputs() -> tuple[list[ContractFile], list[PytestLeg], frozenset[str]]:
    toggles = _marker_toggles()
    return (
        _contract_files(CONTRACTS_ROOT, REPO_ROOT),
        _pytest_legs(WORKFLOWS_ROOT, toggles),
        _default_excluded_markers(),
    )


def test_every_contract_module_runs_in_ci_or_says_why_not() -> None:
    files, legs, excluded = _live_inputs()
    undeclared = [
        file for file in _unwired_modules(files, legs, excluded) if not _declared(file)
    ]
    assert not undeclared, (
        "These store contracts are executed by no pull-request workflow and "
        "are not declared in DELIBERATELY_UNWIRED, so their backend is "
        "'conformant' only in the sense that nothing has ever checked:\n  "
        + "\n  ".join(file.describe() for file in undeclared)
        + "\nWire them (a path in the workflow AND the marker toggle its "
        "tests carry — #351 had the first without the second), or declare "
        "them with a reason."
    )


def test_every_shared_contract_suite_has_an_executed_subclass() -> None:
    files, legs, excluded = _live_inputs()
    unreached = [
        file for file in _unreached_suites(files, legs, excluded) if not _declared(file)
    ]
    assert not unreached, (
        "These contract suites define a spec that no executed backend module "
        "subclasses, so the shared semantics they pin have never run:\n  "
        + "\n  ".join(file.describe() for file in unreached)
    )


def test_the_contracts_package_holds_no_hidden_tests() -> None:
    files, _legs, _excluded = _live_inputs()
    packages = [file for file in files if file.kind == "package"]
    assert packages, "contracts/ must remain a package"
    unpoliced = "This rule classifies __init__.py as plumbing and checks neither."
    for file in packages:
        assert not file.nodes, f"{file.path} carries test nodes. {unpoliced}"
        assert not file.suites, f"{file.path} defines a contract suite. {unpoliced}"


def test_deliberately_unwired_entries_are_current() -> None:
    files, legs, excluded = _live_inputs()
    by_name = {file.path.name: file for file in files}
    executed = _executed_modules(files, legs, excluded)
    reached = {file.path.name for file in files if file.kind == "suite"} - {
        file.path.name for file in _unreached_suites(files, legs, excluded)
    }
    for name, reason in DELIBERATELY_UNWIRED.items():
        assert name in by_name, (
            f"DELIBERATELY_UNWIRED names {name!r}, which is not a file under "
            "contracts/. A declaration that outlives its file is how a roster "
            "starts lying."
        )
        assert _reason_is_written(reason), (
            f"the reason for {name!r} is not prose. Name the blocker and the "
            "issue tracking it; 'not wired yet' restates the key."
        )
        file = by_name[name]
        still_unwired = (
            file.path not in executed if file.kind == "module" else name not in reached
        )
        assert still_unwired, (
            f"{name!r} is declared unwired but CI now runs it. #369 was "
            "closed on the strength of a contract having been *wired*, which "
            "is not the same as its having run — the inverse mistake is "
            "leaving the declaration behind. Delete the entry."
        )


# ---------------------------------------------------------------------------
# Guard 1 — hand-read floors
# ---------------------------------------------------------------------------


def test_the_scan_reaches_a_hand_read_number_of_contract_files() -> None:
    files, _legs, _excluded = _live_inputs()
    assert_hand_read_floor(
        len(files),
        CONTRACT_FILE_FLOOR,
        subject="Python file under tests/unit/stores/contracts/",
        hint="A narrowed rglob makes the unwired set shrink to nothing.",
    )


def test_the_scan_reaches_a_hand_read_number_of_pytest_legs() -> None:
    legs = _pytest_legs(WORKFLOWS_ROOT, _marker_toggles())
    assert_hand_read_floor(
        len(legs),
        PYTEST_LEG_FLOOR,
        subject="workflow step invoking pytest",
        hint="A scan finding no legs reports every contract as unwired, "
        "which fails loudly; one finding a leg too many reports the "
        "opposite, which does not.",
    )


def test_the_marker_toggle_table_is_the_one_conftest_ships() -> None:
    toggles = _marker_toggles()
    assert_hand_read_floor(
        len(toggles),
        MARKER_TOGGLE_FLOOR,
        subject="TRELLIS_TEST_* marker toggle in tests/conftest.py",
        hint="An empty table makes every marker look un-opted-in.",
    )
    excluded = _default_excluded_markers()
    reachable = {marker for markers in toggles.values() for marker in markers}
    assert excluded <= reachable, (
        "addopts deselects marker(s) "
        f"{sorted(excluded - reachable)} that no --include flag can opt back "
        "in, so no workflow can ever run a test carrying them."
    )


# ---------------------------------------------------------------------------
# Guard 2 — a synthetic tree through the shipped parser
# ---------------------------------------------------------------------------

_SUITE_SOURCE = '''\
"""Shared {kind} contract."""


class {name}ContractTests:
    def test_round_trip(self, store):
        assert store
'''

_MODULE_SOURCE = """\
import pytest

from tests.unit.stores.contracts.{suite} import {base}ContractTests

{pytestmark}

class Test{name}(  {base}ContractTests):
    @pytest.fixture
    def store(self):
        return object()
"""

_SYNTHETIC_WORKFLOWS = {
    # The broad pull-request sweep: names the directory, sets no toggle, so
    # it runs the unmarked modules and deselects every marked one.
    "tests.yml": """\
on:
  pull_request:
    branches: [main]
jobs:
  test:
    steps:
      - run: pip install -e ".[dev]"
      - run: pytest tests/unit/stores/contracts/ -v
""",
    # The backend leg: names one file and opts its marker back in.
    "live-infra.yml": """\
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
jobs:
  live-infra:
    steps:
      - name: Run contracts
        env:
          TRELLIS_TEST_POSTGRES: "1"
        run: |
          pytest \\
            tests/unit/stores/contracts/test_postgres_graph.py \\
            -v
""",
    # Runs the ArcadeDB-marked pair with the toggle set — but only after
    # merge, so it can gate nothing.
    "nightly.yml": """\
on:
  push:
    branches: [main]
jobs:
  nightly:
    steps:
      - env:
          TRELLIS_TEST_ARCADEDB: "1"
        run: pytest tests/unit/stores/contracts/test_pushonly_graph.py -v
""",
}


def _write_synthetic_repo(root: Path) -> tuple[Path, Path]:
    """A miniature repo whose two unwired shapes are the two that happened."""
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    for name, source in _SYNTHETIC_WORKFLOWS.items():
        (workflows / name).write_text(source, encoding="utf-8")

    contracts = root / "tests" / "unit" / "stores" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "__init__.py").write_text('"""Contracts."""\n', encoding="utf-8")
    (contracts / "graph_store_contract.py").write_text(
        _SUITE_SOURCE.format(kind="graph", name="Graph"), encoding="utf-8"
    )
    (contracts / "vector_store_contract.py").write_text(
        _SUITE_SOURCE.format(kind="vector", name="Vector"), encoding="utf-8"
    )
    modules = {
        # Runs: tests.yml names it and it carries no marker.
        "test_sqlite_graph.py": ("Graph", "SQLiteGraph", ""),
        # Runs: live-infra names the directory and sets the postgres toggle.
        "test_postgres_graph.py": (
            "Graph",
            "PostgresGraph",
            "pytestmark = [pytest.mark.postgres]\n",
        ),
        # #351's shape: selected by path, deselected by a marker no
        # pull-request leg toggles on, and named by no other leg at all.
        "test_arcadedb_graph.py": (
            "Graph",
            "ArcadeDBGraph",
            "pytestmark = [pytest.mark.arcadedb]\n",
        ),
        # The other shape: a leg does set its toggle and name it, but that
        # leg runs only after merge, so it can gate nothing.
        "test_pushonly_graph.py": (
            "Graph",
            "PushOnlyGraph",
            "pytestmark = [pytest.mark.arcadedb]\n",
        ),
    }
    for name, (base, cls, pytestmark) in modules.items():
        (contracts / name).write_text(
            _MODULE_SOURCE.format(
                suite="graph_store_contract",
                base=base,
                name=cls,
                pytestmark=pytestmark,
            ),
            encoding="utf-8",
        )
    return workflows, contracts


def _synthetic_state(
    root: Path,
) -> tuple[list[ContractFile], list[PytestLeg], frozenset[str]]:
    workflows, contracts = _write_synthetic_repo(root)
    return (
        _contract_files(contracts, root),
        _pytest_legs(workflows, _marker_toggles()),
        _default_excluded_markers(),
    )


def _names(files: list[ContractFile]) -> set[str]:
    return {file.path.name for file in files}


def test_the_shipped_scan_reports_both_unwired_shapes(tmp_path: Path) -> None:
    files, legs, excluded = _synthetic_state(tmp_path)
    assert _names(_unwired_modules(files, legs, excluded)) == {
        "test_arcadedb_graph.py",
        "test_pushonly_graph.py",
    }
    assert _names(_unreached_suites(files, legs, excluded)) == {
        "vector_store_contract.py"
    }


def test_a_declared_unwired_contract_is_accepted(tmp_path: Path) -> None:
    files, legs, excluded = _synthetic_state(tmp_path)
    declared = {
        "test_arcadedb_graph.py": "no service container for ArcadeDB yet, #351",
        "test_pushonly_graph.py": "runs after merge only, cannot gate a PR",
        "vector_store_contract.py": "no backend subclasses it on this tree",
    }
    undeclared = [
        file
        for file in _unwired_modules(files, legs, excluded)
        + _unreached_suites(files, legs, excluded)
        if file.path.name not in declared
    ]
    assert not undeclared
    assert all(_reason_is_written(reason) for reason in declared.values())


def test_the_synthetic_tree_defeats_undercollection_mutants(
    tmp_path: Path,
) -> None:
    files, legs, excluded = _synthetic_state(tmp_path)
    shipped = _names(_unwired_modules(files, legs, excluded))
    shipped_suites = _names(_unreached_suites(files, legs, excluded))

    pr_legs = [leg for leg in legs if leg.pull_request]
    marker_blind = {
        file.path.name
        for file in files
        if file.kind == "module" and not any(leg.selects(file.path) for leg in pr_legs)
    }
    trigger_blind = {
        file.path.name
        for file in files
        if file.kind == "module"
        and not any(_executes(leg, file, excluded) for leg in legs)
    }
    exact_path_only = {
        file.path.name
        for file in files
        if file.kind == "module"
        and not any(
            file.path in leg.targets
            and not ((node.markers & excluded) - leg.included_markers)
            for leg in pr_legs
            for node in file.nodes
        )
    }
    glob_blind = {
        file.path.name
        for file in files
        if file.kind == "suite" and file.path.name.startswith("test_")
    }

    mutants = {
        "marker-blind": (marker_blind, shipped),
        "trigger-blind": (trigger_blind, shipped),
        "exact-path-only": (exact_path_only, shipped),
        "test-glob-only-discovery": (glob_blind, shipped_suites),
    }
    for name, (mutated, reference) in mutants.items():
        assert mutated != reference, (
            f"the {name} mutant agrees with the shipped scan on the "
            "synthetic tree, so that tree no longer proves the shipped "
            "scan is reading what it claims to read"
        )


def test_every_pytest_leg_preserves_its_line_structure() -> None:
    legs = _pytest_legs(WORKFLOWS_ROOT, _marker_toggles())
    for leg in legs:
        assert leg.style in {None, "|"}, (
            f"{leg.workflow}:{leg.job} runs pytest from a {leg.style!r} "
            "scalar, whose lines do not map one-for-one onto the file's. "
            "The line attribution this rule's cross-check rests on would be "
            "silently wrong."
        )
        for target in leg.targets:
            assert "${{" not in str(target), (
                f"{leg.workflow}:{leg.job} interpolates a matrix value into "
                f"a pytest target ({target}); this rule compares literal "
                "paths and would silently match nothing."
            )


# ---------------------------------------------------------------------------
# Guard 3 — a second method, compared over line numbers
# ---------------------------------------------------------------------------


def test_the_yaml_scan_and_a_text_sweep_agree_line_for_line() -> None:
    toggles = _marker_toggles()
    yaml_sites = _target_sites(WORKFLOWS_ROOT, toggles)
    text_sites = _target_sites_by_text(WORKFLOWS_ROOT)
    assert_hand_read_floor(
        len(yaml_sites),
        PYTEST_LEG_FLOOR,
        subject="pytest target site reached by the YAML scan",
        hint="Two empty sets agree, which is why this floor precedes the comparison.",
    )
    assert yaml_sites == text_sites, describe_divergence(yaml_sites, text_sites)


def test_the_cross_check_names_the_line_an_undercollecting_scan_drops() -> None:
    """#457's guards stayed green while its scanner dropped 25 branches.

    A comparison that reports only *that* two methods disagree sends the
    next reader to diff two unordered sets by eye. This one is re-run
    against a deliberately under-collecting copy of the YAML scan and must
    name the dropped site.
    """
    toggles = _marker_toggles()
    yaml_sites = _target_sites(WORKFLOWS_ROOT, toggles)
    text_sites = _target_sites_by_text(WORKFLOWS_ROOT)
    dropped = min(yaml_sites, key=lambda site: site.describe())
    message = describe_divergence(yaml_sites - {dropped}, text_sites)
    assert dropped.describe() in message, (
        "the cross-check no longer names the site a narrowed scan missed, "
        f"only that one exists: {message}"
    )
