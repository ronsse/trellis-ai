"""Smoke test for the eval runner.

Exercises discovery + execution end-to-end against the no-op
``_example`` scenario. The intent is to keep the harness from rotting
between scenario PRs — substantive scenario semantics are tested inside
each scenario's own directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from eval import runner
from eval.runner import (
    Finding,
    ScenarioReport,
    list_scenarios,
    main,
    run_scenario,
    write_report,
)


def test_list_scenarios_includes_example() -> None:
    names = list_scenarios()
    assert "_example" in names


def test_run_scenario_example_passes() -> None:
    registry = MagicMock()
    report = run_scenario("_example", registry)
    assert report.name == "_example"
    assert report.status == "pass"
    assert report.metrics["noop"] == 1.0
    assert any(f.severity == "info" for f in report.findings)


def test_run_scenario_unknown_returns_fail() -> None:
    registry = MagicMock()
    report = run_scenario("does_not_exist", registry)
    assert report.status == "fail"
    assert any(f.severity == "fail" for f in report.findings)


def test_write_report_creates_json_and_markdown(tmp_path: Path) -> None:
    reports = [
        ScenarioReport(
            name="dummy",
            status="pass",
            metrics={"x": 0.5},
            findings=[Finding(severity="info", message="hello")],
            decision="No action; smoke only.",
            duration_seconds=0.01,
        )
    ]
    json_path, md_path = write_report(reports, out_dir=tmp_path)
    assert json_path.exists()
    assert md_path.exists()

    payload = json.loads(json_path.read_text())
    assert payload["scenarios"][0]["name"] == "dummy"
    assert payload["scenarios"][0]["metrics"]["x"] == 0.5

    md = md_path.read_text()
    assert "dummy" in md
    assert "Decision" in md


def test_main_no_write_returns_zero(monkeypatch, tmp_path: Path) -> None:
    """``main`` should run the example scenario and exit 0."""
    # Avoid touching the real config dir / data dir.
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    rc = main(["--scenario", "_example", "--no-write"])
    assert rc == 0


def test_main_list_returns_zero(capsys, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    rc = main(["--list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "_example" in captured.out


def test_runner_module_path_constant() -> None:
    """Pin the package path so a rename can't silently drop scenarios."""
    assert runner.SCENARIOS_PACKAGE == "eval.scenarios"
