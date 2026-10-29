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
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.analyze._holdout_fixture import (
    INTENT_MARKER,
    MONDAY,
    HoldoutLog,
    covariates_payload,
    outcome_payload,
    seed_experiment,
    seed_guardrails,
    seed_late_build,
    seed_missing_tool_errors,
    timing_payload,
)
from trellis.analyze import holdout as holdout_analysis
from trellis.analyze.holdout import (
    SECONDARY_VERDICT,
    HoldoutAdjustment,
    HoldoutCovariateAdjusted,
    analyze_holdout,
)
from trellis.stores.registry import StoreRegistry
from trellis_cli.analyze import _render_holdout_secondary, holdout
from trellis_cli.exit_codes import EXIT_OK, EXIT_VALIDATION
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

if TYPE_CHECKING:
    from click.testing import Result

    from trellis.analyze.holdout import HoldoutReport

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
    # The secondary block is printed even when no join records a covariate.
    assert (
        "Secondary (covariate-adjusted, not decisive): brief-length (pre-registered) "
        "not measurable: no analysed task's join records the covariates" in flat
    )
    assert "CI ignores coefficient uncertainty" not in flat
    assert payload["covariate_adjusted"]["status"] == "not_measurable"
    # A refused fit still says how many tasks it had, and prints no R2 it never
    # computed: its counts are what tell a reader the fit had nothing to use.
    assert flat.count("0 tasks used, 3 missing a covariate") == 2
    assert all(
        (a["n_used"], a["n_missing_covariate"]) == (0, 3)
        for a in payload["covariate_adjusted"]["adjustments"]
    )
    assert "arm balance R2" not in flat


def test_a_partly_decided_pre_treatment_exclusion_is_printed_in_both_formats(
    log: HoldoutLog,
) -> None:
    timings = [
        timing_payload(hook_before=True),
        timing_payload(hook_before=False),
        timing_payload(hook_before=False),
        None,
        None,
    ]
    for index, timing in enumerate(timings):
        log.task(
            start=MONDAY + timedelta(hours=index),
            arms=[index % 2 == 1],
            timing=timing,
        )
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    pre = next(e for e in payload["exclusions"] if e["name"] == "pre_treatment")
    assert (pre["applied"], pre["excluded"], pre["unknown"]) == (True, 1, 2)
    funnel = payload["funnel"]
    assert funnel["pre_treatment_excluded"] == 1
    assert funnel["pre_treatment_unknown"] == 2
    assert funnel["eligible"] == 4
    flat = _flat(text)
    assert (
        "pre-treatment (pre_treatment): partially applied, 1 excluded, "
        "2 undecidable and kept. Partially applied:" in flat
    )
    assert "1 first retrieval after a stop hook's feedback or a commit" in flat
    assert "2 with no retrieval timing" in flat


