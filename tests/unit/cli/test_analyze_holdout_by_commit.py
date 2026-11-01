"""Tests for ``trellis analyze holdout --by-commit``.

Each case runs the same store state through both format arms and compares
the exit codes, a dynamic witness beside the AST rule in
``tests/unit/test_format_exit_parity_rule.py``. Every id and commit the
fixture writes is synthetic.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze._holdout_fixture import MONDAY, HoldoutLog, provenance
from trellis.ops.serving_builds import NO_PROVENANCE_LABEL, build_label
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()

WINDOW = ["--until", "2026-09-28T00:00:00+00:00", "--days", "60"]
FAST = ["--permutations", "999", "--bootstraps", "300", "--power-sims", "30"]
COMMIT_A = "a1b2c3d4e5f6"
COMMIT_B = "9f8e7d6c5b4a"
#: A build is the fixture's stamped ``(version, commit)``, labelled as
#: ``analyze health`` labels it.
BUILD_A = build_label("0.0.0", COMMIT_A)
BUILD_B = build_label("0.0.0", COMMIT_B)
#: A commit the console would mangle unless the label is escaped: ``[b]`` is a
#: style tag Rich deletes and ``:smile:`` an emoji code it replaces.
MARKUP_COMMIT = "[b]x:smile:y"
MARKUP_BUILD = build_label("0.0.0", MARKUP_COMMIT)


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoreRegistry:
    """Point CLI stores at a temp directory and return the registry."""
    data_dir = tmp_path / "data"
    stores_dir = data_dir / "stores"
    stores_dir.mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _reset_registry()

    return StoreRegistry(stores_dir=stores_dir)


@pytest.fixture(autouse=True)
def _every_build_stands_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """These cases are about the table's shape, which pooling would blur.

    The pooling rule has its own tests, run at the real threshold, in
    ``tests/unit/analyze/test_holdout_readout_form.py``.
    """
    monkeypatch.setattr("trellis.analyze.holdout.BY_COMMIT_POOL_THRESHOLD", 0)


@pytest.fixture
def log(_temp_stores: StoreRegistry) -> HoldoutLog:
    return HoldoutLog(_temp_stores.operational.event_log)


def _run(*args: str) -> tuple[Result, Result]:
    """The command in text and in JSON over the same store."""
    base = ["analyze", "holdout", *WINDOW, *FAST, "--outcome", "commits", *args]
    return runner.invoke(app, base), runner.invoke(app, [*base, "--format", "json"])


def _payload(result: Result) -> dict[str, Any]:
    return json.loads(result.stdout.strip())


def _flat(result: Result) -> str:
    """Plain text with whitespace runs folded, so wrapping cannot split a phrase."""
    return " ".join(plain(result.stdout).split())


def _seed(log: HoldoutLog, commit_b: str = COMMIT_B) -> None:
    """Build A (hours 0-1): one served task of 2 commits, one withheld of 4.

    Build B (hours 30-31): one served task of 6 commits, one withheld of 8.
    """
    layout = [
        (0, COMMIT_A, False, 2),
        (1, COMMIT_A, True, 4),
        (30, commit_b, False, 6),
        (31, commit_b, True, 8),
    ]
    for hours, commit, withheld, commits in layout:
        log.task(
            start=MONDAY + timedelta(hours=hours),
            arms=[withheld],
            commits=commits,
            pack_metadata=[provenance(commit)],
        )
    log.sweep()


def test_the_flag_adds_the_table_to_json_and_text(log: HoldoutLog) -> None:
    _seed(log)

    text, machine = _run("--by-commit")

    assert text.exit_code == machine.exit_code == EXIT_OK
    table = _payload(machine)["by_commit"]
    assert [row["build"] for row in table["rows"]] == [BUILD_A, BUILD_B]
    first = table["rows"][0]
    assert first["served"] == {"n": 1, "outcome_mean": 2.0}
    assert first["withheld"] == {"n": 1, "outcome_mean": 4.0}
    assert table["rows"][1]["served"] == {"n": 1, "outcome_mean": 6.0}
    assert table["units_spanning_builds"] == 0
    assert "no inference" in table["note"]
    flat = _flat(text)
    assert "By serving build (descriptive, non-decisive, no inference)" in flat
    assert BUILD_A in flat
    assert BUILD_B in flat
    # The table follows the descriptive block and precedes the exclusions.
    assert flat.index("re-measure") < flat.index("By serving build")
    assert flat.index("By serving build") < flat.index("Exclusions")


def test_without_the_flag_nothing_changes(log: HoldoutLog) -> None:
    _seed(log)

    text, machine = _run()
    flagged_text, flagged_machine = _run("--by-commit")

    assert text.exit_code == machine.exit_code == EXIT_OK
    plain_payload = _payload(machine)
    assert "by_commit" not in plain_payload
    assert "By serving build" not in _flat(text)
    flagged = _payload(flagged_machine)
    del flagged["by_commit"]
    assert flagged == plain_payload
    # Text output is the unflagged output plus the table's block, nothing else.
    head, rest = plain(flagged_text.stdout).split("\n  By serving build", 1)
    tail = rest.split("\n  Exclusions", 1)[1]
    assert head + "\n  Exclusions" + tail == plain(text.stdout)


def test_a_build_name_is_never_read_as_console_markup(log: HoldoutLog) -> None:
    _seed(log, commit_b=MARKUP_COMMIT)

    text, machine = _run("--by-commit")

    assert text.exit_code == machine.exit_code == EXIT_OK
    assert MARKUP_BUILD in [
        row["build"] for row in _payload(machine)["by_commit"]["rows"]
    ]
    assert MARKUP_BUILD in _flat(text)


def test_the_flag_without_tasks_prints_an_empty_table_and_exits_alike() -> None:
    text, machine = _run("--by-commit")

    assert text.exit_code == machine.exit_code
    assert _payload(machine)["by_commit"]["rows"] == []
    assert "no analysed task" in _flat(text)


def test_a_build_is_the_version_and_commit_pair_labelled_as_health_labels_it(
    log: HoldoutLog,
) -> None:
    stamps = [
        {"write_provenance": {"version": "1.0.0", "commit": COMMIT_A}},
        {"write_provenance": {"version": "1.1.0", "commit": COMMIT_A}},
        None,
    ]
    for hours, stamp in enumerate(stamps):
        log.task(
            start=MONDAY + timedelta(hours=hours),
            arms=[False],
            commits=hours + 1,
            pack_metadata=[stamp],
        )
    log.sweep()

    text, machine = _run("--by-commit")

    assert text.exit_code == machine.exit_code == EXIT_OK
    expected = [
        build_label("1.0.0", COMMIT_A),
        build_label("1.1.0", COMMIT_A),
        NO_PROVENANCE_LABEL,
    ]
    table = _payload(machine)["by_commit"]
    assert [row["build"] for row in table["rows"]] == expected
    flat = _flat(text)
    for label in expected:
        assert label in flat
    assert "under its first pack's build" in flat
