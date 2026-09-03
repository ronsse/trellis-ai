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
import re
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
    """Argv is passed explicitly: bare `main()` would read pytest's own."""
    assert pins.main([]) == 0


# ---------------------------------------------------------------------------
# Environment parity (#398)
#
# Two rules govern everything below.
#
# 1. **Nothing here asserts anything about the ambient environment.** The suite
#    runs under the full 3.11/3.12/3.13 matrix and, in the lint job, with only
#    ruff installed — so a test that read `sys.version_info` or the real PATH
#    would be red in CI for correct reasons. Every drift case injects its
#    environment instead.
# 2. **Every negative case mutates from a state proved passing**, the same
#    discipline as the pin cases above: a checker that stopped inspecting
#    something would surface here as a passing mutant, not as silence.
# ---------------------------------------------------------------------------


CLEAN_INSTALL = {"ruff": "0.16.5", "mypy": "2.3.1"}


@pytest.fixture
def gate_python(pins: ModuleType) -> tuple[int, int]:
    """The version the repo declares its gates run on, read not assumed."""
    return pins.gate_python_version(REPO_ROOT / "pyproject.toml")


@pytest.fixture
def matching_env(pins: ModuleType) -> dict[str, str]:
    """Installed versions that agree with the real pins."""
    return dict(pins.pyproject_pins(REPO_ROOT / "pyproject.toml"))


def _errors(findings: list) -> list[str]:
    return [f.message for f in findings if f.severity == "error"]


def _notes(findings: list) -> list[str]:
    return [f.message for f in findings if f.severity == "note"]


# --- the repo's own declarations -------------------------------------------


def test_gate_python_version_is_derived_not_written_down(
    pins: ModuleType, gate_python: tuple[int, int]
) -> None:
    """It comes out of requires-python — there is no constant to go stale."""
    targets = pins.declared_python_targets(REPO_ROOT / "pyproject.toml")
    assert targets["[project] requires-python"] == gate_python
    assert set(targets.values()) == {gate_python}, targets


def test_every_gate_workflow_sets_up_the_gate_python(
    pins: ModuleType, gate_python: tuple[int, int]
) -> None:
    """The claim the environment check rests on, checked against CI itself."""
    workflows = REPO_ROOT / ".github" / "workflows"
    gate_workflows = pins.workflows_running_gates(workflows)
    assert gate_workflows, "no workflow appears to run ruff or mypy at all"
    setups = pins.workflow_python_setups(workflows)
    for path in gate_workflows:
        assert gate_python in setups.get(path, set()), path.name


def test_tools_roster_covers_every_exact_dev_pin(pins: ModuleType) -> None:
    """TOOLS is hand-written, so it is verified rather than trusted (#443)."""
    assert pins.unrostered_exact_pins(REPO_ROOT / "pyproject.toml") == []


def test_catches_a_new_exact_pin_the_roster_does_not_name(
    pins: ModuleType, repo: Path
) -> None:
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            '    "pre-commit>=4.0",', '    "pre-commit>=4.0",\n    "black==24.1.0",'
        ),
        encoding="utf-8",
    )
    problems = pins.check(repo)
    assert len(problems) == 1
    assert "black==24.1.0" in problems[0]
    assert "TOOLS" in problems[0]


def test_catches_a_mypy_target_that_drifts_from_requires_python(
    pins: ModuleType, repo: Path
) -> None:
    """The exact shape that would silently invalidate the env check itself."""
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            '[tool.mypy]\npython_version = "3.11"',
            '[tool.mypy]\npython_version = "3.9"',
        ),
        encoding="utf-8",
    )
    problems = pins.check(repo)
    assert len(problems) == 1
    assert "[tool.mypy] python_version" in problems[0]
    assert "requires-python is authoritative" in problems[0]


def test_catches_a_ruff_target_that_drifts_from_requires_python(
    pins: ModuleType, repo: Path
) -> None:
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            'target-version = "py311"', 'target-version = "py313"'
        ),
        encoding="utf-8",
    )
    assert any("[tool.ruff] target-version" in p for p in pins.check(repo))


def test_catches_a_gate_workflow_moved_off_the_gate_python(
    pins: ModuleType, repo: Path
) -> None:
    """Bump lint.yml to 3.13 and the repo no longer lints what it targets."""
    workflow = repo / ".github" / "workflows" / "lint.yml"
    workflow.write_text(
        workflow.read_text().replace(
            'python-version: "3.11"', 'python-version: "3.13"'
        ),
        encoding="utf-8",
    )
    problems = pins.check(repo)
    assert len(problems) == 1
    assert "lint.yml" in problems[0]
    assert "does not include the declared target 3.11" in problems[0]


def test_a_gate_workflow_with_no_python_version_is_reported(
    pins: ModuleType, repo: Path
) -> None:
    other = repo / ".github" / "workflows" / "gate.yml"
    other.write_text(
        "name: Gate\njobs:\n  t:\n    steps:\n      - run: mypy src/\n",
        encoding="utf-8",
    )
    assert any("declares no literal python-version" in p for p in pins.check(repo))


def test_a_matrix_including_the_gate_python_is_accepted(
    pins: ModuleType, repo: Path
) -> None:
    """publish.yml's real shape: the gate runs on 3.11 *and* 3.12 and 3.13."""
    other = repo / ".github" / "workflows" / "matrix.yml"
    other.write_text(
        "name: Matrix\njobs:\n  t:\n    strategy:\n      matrix:\n"
        '        python-version: ["3.11", "3.12", "3.13"]\n'
        "    steps:\n"
        "      - uses: actions/setup-python@v7\n"
        "        with:\n"
        "          python-version: ${{ matrix.python-version }}\n"
        "      - run: mypy src/\n",
        encoding="utf-8",
    )
    assert pins.check(repo) == []


