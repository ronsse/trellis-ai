"""Every ``importorskip`` is executable or honestly marker-deselected in CI.

An optional import can make a test look covered while pytest quietly skips it.
This rule joins four facts that have to agree: the import at each test node,
the node's effective markers, the paths selected by each pull-request workflow
leg, and the distributions supplied by the extras that leg installs.

The population floor is hand-read. The synthetic tree separately proves that
the shipped scan reaches module, function, class, alias, and nested-file
placements; no confidence check divides by the scan's own output.
"""

from __future__ import annotations

import ast
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tests.ast_rules import assert_hand_read_floor

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
WORKFLOWS_ROOT = REPO_ROOT / ".github" / "workflows"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Hand-read at origin/main e4e7604. The filed issue said 48; the AST corpus
# review and this independent recount both found 47.
IMPORTORSKIP_SITE_FLOOR = 47
TEST_MODULE_FLOOR = 400

# Distribution names cannot be derived mechanically from import names:
# psycopg-pool -> psycopg_pool and PyYAML -> yaml are both real cases here.
# Equality against the live scan below pins this map in both directions.
IMPORT_TO_DISTRIBUTION = {
    "anthropic": "anthropic",
    "neo4j": "neo4j",
    "numpy": "numpy",
    "openai": "openai",
    "pgvector": "pgvector",
    "prometheus_fastapi_instrumentator": "prometheus-fastapi-instrumentator",
    "psycopg": "psycopg",
    "psycopg_pool": "psycopg-pool",
    "yaml": "pyyaml",
}

_INSTALL_RE = re.compile(r"""pip\s+install\b[^\n]*?-e\s+["']?\.\[([^\]]+)]""")
_MATRIX_VALUE_RE = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*}}")
_DIST_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
_SELF_EXTRA_RE = re.compile(r"^\s*trellis-ai\[([^\]]+)]")
_NOT_MARKER_RE = re.compile(r"\bnot\s+([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class ImportOrSkipSite:
    path: Path
    lineno: int
    module: str | None
    owner: str
    markers: frozenset[str]

    def describe(self) -> str:
        module = self.module or "<dynamic module>"
        marker_text = ",".join(sorted(self.markers)) or "unmarked"
        return f"{self.path}:{self.lineno}: {module} ({self.owner}; {marker_text})"


@dataclass(frozen=True)
class WorkflowLeg:
    workflow: Path
    job: str
    extras: frozenset[str]
    distributions: frozenset[str]
    targets: tuple[Path, ...]
    included_markers: frozenset[str]
    pull_request: bool

    def selects(self, path: Path) -> bool:
        for target in self.targets:
            if target == Path("tests"):
                return True
            try:
                relative_target = target.relative_to("tests")
            except ValueError:
                continue
            if path.is_relative_to(relative_target):
                return True
        return False


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.parts
    )


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _importorskip_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    pytest_modules = {"pytest"}
    direct = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            pytest_modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "pytest"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            direct.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "importorskip"
            )

    changed = True
    while changed:
        before = set(direct)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            value = node.value
            is_ref = _name(value) in direct or (
                isinstance(value, ast.Attribute)
                and value.attr == "importorskip"
                and isinstance(value.value, ast.Name)
                and value.value.id in pytest_modules
            )
            if not is_ref:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            direct.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
        changed = direct != before
    return pytest_modules, direct


def _is_importorskip(node: ast.AST, pytest_modules: set[str], direct: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in direct
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "importorskip"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in pytest_modules
    )


def _marker_names(node: ast.AST) -> set[str]:
    return {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Attribute)
        and isinstance(child.value.value, ast.Name)
        and child.value.value.id == "pytest"
        and child.value.attr == "mark"
    }


