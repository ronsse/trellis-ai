"""Tests for ``trellis analyze holdout``.

Each case runs the same store state through both format arms and compares
the exit codes, a dynamic witness beside the AST rule in
``tests/unit/test_format_exit_parity_rule.py``. Every id the fixture
writes is synthetic.
"""

from __future__ import annotations

import inspect
import json
import math
import statistics
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
    outcome_payload,
    seed_experiment,
)
from trellis.analyze.holdout import analyze_holdout
from trellis.stores.registry import StoreRegistry
from trellis_cli.analyze import holdout
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


def test_rules_the_analysis_cannot_apply_are_printed_in_both_formats(
    log: HoldoutLog,
) -> None:
    for index in range(4):
        log.task(
            start=MONDAY + timedelta(hours=index),
            arms=[index % 2 == 1],
            turns=10 + index,
            ended_on_error=index == 2,
        )
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    rules = {e["name"]: (e["applied"], e["excluded"]) for e in payload["exclusions"]}
    assert rules == {
        "non_ephemeral": (True, None),
        "pre_treatment": (False, None),
        "cut_offs": (True, 1),
    }
    assert any("brief-length" in note for note in payload["notes"])
    flat = _flat(text)
    assert "population (non_ephemeral): applied. Upstream:" in flat
    assert "pre-treatment (pre_treatment): not applied." in flat
    assert "post-treatment (cut_offs): applied, 1 excluded." in flat
    assert "residualised permutation (secondary analysis): not applied" in flat


def test_every_cli_default_is_the_library_default() -> None:
    """The command repeats analyze_holdout's defaults as literals; drift fails here."""
    library = {
        name: parameter.default
        for name, parameter in inspect.signature(analyze_holdout).parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }
    command = {
        name: parameter.default.default
        for name, parameter in inspect.signature(holdout).parameters.items()
        if name != "output_format"
    }

    assert command == library


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


#: Exact t(0.975) + t(0.80) at 4, 10 and 16 degrees of freedom, to six
#: decimals: the prestudy's MDE multiplier at N = 6, 12 and 18.
T_SUM = {6: 3.717410, 12: 3.107197, 18: 2.984572}


def test_every_r_figure_is_in_the_json_and_the_text(log: HoldoutLog) -> None:
    """Four main sessions of three served sub-agent tasks each, flag off.

    12 analysed tasks in 60 days is 6 per 30 days (N 6 / 12 / 18); 4
    analysed main sessions is 2 per 30 days (N 2 / 4 / 6). Sub-agent turns
    are 4 + s, 9 + s and 14 + s in session s, and the first sub-agent of
    each session opens one PR.
    """
    for s in range(4):
        start = MONDAY + timedelta(days=s)
        main = log.task(start=start, parent=None, arms=(), turns=2 + s, rate=0.0)
        for sub in range(3):
            log.task(
                start=start + timedelta(hours=2 + sub),
                parent=main,
                arms=[False],
                rate=0.0,
                turns=4 + 5 * sub + s,
                prs_created=1 if sub == 0 else 0,
            )
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    d = _payload(machine)["descriptive"]
    assert "pr_base_rate" not in d
    assert d["pr_base_rate_served"] == pytest.approx(1 / 3)
    assert d["top_parent_share"] == pytest.approx(0.25)
    sub_turns = [[4 + s, 9 + s, 14 + s] for s in range(4)]
    residuals = [
        math.log1p(t) - statistics.fmean(math.log1p(u) for u in turns)
        for turns in sub_turns
        for t in turns
    ]
    sd = math.sqrt(sum(r * r for r in residuals) / (12 - 4))
    assert d["outcome_sd_within_parent"] == pytest.approx(sd)
    horizons = d["mde_by_horizon"]
    assert [h["days"] for h in horizons] == [30, 60, 90]
    assert [h["n"] for h in horizons] == pytest.approx([6, 12, 18])
    expected = [T_SUM[n] * sd * math.sqrt(4 / n) for n in (6, 12, 18)]
    assert [h["mde"] for h in horizons] == pytest.approx(expected, rel=1e-3)
    assert [h["ratio"] for h in horizons] == pytest.approx(
        [math.exp(m) for m in expected], rel=1e-3
    )
    assert d["n_for_10pct_effect"] > 18
    sessions = d["sessions"]
    counts = ("rolled_up", "reaching_retrieval", "eligible", "analysed")
    assert [sessions[key] for key in counts] == [4, 4, 4, 4]
    assert sessions["reaching_retrieval_per_30d"] == pytest.approx(2.0)
    session_sd = statistics.stdev(math.log1p(29 + 4 * s) for s in range(4))
    assert sessions["outcome_sd"] == pytest.approx(session_sd)
    assert [h["n"] for h in sessions["mde_by_horizon"]] == pytest.approx([2, 4, 6])
    assert set(d["not_measurable"]) == {
        "post_hoc_share",
        "cut_off_share_by_arm.withheld",
    }
    flat = _flat(text)
    for label in (
        "top parent's share of eligible tasks 0.25",
        "PR base rate (served arm) 0.333",
        "between-parent share of variance (bias-adjusted",
        "task MDE (80% power",
        "30 days: N 6, MDE",
        "60 days: N 12, MDE",
        "90 days: N 18, MDE",
        "N for a 10% effect",
        "main sessions 4 rolled up",
        "reaching a retrieval 4 (2 per 30 days)",
        "main-session MDE",
        "30 days: N 2, MDE not measurable: N 2 is below 6",
    ):
        assert label in flat