def test_a_join_without_a_pack_is_split_by_why_in_the_funnel(
    log: HoldoutLog,
) -> None:
    for index, counters in enumerate(
        [{"retrieval_results": 0}, {"retrieval_results": 1, "retrieval_errors": 1}]
    ):
        log.join(
            f"task-{index}",
            at=MONDAY + timedelta(hours=index),
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
            **counters,  # type: ignore[arg-type]
        )
    log.sweep()

    text, machine = _run()

    funnel = _payload(machine)["funnel"]
    assert funnel["no_retrieval"] == 2
    assert funnel["no_retrieval_never_called"] == 1
    assert funnel["no_retrieval_errored"] == 1
    assert "2 without a pack (1 never retrieved, 1 errored, 0 unparsed, 0 empty" in (
        _flat(text)
    )


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
    log.deployed_before_window(rate=0.0)
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
    # x0.9 on log1p turns at the within-parent SD, not the total SD.
    n_10 = holdout_analysis.n_for_mde(sd, -math.log(0.9))
    assert d["n_for_10pct_effect"] == n_10
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
        (
            "floored at 0) "
            f"{format(d['between_parent_share'], '.3g')} across 4 parent sessions"
        ),
        "task MDE (80% power",
        "30 days: N 6, MDE",
        "60 days: N 12, MDE",
        "90 days: N 18, MDE",
        (
            "N for a 10% effect (x0.9 on a log1p outcome, 10% of the served "
            f"mean otherwise): {n_10:,}"
        ),
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
    assert d["post_hoc_share"] is None
    assert (
        "none of the 3 tasks that reached the pre-treatment rule has a join "
        "recording the first retrieval's position"
        in d["not_measurable"]["post_hoc_share"]
    )
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
    # The reason gives the counts, so no parent-session suffix follows it.
    assert f"floored at 0) not measurable: {spread} top parent's share" in flat
    assert (
        "PR base rate (served arm) not measurable: no analysed served-arm "
        "task records prs_created" in flat
    )
    assert "main-session outcome SD not measurable: needs 2 main sessions" in flat
    descriptive = flat.split("Descriptive (the [R] re-measure)")[1]
    assert "n/a" not in descriptive.split("Exclusions")[0]


def _guardrail_block(result: Result) -> str:
    """The text report's guardrails block, up to the first report note."""
    after = _flat(result).split("Guardrails (reported, never decisive")[1]
    return after.split("note:")[0]


def _guardrails(result: Result) -> dict[str, dict[str, Any]]:
    block = _payload(result)["guardrails"]
    return {measure["name"]: measure for measure in block["measures"]}


def test_guardrails_are_printed_per_arm_in_both_formats(log: HoldoutLog) -> None:
    """The hand store of ``tests/unit/analyze/test_holdout_guardrails.py``."""
    seed_guardrails(log)

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    assert _payload(machine)["guardrails"]["status"] == "ok"
    rows = _guardrails(machine)
    assert rows["prs_created"]["served"] == {
        "n": 4,
        "value": pytest.approx(0.75),
        "missing": 0,
        "not_measurable": None,
    }
    assert rows["cut_off_rate"]["task_set"] == "eligible tasks, cut-offs included"
    assert rows["cut_off_rate"]["withheld"]["n"] == 6
    assert rows["cut_off_rate"]["withheld"]["value"] == pytest.approx(1 / 3)
    assert rows["recall_rate"]["withheld"]["value"] == pytest.approx(0.75)
    flat = _flat(text)
    assert "Guardrails (reported, never decisive: no p-value, no verdict)" in flat
    block = _guardrail_block(text)
    for line in (
        (
            "PRs created per task (analysed tasks): served 0.75 (n 4), withheld "
            "0.25 (n 4)"
        ),
        (
            "share of tasks with a commit (analysed tasks): served 0.5 (n 4), "
            "withheld 0.25 (n 4)"
        ),
        (
            f"mean log1p commits (analysed tasks): served {3 * math.log(2) / 4:.3g} "
            f"(n 4), withheld {math.log(3) / 4:.3g} (n 4)"
        ),
        (
            "cut-off rate (eligible tasks, cut-offs included): served 0.2 (n 5), "
            "withheld 0.333 (n 6)"
        ),
        (
            "tool errors per task, covariate grade (analysed tasks): served 1.75 "
            "(n 4), withheld 2.25 (n 4)"
        ),
        (
            "re-call rate after the first retrieval, a manipulation check "
            "(analysed tasks): served 0.5 (n 4), withheld 0.75 (n 4)"
        ),
    ):
        assert line in block