def test_a_tool_name_in_a_comment_is_not_a_gate_step(
    pins: ModuleType, repo: Path
) -> None:
    """lint.yml's prose mentions both tools; only `- run:` steps count."""
    other = repo / ".github" / "workflows" / "prose.yml"
    other.write_text(
        "name: Prose\njobs:\n  t:\n    steps:\n"
        "      # we used to run mypy src/ here\n"
        "      - run: echo mypy\n",
        encoding="utf-8",
    )
    assert pins.workflows_running_gates(other.parent).get(other) is None


def test_rejects_a_requires_python_that_is_not_a_bare_floor(
    pins: ModuleType, repo: Path
) -> None:
    """Guessing a gate version from a range is how a checker becomes a no-op."""
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            'requires-python = ">=3.11"', 'requires-python = ">=3.11,<4.0"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=re.escape("bare '>=X.Y' floor")):
        pins.check(repo)


def test_rejects_a_missing_mypy_python_version(pins: ModuleType, repo: Path) -> None:
    """Absent, mypy targets the running interpreter — the #398 drift itself."""
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace('\npython_version = "3.11"\n', "\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must set python_version"):
        pins.check(repo)


# --- the environment -------------------------------------------------------


def test_a_matching_environment_raises_no_error(
    pins: ModuleType, gate_python: tuple[int, int], matching_env: dict[str, str]
) -> None:
    """Guards every drift case below: they mutate from this passing state."""
    findings = pins.check_environment(
        python_version=gate_python, installed=matching_env
    )
    assert _errors(findings) == []
    assert _notes(findings) == []


def test_catches_the_wrong_python_minor_version(
    pins: ModuleType, gate_python: tuple[int, int], matching_env: dict[str, str]
) -> None:
    """`.ci-venv` on 3.12 against gates that run 3.11 — the live case."""
    drifted = (gate_python[0], gate_python[1] + 1)
    errors = _errors(
        pins.check_environment(python_version=drifted, installed=matching_env)
    )
    assert len(errors) == 1
    assert f"Python {drifted[0]}.{drifted[1]} is running" in errors[0]
    assert "checked zero files" in errors[0]


def test_catches_a_tool_one_patch_behind_the_pin(
    pins: ModuleType, gate_python: tuple[int, int], matching_env: dict[str, str]
) -> None:
    """`.ci-venv` on ruff 0.16.4 against a 0.16.5 pin — the other live case.

    A patch version is exactly the drift that looks too trivial to fail on,
    and #378's whole finding was that the older ruff stays green on code the
    newer one rejects.
    """
    drifted = {**matching_env, "ruff": "0.0.1"}
    errors = _errors(
        pins.check_environment(python_version=gate_python, installed=drifted)
    )
    assert len(errors) == 1
    assert "ruff 0.0.1 is on PATH" in errors[0]
    assert f"pip install 'ruff=={matching_env['ruff']}'" in errors[0]


def test_catches_both_drifts_at_once(
    pins: ModuleType, gate_python: tuple[int, int], matching_env: dict[str, str]
) -> None:
    """The real state of `.ci-venv` on 2026-09-03: wrong python AND stale ruff."""
    errors = _errors(
        pins.check_environment(
            python_version=(gate_python[0], gate_python[1] + 1),
            installed={**matching_env, "ruff": "0.0.1"},
        )
    )
    assert len(errors) == 2


def test_an_absent_tool_is_a_note_never_an_error(
    pins: ModuleType, gate_python: tuple[int, int], matching_env: dict[str, str]
) -> None:
    """CI's lint job installs ruff alone, and `ruff: not found` is already loud.

    The check exists for *silent* divergence. Failing on absence would break
    lint.yml for a condition that announces itself with exit 127.
    """
    findings = pins.check_environment(
        python_version=gate_python, installed={**matching_env, "mypy": None}
    )
    assert _errors(findings) == []
    assert len(_notes(findings)) == 1
    assert "mypy is not on PATH" in _notes(findings)[0]


def test_a_tool_that_will_not_report_its_version_is_an_error(
    pins: ModuleType, gate_python: tuple[int, int], matching_env: dict[str, str]
) -> None:
    """Unknown is not absent: it can diverge, and silently."""
    errors = _errors(
        pins.check_environment(
            python_version=gate_python,
            installed={**matching_env, "mypy": pins.UNKNOWN_VERSION},
        )
    )
    assert len(errors) == 1
    assert "would not report a version" in errors[0]


def test_installed_tool_versions_reads_path_not_metadata(pins: ModuleType) -> None:
    """`make lint` invokes the bare name, so the bare name is what is asked.

    Asserts the shape of the result, not its content — the ambient PATH is
    exactly what this file must not depend on.
    """
    resolved = pins.installed_tool_versions()
    assert set(resolved) == set(pins.TOOLS)
    for version in resolved.values():
        assert version is None or isinstance(version, str)


# --- the report -------------------------------------------------------------


def test_report_exits_one_on_drift_and_zero_when_overridden(
    pins: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The override is explicit by construction — an env var, not a default."""
    drift = [pins.EnvFinding("error", "boom")]
    assert pins._report_environment(drift, override=False) == 1
    assert pins._report_environment(drift, override=True) == 0
    assert pins.ENV_DRIFT_OVERRIDE in capsys.readouterr().err


def test_main_rejects_an_unknown_argument(
    pins: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pins.main(["--python-version", "3.12"]) == 2
    assert "unknown argument" in capsys.readouterr().err