def test_a_figure_the_rows_cannot_give_reads_not_measurable(
    log: HoldoutLog,
) -> None:
    """Three tasks, one per parent, whose outcomes lack prs_created.

    A within-parent SD needs more tasks than parents, the PR figures need
    the field, and no task names a main transcript that joined.
    """
    for index, parent in enumerate(["parent-a", "parent-b", "parent-c"]):
        at = MONDAY + timedelta(hours=index)
        pack = log.pack(at=at, withheld=False, items=2, rate=0.0)
        outcome = outcome_payload(turns=5 + 3 * index)
        del outcome["prs_created"]
        log.join(
            f"task-{index}",
            at=at + timedelta(hours=1),
            pack_ids=[pack],
            parent=parent,
            outcome=outcome,
        )
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    d = _payload(machine)["descriptive"]
    spread = "needs more analysed tasks than parent sessions, found 3 across 3"
    assert d["not_measurable"] == {
        "outcome_sd_within_parent": spread,
        "between_parent_share": spread,
        "n_for_10pct_effect": spread,
        "post_hoc_share": d["not_measurable"]["post_hoc_share"],
        "cut_off_share_by_arm.withheld": "no eligible task in the withheld arm",
        "pr_base_rate_served": "no analysed served-arm task records prs_created",
        "prs_created_mean": "no analysed task records prs_created",
    }
    assert "capture records none of them" in d["not_measurable"]["post_hoc_share"]
    for key in ("outcome_sd_within_parent", "between_parent_share"):
        assert d[key] is None
    assert d["n_for_10pct_effect"] is None
    assert d["pr_base_rate_served"] is None
    assert [h["mde"] for h in d["mde_by_horizon"]] == [None, None, None]
    assert {h["not_measurable"] for h in d["mde_by_horizon"]} == {spread}
    sessions = d["sessions"]
    assert (sessions["rolled_up"], sessions["without_main_join"]) == (0, 3)
    assert sessions["outcome_sd"] is None
    assert "found 0" in sessions["not_measurable"]["outcome_sd"]
    flat = _flat(text)
    assert f"outcome SD within parent not measurable: {spread}" in flat
    assert (
        "PR base rate (served arm) not measurable: no analysed served-arm "
        "task records prs_created" in flat
    )
    assert "main-session outcome SD not measurable: needs 2 main sessions" in flat
    descriptive = flat.split("Descriptive (the [R] re-measure)")[1]
    assert "n/a" not in descriptive.split("Exclusions")[0]
