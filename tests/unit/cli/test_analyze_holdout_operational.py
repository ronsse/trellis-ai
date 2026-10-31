"""Tests for ``trellis analyze holdout --operational``.

Each case runs the same store state through both format arms and compares
the exit codes, a dynamic witness beside the AST rule in
``tests/unit/test_format_exit_parity_rule.py``. Every id and commit the
fixtures write is synthetic.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze._holdout_fixture import DAYS, UNTIL, HoldoutLog
from tests.unit.analyze._operational_seed import seed_store
from trellis.analyze.holdout import analyze_holdout_operational
from trellis.stores.registry import StoreRegistry
from trellis_cli.analyze import holdout
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from click.testing import Result

runner = CliRunner()

WINDOW = ["--until", UNTIL.isoformat(), "--days", str(DAYS)]
FAST = ["--permutations", "99", "--bootstraps", "50", "--power-sims", "5"]
#: Top-level keys only the outcome analysis writes.
OUTCOME_KEYS = {"seed", "inference", "covariate_adjusted", "power", "guardrails"}


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
def seeded(_temp_stores: StoreRegistry) -> HoldoutLog:
    log = HoldoutLog(_temp_stores.operational.event_log)
    seed_store(log)
    return log


def _run(*args: str) -> tuple[Result, Result]:
    """The command in text and in JSON over the same store."""
    base = ["analyze", "holdout", *WINDOW, *args]
    return runner.invoke(app, base), runner.invoke(app, [*base, "--format", "json"])


def _payload(result: Result) -> dict[str, Any]:
    return json.loads(result.stdout.strip())


def _flat(result: Result) -> str:
    """Plain text with whitespace runs folded, so wrapping cannot split a phrase."""
    return " ".join(plain(result.stdout).split())


@pytest.mark.usefixtures("seeded")
def test_operational_json_has_no_outcome_section_and_exits_alike() -> None:
    text, as_json = _run("--operational")
    assert text.exit_code == as_json.exit_code == EXIT_OK
    payload = _payload(as_json)
    assert payload["mode"] == "operational"
    assert OUTCOME_KEYS.isdisjoint(payload)
    assert payload["funnel"]["eligible"] == 24
    assert payload["funnel"]["no_outcome"] == 1
    assert {"analysed", "cut_offs_excluded", "outcome_missing"}.isdisjoint(
        payload["funnel"]
    )
    assert "by_commit" not in payload


@pytest.mark.usefixtures("seeded")
def test_operational_text_prints_counts_and_no_outcome_figure() -> None:
    text, _ = _run("--operational", "--by-commit")
    flat = _flat(text)
    assert "Counts (no outcome value read)" in flat
    assert "Covariate coverage" in flat
    assert "By serving build (eligible task counts only)" in flat
    for absent in (
        "Inference",
        "Guardrails",
        "Secondary",
        "MDE",
        "mean served",
        "outcome SD",
        "n/mean",
        "analysed tasks",
        "outcome missing",
        "cut-offs excluded",
        "packs per task",
        "post-hoc",
        "span builds",
        "outcome's field",
    ):
        assert absent not in flat, absent


@pytest.mark.usefixtures("seeded")
def test_operational_by_commit_emits_counts_only() -> None:
    _, as_json = _run("--operational", "--by-commit")
    table = _payload(as_json)["by_commit"]
    assert table["rows"]
    for row in table["rows"]:
        assert set(row) == {"build", "first_seen", "last_seen", "served", "withheld"}
        assert isinstance(row["served"], int)
        assert isinstance(row["withheld"], int)


@pytest.mark.usefixtures("seeded")
def test_inference_options_have_no_effect_in_operational_mode() -> None:
    _, plain_json = _run("--operational")
    _, tuned_json = _run(
        "--operational", *FAST, "--seed", "5", "--planned-mde", "9", "--planned-n", "3"
    )
    assert plain_json.stdout == tuned_json.stdout


@pytest.mark.usefixtures("seeded")
def test_the_default_report_is_unchanged_without_the_flag() -> None:
    text, as_json = _run(*FAST)
    assert text.exit_code == as_json.exit_code == EXIT_OK
    payload = _payload(as_json)
    assert set(payload) >= OUTCOME_KEYS
    assert "mode" not in payload
    assert "Inference" in _flat(text)


@pytest.mark.usefixtures("seeded")
def test_a_validation_error_exits_alike_in_both_formats() -> None:
    text, as_json = _run("--operational", "--days", "0")
    assert text.exit_code == as_json.exit_code == EXIT_VALIDATION
    assert _payload(as_json)["status"] == "error"


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize(
    "option",
    [
        ("--permutations", "0"),
        ("--bootstraps", "0"),
        ("--power-sims", "0"),
        ("--planned-mde", "0"),
        ("--planned-n", "0"),
        ("--seed", "-1"),
    ],
    ids=lambda option: option[0],
)
def test_an_invalid_inference_option_exits_alike_with_and_without_the_flag(
    option: tuple[str, str],
) -> None:
    """The flag ignores these options, but a command line that is wrong is wrong."""
    for flag in ((), ("--operational",)):
        text, as_json = _run(*flag, *option)
        assert text.exit_code == as_json.exit_code == EXIT_VALIDATION, (flag, option)
        assert _payload(as_json)["status"] == "error"


@pytest.mark.usefixtures("seeded")
def test_help_describes_the_flag_and_no_longer_denies_the_means() -> None:
    holdout_help = runner.invoke(app, ["analyze", "holdout", "--help"])
    flat = _flat(holdout_help)
    assert "--operational" in flat
    assert "No estimate" not in flat
    assert "No outcome value is read" in flat
    assert "validated" in flat
    value_help = _flat(runner.invoke(app, ["analyze", "value", "--help"]))
    assert "does not have" not in value_help
    assert "analyze holdout" in value_help


def test_every_shared_cli_default_is_the_operational_library_default() -> None:
    """The flag reuses the command's options; each one it reads keeps its default."""
    library = {
        name: parameter.default
        for name, parameter in inspect.signature(
            analyze_holdout_operational
        ).parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }
    command = {
        name: parameter.default.default
        for name, parameter in inspect.signature(holdout).parameters.items()
        if name in library
    }

    assert command == library
    assert set(library) >= {
        "days",
        "rate",
        "outcome",
        "itt",
        "by_commit",
        "permutations",
        "bootstraps",
        "power_sims",
        "planned_mde",
        "planned_n",
        "seed",
    }
