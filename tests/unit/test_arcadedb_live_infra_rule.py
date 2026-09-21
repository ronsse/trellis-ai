"""The blessed ArcadeDB suites must execute in live-infra CI.

CI coverage is a **join**: a test runs only if the job selects its path
*and* sets every marker gate on it. ``live-infra.yml`` names paths, so
until #579 four ArcadeDB-marked files sat outside ``contracts/`` and
nothing selected them — they passed locally and had never been run by
CI. The roster below is therefore **derived by scanning for the
marker**, not hand-written: a hand-written roster rots in both
directions, and #443 shipped one declaring 3 keys against 6 real sites.
"""

import ast
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "live-infra.yml"
TESTS_ROOT = REPO_ROOT / "tests"
MARKER = "arcadedb"

# The scan must not be able to pass by finding nothing. This floor is
# below today's count (5) so ordinary additions do not trip it, and
# above zero so a scanner that breaks reports a failure instead of a
# clean sweep.
MIN_MARKED_FILES = 4


def _live_job() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["live-infra"]


def _test_step() -> dict[str, Any]:
    [step] = [
        step
        for step in _live_job()["steps"]
        if step.get("name") == "Run live + contract suites against the containers"
    ]
    return step


def _pytest_targets() -> list[Path]:
    command = _test_step()["run"].replace("\\\n", " ")
    return [Path(arg) for arg in shlex.split(command) if arg.startswith("tests/")]


def _selects(targets: Iterable[Path], path: Path) -> bool:
    """Would ``pytest <targets>`` collect ``path``?"""
    return any(target == path or target in path.parents for target in targets)


def _is_marker_node(node: ast.AST, marker: str) -> bool:
    """``pytest.mark.<marker>`` or a ``mark.<marker>`` imported off pytest.

    Both spellings count. A miss here means the rule silently under-
    enforces, which is the failure it exists to prevent, so it errs
    toward claiming too much rather than too little.
    """
    if not isinstance(node, ast.Attribute) or node.attr != marker:
        return False
    owner = node.value
    if isinstance(owner, ast.Attribute):
        return owner.attr == "mark" and isinstance(owner.value, ast.Name)
    return isinstance(owner, ast.Name) and owner.id == "mark"


def _source_is_marked(source: str, marker: str) -> bool:
    return any(_is_marker_node(node, marker) for node in ast.walk(ast.parse(source)))


def _marked_files(marker: str) -> list[Path]:
    """Every test file applying ``pytest.mark.<marker>``, repo-relative."""
    return [
        path.relative_to(REPO_ROOT)
        for path in sorted(TESTS_ROOT.rglob("test_*.py"))
        if _source_is_marked(path.read_text(encoding="utf-8"), marker)
    ]


def test_live_infra_provisions_arcadedb_with_bolt() -> None:
    service = _live_job()["services"]["arcadedb"]

    assert service["image"] == "arcadedata/arcadedb:26.8.1"
    assert set(service["ports"]) >= {"2480:2480", "17687:7687"}
    java_opts = service["env"]["JAVA_OPTS"]
    assert "-Darcadedb.server.rootPassword=playwithdata" in java_opts
    assert (
        "-Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin"
    ) in java_opts
    assert "/api/v1/ready" in service["options"]


def test_live_infra_sets_every_arcadedb_env_gate() -> None:
    """The other half of the join: selection alone collects nothing.

    ``tests/conftest.py`` deselects the marker unless this flag is set,
    so a path added to the job without these would be collected and
    then skipped.
    """
    env = _test_step()["env"]

    assert env["TRELLIS_TEST_ARCADEDB"] == "1"
    assert env["TRELLIS_TEST_ARCADEDB_URI"] == "bolt://localhost:17687"
    assert env["TRELLIS_TEST_ARCADEDB_HTTP_URL"] == "http://localhost:2480"
    assert env["TRELLIS_TEST_ARCADEDB_USER"] == "root"
    assert env["TRELLIS_TEST_ARCADEDB_PASSWORD"] == "playwithdata"  # noqa: S105
    assert env["TRELLIS_TEST_ARCADEDB_DATABASE"] == "trellis_test"


def test_every_arcadedb_marked_file_is_selected_by_live_infra() -> None:
    """The rule itself: derived roster in, selection out.

    Adding a file with the marker and not adding it here fails this.
    """
    targets = _pytest_targets()
    marked = _marked_files(MARKER)

    unselected = [path for path in marked if not _selects(targets, path)]

    assert unselected == [], (
        f"these carry pytest.mark.{MARKER} but no live-infra target "
        f"selects them, so they run nowhere: "
        f"{[str(p) for p in unselected]}"
    )


def test_the_marker_scan_is_not_vacuous() -> None:
    """A scan that finds nothing satisfies the rule above for free."""
    marked = _marked_files(MARKER)

    assert len(marked) >= MIN_MARKED_FILES, (
        f"expected at least {MIN_MARKED_FILES} files carrying "
        f"pytest.mark.{MARKER}, found {[str(p) for p in marked]} — the "
        f"scan is more likely broken than the marker genuinely removed"
    )
    assert (
        Path("tests/unit/stores/contracts/test_arcadedb_vector_contract.py") in marked
    )


def test_the_marker_scan_discriminates_marked_from_unmarked() -> None:
    """Run the shipped predicate over a synthetic tree, not today's repo.

    A floor proves the scan finds *something*; this proves it finds the
    right thing. Every case is fed to ``_source_is_marked`` itself.
    """
    mark_attr = "pytest.mark." + MARKER
    marked_sources = [
        f"import pytest\n\npytestmark = [{mark_attr}]\n",
        f"import pytest\n\n@{mark_attr}\ndef test_x():\n    pass\n",
        f"from pytest import mark\n\n@mark.{MARKER}\ndef test_x():\n    pass\n",
    ]
    unmarked_sources = [
        "import pytest\n\npytestmark = [pytest.mark.neo]\n",
        f"# {mark_attr} — mentioned in a comment only\nimport pytest\n",
        f'REASON = "{mark_attr}"\nimport pytest\n',
        f"import pytest\n\n@pytest.mark.{MARKER}x\ndef test_x():\n    pass\n",
    ]

    assert [_source_is_marked(s, MARKER) for s in marked_sources] == [
        True,
        True,
        True,
    ]
    assert [_source_is_marked(s, MARKER) for s in unmarked_sources] == [
        False,
        False,
        False,
        False,
    ]


def test_the_selection_predicate_discriminates() -> None:
    """Same proof for the other half: ``_selects`` over a synthetic tree."""
    targets = [Path("tests/unit/stores/contracts/"), Path("tests/unit/a/b.py")]

    assert _selects(targets, Path("tests/unit/stores/contracts/test_c.py"))
    assert _selects(targets, Path("tests/unit/a/b.py"))
    assert not _selects(targets, Path("tests/unit/stores/test_c.py"))
    assert not _selects(targets, Path("tests/unit/a/c.py"))
    assert not _selects(targets, Path("tests/unit/stores/contracts.py"))