def test_a_guardrail_field_the_join_lacks_reads_not_measurable_in_both_formats(
    log: HoldoutLog,
) -> None:
    seed_missing_tool_errors(log)

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    rows = _guardrails(machine)
    assert len(rows) == 6
    errors = rows["tool_errors"]
    assert errors["served"] == {
        "n": 0,
        "value": None,
        "missing": 3,
        "not_measurable": "outcome.tool_errors missing",
    }
    assert errors["withheld"] == {
        "n": 2,
        "value": pytest.approx(2.0),
        "missing": 1,
        "not_measurable": None,
    }
    assert (
        "tool errors per task, covariate grade (analysed tasks): served not "
        "measurable: outcome.tool_errors missing (n 0), withheld 2 (n 2, 1 "
        "without outcome.tool_errors)" in _guardrail_block(text)
    )


def test_a_flag_off_store_shows_the_guardrails_for_the_served_arm_only(
    log: HoldoutLog,
) -> None:
    """Rate 0: commits 0, 1, 2 and 3, so 3 of 4 tasks committed."""
    for index in range(4):
        log.task(
            start=MONDAY + timedelta(hours=index),
            parent="parent-a" if index % 2 else "parent-b",
            rate=0.0,
            turns=5 + index,
            commits=index,
        )
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    assert _payload(machine)["guardrails"]["status"] == "no withheld arm"
    rows = _guardrails(machine)
    assert [row["withheld"] for row in rows.values()] == [None] * 6
    assert {row["served"]["n"] for row in rows.values()} == {4}
    flat = _flat(text)
    assert (
        "Guardrails (reported, never decisive: no p-value, no verdict): no "
        "withheld arm" in flat
    )
    block = _guardrail_block(text)
    assert "share of tasks with a commit (analysed tasks): served 0.75 (n 4)" in block
    assert "), withheld" not in block
    assert "served arm only" in block


def test_the_task_window_is_printed_in_both_formats(log: HoldoutLog) -> None:
    """Rows at rate 0.5 start 20 days into the window: 10 and 8 tasks in 40 days."""
    seed_late_build(log)

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    payload = _payload(machine)
    assert payload["window_days"] == 60
    assert payload["task_window_since"] == "2026-08-19T00:00:00+00:00"
    assert payload["task_window_days"] == pytest.approx(40.0)
    d = payload["descriptive"]
    assert d["eligible_per_30d"] == pytest.approx(7.5)
    assert d["analysed_per_30d"] == pytest.approx(6.0)
    assert d["sessions"]["analysed_per_30d"] == pytest.approx(1.0)
    assert "whole 60-day window" in d["sessions"]["note"]
    assert any("not by the 60-day window" in note for note in payload["notes"])
    flat = _flat(text)
    assert (
        "task window 2026-08-19T00:00:00+00:00 to 2026-09-28T00:00:00+00:00 "
        "(40 days)" in flat
    )
    assert "eligible tasks 10 (7.5 per 30 days); analysed 8 (6 per 30 days)" in flat
    assert "whole 60-day window" in flat
    assert "not by the 60-day window" in flat


def test_a_fit_refused_for_too_few_tasks_prints_the_counts_that_explain_it(
    log: HoldoutLog,
) -> None:
    """Five of eight tasks carry a brief length: too few left once the fit's
    stratum columns and covariate are paid for, so the fit is refused."""
    for parent in range(2):
        for index in range(4):
            task = 4 * parent + index
            log.task(
                start=MONDAY + timedelta(hours=task),
                parent=f"parent-{parent}",
                arms=[index % 2 == 1],
                turns=10 + task,
                covariates=(
                    covariates_payload(brief_chars=100 + 30 * task)
                    if task < 5
                    else None
                ),
            )
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    block = _payload(machine)["covariate_adjusted"]
    assert block["status"] == "not_measurable"
    for adjustment in block["adjustments"]:
        assert adjustment["inference"] is None
        assert "residual degrees of freedom" in adjustment["reason"]
        assert (adjustment["n_used"], adjustment["n_missing_covariate"]) == (5, 3)
        assert adjustment["arm_balance_r2"] is None
    flat = _flat(text)
    assert (
        flat.count(
            "not measurable: 2 residual degrees of freedom after the fit, fewer "
            "than 4 5 tasks used, 3 missing a covariate"
        )
        == 2
    )
    assert "arm balance R2" not in flat