def _module_markers(tree: ast.Module) -> set[str]:
    markers: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in targets
        ):
            continue
        markers |= _marker_names(statement.value)
    return markers


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {
        id(child): parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _owner_and_markers(
    call: ast.Call, tree: ast.Module, parents: dict[int, ast.AST]
) -> tuple[str, frozenset[str]]:
    markers = _module_markers(tree)
    classes: list[ast.ClassDef] = []
    function: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    current: ast.AST = call
    while id(current) in parents:
        current = parents[id(current)]
        if isinstance(current, ast.ClassDef):
            classes.append(current)
        elif function is None and isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            function = current
    for cls in classes:
        for decorator in cls.decorator_list:
            markers |= _marker_names(decorator)
    if function is None:
        return "module", frozenset(markers)
    for decorator in function.decorator_list:
        markers |= _marker_names(decorator)
    owner = (
        f"test:{function.name}"
        if function.name.startswith("test")
        else f"support:{function.name}"
    )
    return owner, frozenset(markers)


def _importorskip_sites(root: Path) -> list[ImportOrSkipSite]:
    sites: list[ImportOrSkipSite] = []
    for path in _python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        pytest_modules, direct = _importorskip_bindings(tree)
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not _is_importorskip(node, pytest_modules, direct):
                continue
            assert isinstance(node, ast.Call)
            module = (
                node.args[0].value
                if node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                else None
            )
            owner, markers = _owner_and_markers(node, tree, parents)
            sites.append(
                ImportOrSkipSite(
                    path=path.relative_to(root),
                    lineno=node.lineno,
                    module=module,
                    owner=owner,
                    markers=markers,
                )
            )
    return sorted(sites, key=lambda site: (str(site.path), site.lineno))


def _normalise_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _project_config(path: Path = PYPROJECT) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _declared_extras(config: dict[str, Any]) -> dict[str, list[str]]:
    return config["project"]["optional-dependencies"]


def _distributions_for_extras(
    extras: set[str], config: dict[str, Any]
) -> frozenset[str]:
    declared = _declared_extras(config)
    requirements = list(config["project"]["dependencies"])
    pending = list(extras)
    seen: set[str] = set()
    while pending:
        extra = pending.pop()
        if extra in seen:
            continue
        seen.add(extra)
        requirements.extend(declared.get(extra, ()))

    distributions: set[str] = set()
    for requirement in requirements:
        self_match = _SELF_EXTRA_RE.match(requirement)
        if self_match:
            pending = {
                extra.strip()
                for extra in self_match.group(1).split(",")
                if extra.strip()
            } - seen
            if pending:
                distributions |= set(_distributions_for_extras(pending, config))
            continue
        match = _DIST_RE.match(requirement)
        if match:
            distributions.add(_normalise_dist(match.group(1)))
    return frozenset(distributions)


def _pull_request_trigger(workflow: dict[Any, Any]) -> bool:
    trigger = workflow.get("on", workflow.get(True, {}))
    return isinstance(trigger, dict) and "pull_request" in trigger


def _matrix_rows(job: dict[str, Any]) -> list[dict[str, str]]:
    matrix = job.get("strategy", {}).get("matrix", {})
    include = matrix.get("include")
    if isinstance(include, list):
        return [
            {str(key): str(value) for key, value in row.items()}
            for row in include
            if isinstance(row, dict)
        ]
    return [{}]


def _expand_matrix(value: str, row: dict[str, str]) -> str:
    return _MATRIX_VALUE_RE.sub(lambda match: row.get(match.group(1), ""), value)


def _pytest_targets(command: str) -> tuple[Path, ...]:
    normalised = command.replace("\\\n", " ")
    tokens = shlex.split(normalised)
    try:
        pytest_at = tokens.index("pytest")
    except ValueError:
        return ()
    targets = []
    for token in tokens[pytest_at + 1 :]:
        if token.startswith("-"):
            continue
        path = Path(token.rstrip("/"))
        if path.parts and path.parts[0] == "tests":
            targets.append(path)
    return tuple(targets)


def _included_markers(env: dict[str, Any]) -> frozenset[str]:
    names = {
        "TRELLIS_TEST_LIVE": {"live"},
        "TRELLIS_TEST_SLOW": {"slow"},
        "TRELLIS_TEST_NEO": {"neo", "neo4j"},
        "TRELLIS_TEST_POSTGRES": {"postgres"},
        "TRELLIS_TEST_PGVECTOR": {"pgvector"},
        "TRELLIS_TEST_ARCADEDB": {"arcadedb"},
    }
    return frozenset(
        marker
        for variable, markers in names.items()
        if str(env.get(variable, "")).lower() in {"1", "true", "yes", "on"}
        for marker in markers
    )


def _workflow_legs(
    root: Path = WORKFLOWS_ROOT, config: dict[str, Any] | None = None
) -> list[WorkflowLeg]:
    project = config or _project_config()
    legs: list[WorkflowLeg] = []
    for path in sorted(root.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        pull_request = _pull_request_trigger(workflow)
        for job_name, job in workflow.get("jobs", {}).items():
            if not isinstance(job, dict):
                continue
            steps = [step for step in job.get("steps", ()) if isinstance(step, dict)]
            install_commands = [
                str(step["run"])
                for step in steps
                if "run" in step and "pip install" in str(step["run"])
            ]
            pytest_steps = [
                step
                for step in steps
                if "run" in step and _pytest_targets(str(step["run"]))
            ]
            if not install_commands or not pytest_steps:
                continue
            for row in _matrix_rows(job):
                extras: set[str] = set()
                for command in install_commands:
                    for match in _INSTALL_RE.finditer(_expand_matrix(command, row)):
                        extras.update(
                            value.strip()
                            for value in match.group(1).split(",")
                            if value.strip()
                        )
                distributions = _distributions_for_extras(extras, project)
                for step in pytest_steps:
                    env = {**job.get("env", {}), **step.get("env", {})}
                    legs.append(
                        WorkflowLeg(
                            workflow=path,
                            job=str(job_name),
                            extras=frozenset(extras),
                            distributions=distributions,
                            targets=_pytest_targets(str(step["run"])),
                            included_markers=_included_markers(env),
                            pull_request=pull_request,
                        )
                    )
    return legs


def _default_excluded_markers(config: dict[str, Any]) -> frozenset[str]:
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    return frozenset(_NOT_MARKER_RE.findall(addopts))


def _coverage_status(
    site: ImportOrSkipSite,
    leg: WorkflowLeg,
    excluded_markers: frozenset[str],
) -> str | None:
    if not leg.selects(site.path):
        return None
    inactive = (site.markers & excluded_markers) - leg.included_markers
    if site.owner != "module" and inactive:
        return "marker-deselected"
    if site.module is None:
        return None
    distribution = IMPORT_TO_DISTRIBUTION.get(site.module)
    if distribution is None or _normalise_dist(distribution) not in leg.distributions:
        return None
    return "executes"


def _uncovered_sites(
    sites: list[ImportOrSkipSite],
    legs: list[WorkflowLeg],
    excluded_markers: frozenset[str],
) -> list[ImportOrSkipSite]:
    pr_legs = [leg for leg in legs if leg.pull_request]
    return [
        site
        for site in sites
        if not any(_coverage_status(site, leg, excluded_markers) for leg in pr_legs)
    ]


def _unknown_extras(legs: list[WorkflowLeg], config: dict[str, Any]) -> list[str]:
    declared = set(_declared_extras(config))
    return sorted(
        {extra for leg in legs for extra in leg.extras if extra not in declared}
    )


def test_every_importorskip_site_is_covered_by_a_pull_request_leg() -> None:
    config = _project_config()
    sites = _importorskip_sites(TESTS_ROOT)
    assert_hand_read_floor(
        len(sites),
        IMPORTORSKIP_SITE_FLOOR,
        subject="pytest.importorskip call under tests/",
        hint="The AST recount at origin/main e4e7604 found 47, not the filed 48.",
    )
    assert {site.module for site in sites} == set(IMPORT_TO_DISTRIBUTION), (
        "The import-name to distribution-name map has drifted from the scan. "
        "Add or remove the mapping deliberately; do not let an unknown import "
        "silently disappear from dependency coverage."
    )
    uncovered = _uncovered_sites(
        sites, _workflow_legs(config=config), _default_excluded_markers(config)
    )
    assert not uncovered, (
        "These importorskip sites neither execute in a pull-request workflow "
        "that installs their provider nor sit on a test node that the workflow "
        "honestly marker-deselects:\n  "
        + "\n  ".join(site.describe() for site in uncovered)
    )


def test_pull_requests_have_a_full_all_extras_test_leg() -> None:
    legs = _workflow_legs()
    assert any(
        leg.pull_request
        and leg.workflow.name == "tests.yml"
        and "all" in leg.extras
        and Path("tests") in leg.targets
        for leg in legs
    ), "tests.yml must run all tests with the `all` extra on pull requests"


def test_every_workflow_install_names_declared_extras() -> None:
    config = _project_config()
    unknown = _unknown_extras(_workflow_legs(config=config), config)
    assert not unknown, (
        f"workflow install(s) request nonexistent extra(s) {unknown}; pip only "
        "warns and then runs with less coverage than the workflow claims"
    )


_SYNTHETIC_TESTS = {
    "test_module.py": """\
import pytest as pt

pt.importorskip("module_dep")

def test_plain():
    pt.importorskip("function_dep")
""",
    "nested/test_class.py": """\
from pytest import importorskip as optional
import pytest

@pytest.mark.postgres
class TestMarked:
    def test_marked(self):
        optional("class_dep")
""",
    "conftest.py": """\
import pytest

@pytest.fixture
def optional_fixture():
    gate = pytest.importorskip
    gate("fixture_dep")
""",
}

_SYNTHETIC_SITE_KEYS = {
    ("conftest.py", 6, "fixture_dep", "support:optional_fixture", frozenset()),
    (
        "nested/test_class.py",
        7,
        "class_dep",
        "test:test_marked",
        frozenset({"postgres"}),
    ),
    ("test_module.py", 3, "module_dep", "module", frozenset()),
    ("test_module.py", 6, "function_dep", "test:test_plain", frozenset()),
}


def _write_synthetic_tests(root: Path) -> None:
    for name, source in _SYNTHETIC_TESTS.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _site_keys(sites: list[ImportOrSkipSite]) -> set[tuple[object, ...]]:
    return {
        (str(site.path), site.lineno, site.module, site.owner, site.markers)
        for site in sites
    }


def test_the_shipped_scan_reaches_the_synthetic_population(tmp_path: Path) -> None:
    _write_synthetic_tests(tmp_path)
    assert _site_keys(_importorskip_sites(tmp_path)) == _SYNTHETIC_SITE_KEYS


def test_the_synthetic_population_defeats_undercollection_mutants(
    tmp_path: Path,
) -> None:
    _write_synthetic_tests(tmp_path)
    sites = _importorskip_sites(tmp_path)
    mutants = {
        "module-scope-only": [site for site in sites if site.owner == "module"],
        "pytest-spelling-only": [
            site for site in sites if site.module in {"module_dep", "function_dep"}
        ],
        "top-directory-only": [site for site in sites if len(site.path.parts) == 1],
        "test-functions-only": [
            site for site in sites if site.owner.startswith("test:")
        ],
    }
    for name, mutated in mutants.items():
        assert _site_keys(mutated) != _SYNTHETIC_SITE_KEYS, (
            f"{name} unexpectedly covers the synthetic corpus; the "
            "non-vacuity proof no longer distinguishes that undercollection"
        )


def test_marker_selection_is_decided_at_the_owning_test_node(
    tmp_path: Path,
) -> None:
    _write_synthetic_tests(tmp_path)
    sites = {site.module: site for site in _importorskip_sites(tmp_path)}
    leg = WorkflowLeg(
        workflow=Path("tests.yml"),
        job="test",
        extras=frozenset(),
        distributions=frozenset(),
        targets=(Path("tests"),),
        included_markers=frozenset(),
        pull_request=True,
    )
    excluded = frozenset({"postgres"})
    assert _coverage_status(sites["class_dep"], leg, excluded) == "marker-deselected"
    assert _coverage_status(sites["function_dep"], leg, excluded) is None


def test_named_workflow_paths_are_compared_relative_to_tests() -> None:
    leg = WorkflowLeg(
        workflow=Path("live-infra.yml"),
        job="live",
        extras=frozenset(),
        distributions=frozenset(),
        targets=(Path("tests/unit/stores/contracts"),),
        included_markers=frozenset(),
        pull_request=True,
    )
    assert leg.selects(Path("unit/stores/contracts/test_graph.py"))
    assert not leg.selects(Path("unit/stores/test_graph.py"))


def test_a_dev_only_pr_workflow_leaves_optional_sites_uncovered(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "tests.yml").write_text(
        """\
on:
  pull_request:
jobs:
  test:
    steps:
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v
""",
        encoding="utf-8",
    )
    config = _project_config()
    uncovered = _uncovered_sites(
        _importorskip_sites(TESTS_ROOT),
        _workflow_legs(workflows, config),
        _default_excluded_markers(config),
    )
    assert {"anthropic", "openai", "prometheus_fastapi_instrumentator"} <= {
        site.module for site in uncovered
    }


def test_the_scan_reaches_a_hand_read_number_of_test_modules() -> None:
    assert_hand_read_floor(
        len(_python_files(TESTS_ROOT)),
        TEST_MODULE_FLOOR,
        subject="Python test module reached by the importorskip scan",
        hint="A narrowed rglob makes both the site count and its own checks shrink.",
    )


def test_a_nonexistent_workflow_extra_is_rejected(tmp_path: Path) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "tests.yml").write_text(
        """\
on:
  pull_request:
jobs:
  test:
    steps:
      - run: pip install -e ".[dev,vectors]"
      - run: pytest tests/ -v
""",
        encoding="utf-8",
    )
    config = _project_config()
    assert _unknown_extras(_workflow_legs(workflows, config), config) == ["vectors"]
