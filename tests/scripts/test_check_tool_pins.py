"""Tests for ``scripts/check_tool_pins.py`` (#378).

The script replaces three comments that each asserted a synchronisation
that had silently stopped holding. A test suite for it therefore has to do
two things, and the second matters more than the first:

1. Prove it **passes** on the real repo — otherwise CI is red on main.
2. Prove it **fails** on each drift shape it exists to catch. A checker
   that cannot fail is the same defect one level up, and this repo has
   shipped that exact thing more than once (a fixture that could never
   run, a contract suite that never executed). Every negative case below
   is built by mutating a known-good tree, so a check that silently
   stopped inspecting something would show up here as a passing mutant.

The ``scripts/`` directory has no ``__init__.py``, so the module is loaded
via ``importlib.util`` — same pattern as ``test_audit_silent_fallbacks``.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_tool_pins.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_tool_pins", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        msg = f"could not load spec for {SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_tool_pins"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pins() -> ModuleType:
    return _load_module()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal copy of the real pin-bearing files, known-consistent.

    Copied from the repo rather than synthesised so a future restructure of
    any of these files breaks the fixture loudly instead of leaving the
    negative cases quietly testing a shape that no longer exists.
    """
    root = tmp_path / "repo"
    (root / ".github" / "workflows").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    shutil.copy(REPO_ROOT / ".pre-commit-config.yaml", root / ".pre-commit-config.yaml")
    shutil.copy(
        REPO_ROOT / ".github" / "workflows" / "lint.yml",
        root / ".github" / "workflows" / "lint.yml",
    )
    return root


# ---------------------------------------------------------------------------
# The real repo
# ---------------------------------------------------------------------------


def test_the_real_repo_is_consistent(pins: ModuleType) -> None:
    """The gate must be green on main — this is what CI runs."""
    assert pins.check() == []


def test_real_pyproject_pins_both_tools_exactly(pins: ModuleType) -> None:
    found = pins.pyproject_pins(REPO_ROOT / "pyproject.toml")
    assert set(found) == {"ruff", "mypy"}
    assert all(version and version[0].isdigit() for version in found.values())


def test_no_workflow_hardcodes_a_pin(pins: ModuleType) -> None:
    """Including comments — see the rule's docstring for why."""
    assert pins.hardcoded_workflow_pins(REPO_ROOT / ".github" / "workflows") == []


# ---------------------------------------------------------------------------
# Drift shapes it must catch
# ---------------------------------------------------------------------------


def test_fixture_repo_starts_clean(pins: ModuleType, repo: Path) -> None:
    """Guards every negative case below: they mutate from a passing state."""
    assert pins.check(repo) == []


def test_catches_a_stale_precommit_ruff_rev(pins: ModuleType, repo: Path) -> None:
    """The exact drift on main: Dependabot bumps [dev], `rev:` stays put."""
    config = repo / ".pre-commit-config.yaml"
    config.write_text(
        config.read_text().replace("rev: v", "rev: v9.9.9-", 1), encoding="utf-8"
    )
    problems = pins.check(repo)
    assert len(problems) == 1
    assert "ruff" in problems[0]
    assert "pyproject.toml is authoritative" in problems[0]


def test_catches_a_stale_precommit_mypy_rev(pins: ModuleType, repo: Path) -> None:
    config = repo / ".pre-commit-config.yaml"
    text = config.read_text()
    mypy_rev = text.split("mirrors-mypy")[1].split("rev:")[1].split("\n")[0].strip()
    config.write_text(text.replace(f"rev: {mypy_rev}", "rev: v0.0.1"), encoding="utf-8")
    problems = pins.check(repo)
    assert len(problems) == 1
    assert "mypy" in problems[0]


def test_catches_a_workflow_that_restates_the_pin(pins: ModuleType, repo: Path) -> None:
    """The `pip install "ruff==X"` shape lint.yml carried for months."""
    workflow = repo / ".github" / "workflows" / "lint.yml"
    workflow.write_text(
        workflow.read_text() + '\n      - run: pip install "ruff==0.1.0"\n',
        encoding="utf-8",
    )
    problems = pins.check(repo)
    assert len(problems) == 1
    assert "hardcodes ruff==0.1.0" in problems[0]
    assert "lint.yml" in problems[0]


def test_catches_a_restated_pin_in_any_workflow_not_just_lint(
    pins: ModuleType, repo: Path
) -> None:
    """publish.yml is the one whose drift went unnoticed for three months."""
    other = repo / ".github" / "workflows" / "publish.yml"
    other.write_text(
        "name: Publish\njobs:\n  t:\n    steps:\n"
        '      - run: pip install "mypy==1.0.0"\n',
        encoding="utf-8",
    )
    problems = pins.check(repo)
    assert len(problems) == 1
    assert "publish.yml" in problems[0]
    assert "mypy==1.0.0" in problems[0]


def test_catches_a_version_literal_hiding_in_a_comment(
    pins: ModuleType, repo: Path
) -> None:
    """Comments are not exempt — a stale literal in prose is the origin story."""
    workflow = repo / ".github" / "workflows" / "lint.yml"
    workflow.write_text(
        workflow.read_text() + "\n      # historically we used ruff==0.9.9 here\n",
        encoding="utf-8",
    )
    assert any("ruff==0.9.9" in p for p in pins.check(repo))


def test_reports_every_divergence_not_just_the_first(
    pins: ModuleType, repo: Path
) -> None:
    """One run should name the whole edit, not send you round the loop."""
    config = repo / ".pre-commit-config.yaml"
    config.write_text(
        config.read_text().replace("rev: v", "rev: v9.9.9-"), encoding="utf-8"
    )
    workflow = repo / ".github" / "workflows" / "lint.yml"
    workflow.write_text(
        workflow.read_text() + '\n      - run: pip install "ruff==0.1.0"\n',
        encoding="utf-8",
    )
    assert len(pins.check(repo)) == 3  # ruff rev, mypy rev, hardcoded pin


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_rejects_a_range_instead_of_an_exact_pin(pins: ModuleType, repo: Path) -> None:
    """`ruff>=0.16` makes "the pinned version" meaningless, so it raises.

    Degrading to "no pin found, nothing to check" would turn the gate into
    a no-op at exactly the moment someone loosened the thing it guards.
    """
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace('"ruff==', '"ruff>='), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="must pin ruff"):
        pins.check(repo)


def test_missing_precommit_entry_is_reported_not_skipped(
    pins: ModuleType, repo: Path
) -> None:
    config = repo / ".pre-commit-config.yaml"
    config.write_text(
        config.read_text().replace(
            "https://github.com/astral-sh/ruff-pre-commit", "https://example.invalid"
        ),
        encoding="utf-8",
    )
    problems = pins.check(repo)
    assert len(problems) == 1
    assert "no 'rev:' found for ruff" in problems[0]


def test_main_exits_zero_on_the_real_repo(pins: ModuleType) -> None:
    assert pins.main() == 0