def test_a_fit_refused_after_its_r2_is_computed_prints_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the R2 refusal reaches a figure for the balance line to print."""
    refused = HoldoutAdjustment(
        name="brief_length",
        preregistered=True,
        status="not_measurable",
        reason="the covariates explain 0.95 of the arm's within-stratum variance",
        n_used=40,
        n_missing_covariate=2,
        agent_type_levels=None,
        df_residual=36,
        arm_balance_r2=0.95,
        inference=None,
        unadjusted_same_subset=None,
    )
    report = SimpleNamespace(
        covariate_adjusted=HoldoutCovariateAdjusted(
            status="not_measurable", note="", adjustments=[refused]
        )
    )

    _render_holdout_secondary(cast("HoldoutReport", report))

    flat = " ".join(plain(capsys.readouterr().out).split())
    assert "not measurable: the covariates explain 0.95" in flat
    assert "40 tasks used, 2 missing a covariate; arm balance R2 0.95" in flat


def test_the_covariate_adjusted_block_is_printed_in_both_formats(
    log: HoldoutLog,
) -> None:
    digests = ("0a1b2c3d4e51", "0a1b2c3d4e52")
    for parent in range(3):
        for index in range(8):
            log.task(
                start=MONDAY + timedelta(hours=3 * index, minutes=parent),
                parent=f"parent-{parent}",
                arms=[index % 2 == 1],
                turns=10 + 3 * parent + (index // 2) + 2 * (index % 2 == 0),
                covariates=covariates_payload(
                    brief_chars=100 + 40 * (index // 2) + parent,
                    agent_type=digests[(index // 2) % 2],
                ),
            )
    log.sweep()

    text, machine = _run()

    assert text.exit_code == machine.exit_code == EXIT_OK
    block = _payload(machine)["covariate_adjusted"]
    assert block["status"] == "ok"
    assert [(a["name"], a["preregistered"]) for a in block["adjustments"]] == [
        ("brief_length", True),
        ("brief_length_and_agent_type", False),
    ]
    assert all(a["n_used"] == 24 for a in block["adjustments"])
    assert all(a["n_missing_covariate"] == 0 for a in block["adjustments"])
    flat = _flat(text)
    first = flat.index(
        "Secondary (covariate-adjusted, not decisive): brief-length (pre-registered)"
    )
    second = flat.index(
        "Secondary (covariate-adjusted, not decisive): "
        "brief-length and agent type (exploratory)"
    )
    assert first < second
    assert "served minus withheld" in flat[first:second]
    assert "24 tasks used, 0 missing a covariate" in flat[second:]
    # The caveat is on the Secondary line itself, once per adjustment.
    assert flat.count("(CI ignores coefficient uncertainty)") == 2
    assert flat.index("(CI ignores coefficient uncertainty)") < flat.index(
        "adjusted: served minus withheld"
    )
    assert flat.count("unadjusted, same tasks: served minus withheld") == 2
    assert flat.count("arm balance R2") == 2
    for adjustment in block["adjustments"]:
        assert adjustment["arm_balance_r2"] is not None
        assert adjustment["unadjusted_same_subset"]["verdict"] == SECONDARY_VERDICT
        assert adjustment["inference"]["verdict"] == SECONDARY_VERDICT
        assert adjustment["inference"]["mean_served"] is None
        assert adjustment["inference"]["mean_withheld"] is None
        assert adjustment["inference"]["mean_residual_served"] is not None
        assert adjustment["unadjusted_same_subset"]["mean_served"] is not None
    # The only verdict a reader finds in the text is the primary's.
    assert flat.count("Inference:") == 1
    assert "secondary_not_decisive" not in flat
    for digest in digests:
        assert digest not in text.stdout
        assert digest not in machine.stdout
    assert sum("brief-length" in note for note in _payload(machine)["notes"]) == 1
