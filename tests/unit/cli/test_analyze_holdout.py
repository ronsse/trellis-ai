"""Tests for ``trellis analyze holdout``.

Each case runs the same store state through both format arms and compares
the exit codes, a dynamic witness beside the AST rule in
``tests/unit/test_format_exit_parity_rule.py``. Every id the fixture
writes is synthetic.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze._holdout_fixture import (
    INTENT_MARKER,
    MONDAY,
    HoldoutLog,
    seed_experiment,
)
from trellis.stores.registry import StoreRegistry
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()

WINDOW = ["--until", "2026-09-28T00:00:00+00:00", "--days", "60"]
FAST = ["--permutations", "999", "--bootstraps", "300", "--power-sims", "30"]


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


@pytest.fixture
def log(_temp_stores: StoreRegistry) -> HoldoutLog:
    return HoldoutLog(_temp_stores.operational.event_log)


def _run(*args: str) -> tuple[Result, Result]:
    """The command in text and in JSON over the same store."""
    base = ["analyze", "holdout", *WINDOW, *FAST, *args]
    return runner.invoke(app, base), runner.invoke(app, [*base, "--format", "json"])


def _payload(result: Result) -> dict[str, Any]:
    return json.loads(result.stdout.strip())


def _flat(result: Result) -> str:
    """Plain text with whitespace runs folded, so wrapping cannot split a phrase."""
    return " ".join(plain(result.stdout).split())


def _seed_rates(log: HoldoutLog) -> None:
    hour = 0
    for rate, arms in [(0.5, [False, True, False]), (0.25, [False, True])]:
        for withheld in arms:
            hour += 1
            log.task(start=MONDAY + timedelta(hours=hour), arms=[withheld], rate=rate)
    log.sweep()


def test_a_seeded_effect_is_reported_in_both_formats(log: HoldoutLog) -> None:
    seed_experiment(log, effect=1.5, seed=101, parents=3)
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    assert payload["status"] == "ok"
    assert payload["funnel"]["analysed"] == 60
    assert payload["inference"]["status"] == "ok"
    assert payload["inference"]["difference"] < 0
    assert payload["inference"]["p_value"] < 0.05
    flat = _flat(text)
    assert "difference detected at alpha 0.05" in flat
    assert "re-measure" in flat
    assert "within-stratum permutations" in flat


def test_a_flag_off_store_reports_no_withheld_arm_and_exits_0(
    log: HoldoutLog,
) -> None:
    for index in range(6):
        log.task(
            start=MONDAY + timedelta(hours=index),
            parent="parent-a" if index % 2 else "parent-b",
            arms=[False],
            rate=0.0,
            turns=5 + index,
        )
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    assert payload["inference"]["status"] == "no withheld arm"
    assert payload["inference"]["p_value"] is None
    assert payload["descriptive"]["eligible_tasks"] == 6
    flat = _flat(text)
    assert "no withheld arm" in flat
    assert "eligible tasks" in flat


def test_several_rates_without_rate_exit_2_in_both_formats(log: HoldoutLog) -> None:
    _seed_rates(log)

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_VALIDATION
    payload = _payload(machine)
    assert payload["status"] == "error"
    assert "--rate" in payload["message"]
    assert "--rate" in _flat(text)

    named_text, named_machine = _run("--rate", "0.25")
    assert named_text.exit_code == named_machine.exit_code == EXIT_OK
    assert _payload(named_machine)["funnel"]["eligible"] == 2


@pytest.mark.parametrize(
    "args",
    [["--outcome", "turns"], ["--until", "not-a-date"]],
    ids=["unknown-outcome", "bad-until"],
)
def test_invalid_options_exit_2_in_both_formats(
    log: HoldoutLog, args: list[str]
) -> None:
    seed_experiment(log, effect=1.0, seed=7, parents=2)
    log.sweep()

    text, machine = _run(*args)

    assert text.exit_code == machine.exit_code == EXIT_VALIDATION
    assert _payload(machine)["status"] == "error"


def test_a_null_result_is_never_worded_as_no_effect(log: HoldoutLog) -> None:
    seed_experiment(log, effect=1.0, seed=202, parents=3)
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    assert (
        _payload(machine)["inference"]["verdict"]
        == "no effect larger than the MDE detected"
    )
    flat = _flat(text)
    assert flat.count("no effect") >= 1
    assert flat.count("no effect") == flat.count("no effect larger than the MDE")


def test_no_id_or_intent_is_printed(log: HoldoutLog) -> None:
    seed_experiment(log, effect=1.5, seed=404, parents=2)
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    assert _payload(machine)["funnel"]["analysed"] == 40
    for output in (plain(text.stdout), machine.stdout):
        for needle in (INTENT_MARKER, "task-", "pack-0", "parent-", "item-"):
            assert needle not in output


def test_the_seed_reproduces_the_json_report(log: HoldoutLog) -> None:
    seed_experiment(log, effect=1.2, seed=303, parents=2)
    log.sweep()

    _, first = _run("--seed", "5")
    _, second = _run("--seed", "5")
    _, other = _run("--seed", "6")

    assert _payload(first) == _payload(second)
    assert _payload(first)["inference"] != _payload(other)["inference"]
