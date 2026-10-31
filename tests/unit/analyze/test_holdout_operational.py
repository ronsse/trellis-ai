"""Tests for the holdout analysis's operational mode.

The mode answers "is the experiment running?" and must not let its reader see
how the experiment is going. Three guards, each of a different kind:

* a walker that fails on any JSON key outside a hand-written allowlist, proved
  against a report with an outcome field leaked into it;
* two stores that differ only in outcome magnitudes, whose operational reports
  must be byte-identical (the guard that does not depend on knowing which
  fields are outcome-derived);
* the outcome readers and the inference stages patched to raise, so the mode
  is shown not to compute an outcome value rather than to hide one.

Every id, commit and word the fixtures write is synthetic.
"""

from __future__ import annotations

import copy
import itertools
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.unit.analyze._holdout_fixture import DAYS, UNTIL, HoldoutLog
from tests.unit.analyze._operational_seed import COMMIT_A, COMMIT_B, TASKS, seed_store
from trellis.analyze import holdout
from trellis.ops.serving_builds import NO_PROVENANCE_LABEL, build_label
from trellis.stores.sqlite.event_log import SQLiteEventLog

WINDOW: dict[str, Any] = {"days": DAYS, "until": UNTIL}
FAST: dict[str, Any] = {
    "seed": 7,
    "permutations": 499,
    "bootstraps": 200,
    "power_sims": 20,
}

#: Option sets the mode is exercised under: every outcome, ITT, and the table.
VARIANTS: list[dict[str, Any]] = [
    {},
    {"itt": True},
    {"by_commit": True},
    {"outcome": "prs_created", "by_commit": True},
    {"outcome": "log1p_tokens", "itt": True},
    {"outcome": "commits", "by_commit": True, "itt": True},
    {"outcome": "log1p_commits"},
    {"outcome": "any_commit", "by_commit": True},
]


# ---------------------------------------------------------------------------
# The allowlist: every key the operational report may carry, by path
# ---------------------------------------------------------------------------

#: Paths of free-keyed maps (their keys are labels, not fields): the walker
#: allows any key under them and requires scalar values.
FREE_KEYED = {
    "rows.by_rate",
    "descriptive.not_measurable",
    "descriptive.sessions.not_measurable",
}


def _under(prefix: str, names: str) -> list[str]:
    """``prefix`` joined to each space-separated name."""
    return [prefix + name for name in names.split()]


#: Every other allowed path. A list item's keys sit under ``[]``.
ALLOWED = {
    "status",
    "mode",
    "window_days",
    "effective_window_days",
    "since",
    "until",
    "task_window_since",
    "task_window_days",
    "rate",
    "outcome",
    "itt",
    "settle_hours",
    "notes",
    # rows: the PACK_ASSEMBLED rows scanned, by holdout key and rate
    "rows",
    "rows.scanned",
    "rows.without_holdout_key",
    "rows.by_rate",
    "rows.at_rate",
    "rows.withheld_at_rate",
    # funnel: every stage count, pooled over both arms
    "funnel",
    *_under(
        "funnel.",
        "joins_scanned sessions main_sessions no_outcome no_retrieval "
        "no_retrieval_never_called no_retrieval_errored "
        "no_retrieval_unparsed no_retrieval_empty_answer "
        "first_pack_not_found outside_window first_pack_old_build "
        "other_rate empty_first_pack unfinished pre_treatment_excluded "
        "pre_treatment_excluded_after_commit eligible pre_treatment_unknown "
        "pre_treatment_hook_feedback_seen post_hoc_measured "
        "post_hoc_after_work cut_offs_excluded outcome_missing analysed "
        "eligible_with_unparsed_pack_ids",
    ),
    # exclusions: what each rule removed, pooled
    "exclusions",
    *_under(
        "exclusions[].",
        "name timing applied excluded note unknown",
    ),
    # descriptive: counts, volumes and the arm-ratio checks
    "descriptive",
    *_under(
        "descriptive.",
        "eligible_tasks eligible_per_30d analysed_tasks analysed_per_30d "
        "packs_per_task tasks_with_several_packs post_hoc_share "
        "post_hoc_note eligible_by_arm tasks_with_both_arms arm_ratio "
        "top_parent_share sessions not_measurable",
    ),
    "descriptive.eligible_by_arm.served",
    "descriptive.eligible_by_arm.withheld",
    *_under(
        "descriptive.arm_ratio[].",
        "population n withheld expected_share p_value",
    ),
    *_under(
        "descriptive.sessions.",
        "rolled_up not_finished without_main_join reaching_retrieval "
        "reaching_retrieval_per_30d eligible eligible_per_30d note "
        "not_measurable",
    ),
    # covariate coverage: counts of tasks carrying a covariate, with no fit
    "covariate_coverage",
    *_under(
        "covariate_coverage[].",
        "name preregistered n_used n_missing_covariate",
    ),
    # by_commit: counts of eligible tasks per build and arm
    "by_commit",
    "by_commit.rows",
    *_under(
        "by_commit.rows[].",
        "build first_seen last_seen served withheld",
    ),
    "by_commit.units_spanning_builds",
    "by_commit.note",
    # scan: how much of the window the read covered
    "scan",
    *_under(
        "scan.",
        "limit scanned matched dropped truncated truncated_event_types "
        "covered_since note",
    ),
}

#: Allowed paths a report need not carry: free-keyed map entries.
CONDITIONAL = {f"{path}.*" for path in FREE_KEYED}


def _walk(node: Any, path: str = "") -> Iterator[str]:
    """Every key path in a JSON value; a free-keyed map yields ``<path>.*``."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if path in FREE_KEYED:
                assert not isinstance(value, dict | list), f"{child} is not scalar"
                yield f"{path}.*"
                continue
            yield child
            yield from _walk(value, child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, f"{path}[]")


def _outside_allowlist(payload: dict[str, Any]) -> set[str]:
    return {path for path in _walk(payload) if path not in ALLOWED | CONDITIONAL}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_log(tmp_path: Path) -> Iterator[Callable[[int], HoldoutLog]]:
    """A factory of stores seeded with :func:`seed_store` at a given magnitude."""
    opened: list[SQLiteEventLog] = []

    def make(magnitude: int) -> HoldoutLog:
        event_log = SQLiteEventLog(tmp_path / f"events-{len(opened)}.db")
        opened.append(event_log)
        log = HoldoutLog(event_log)
        seed_store(log, magnitude=magnitude)
        return log

    yield make
    for event_log in opened:
        event_log.close()


def _operational(log: HoldoutLog, **options: Any) -> holdout.HoldoutOperationalReport:
    return holdout.analyze_holdout_operational(log.event_log, **{**WINDOW, **options})


def _full(log: HoldoutLog, **options: Any) -> holdout.HoldoutReport:
    return holdout.analyze_holdout(log.event_log, **{**WINDOW, **FAST, **options})


def _bytes(report: holdout.HoldoutOperationalReport) -> str:
    return json.dumps(report.model_dump(), sort_keys=False)


# ---------------------------------------------------------------------------
# Guard 1: the key allowlist
# ---------------------------------------------------------------------------


def test_every_key_is_allowlisted_and_the_allowlist_is_not_stale(
    make_log: Callable[[int], HoldoutLog],
) -> None:
    """No key outside the allowlist, and none of the allowlist goes unused."""
    payload = _operational(make_log(1), by_commit=True).model_dump()
    seen = set(_walk(payload))
    assert seen - ALLOWED - CONDITIONAL == set()
    assert (ALLOWED - seen) == set(), "allowlist names a key the report lacks"
    assert len(seen) > 90  # a hand-read floor: the walk did visit the tree


def test_the_key_walker_flags_a_leaked_outcome_field(
    make_log: Callable[[int], HoldoutLog],
) -> None:
    """Mutants: an outcome figure added at top level, nested, and in a list item."""
    clean = _operational(make_log(1), by_commit=True).model_dump()
    assert _outside_allowlist(clean) == set()

    top = copy.deepcopy(clean)
    top["inference"] = {"difference": 0.4}
    nested = copy.deepcopy(clean)
    nested["descriptive"]["outcome_sd_total"] = 0.4
    in_list = copy.deepcopy(clean)
    in_list["by_commit"]["rows"][0]["served"] = {"n": 3, "outcome_mean": 1.5}
    in_map = copy.deepcopy(clean)
    in_map["descriptive"]["not_measurable"]["leak"] = {"mean": 1.5}

    assert "inference" in _outside_allowlist(top)
    assert "descriptive.outcome_sd_total" in _outside_allowlist(nested)
    assert "by_commit.rows[].served.outcome_mean" in _outside_allowlist(in_list)
    with pytest.raises(AssertionError, match="not scalar"):
        _outside_allowlist(in_map)


def test_the_report_model_has_no_field_for_an_outcome_figure() -> None:
    """A leak needs a new field: ``extra="forbid"`` refuses a stray one."""
    fields = set(holdout.HoldoutOperationalReport.model_fields)
    assert fields.isdisjoint(
        {"seed", "inference", "power", "guardrails", "covariate_adjusted"}
    )
    with pytest.raises(ValueError, match="outcome_mean"):
        holdout.HoldoutBuildCountRow(
            build="b",
            first_seen="x",
            last_seen="y",
            served=1,
            withheld=1,
            outcome_mean=1.0,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Guard 2: outcomes differing, the report not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_reports_over_stores_that_differ_only_in_outcomes_are_byte_identical(
    make_log: Callable[[int], HoldoutLog], options: dict[str, Any]
) -> None:
    """The real guard: no figure in the report moves with an outcome magnitude."""
    small, large = make_log(1), make_log(3)
    assert _bytes(_operational(small, **options)) == _bytes(
        _operational(large, **options)
    )
    # The knob is live: the full report over the same two stores differs.
    assert json.dumps(_full(small, **options).model_dump()) != json.dumps(
        _full(large, **options).model_dump()
    )


# ---------------------------------------------------------------------------
# Guard 3: the outcome is never computed
# ---------------------------------------------------------------------------


def _raise(*_args: Any, **_kwargs: Any) -> Any:
    msg = "an outcome-derived value was computed"
    raise AssertionError(msg)


def _forbid_outcome_computation(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in holdout.OUTCOMES:
        monkeypatch.setitem(holdout.OUTCOMES, name, _raise)
    for name in (
        "_log1p_count",
        "_log1p_tokens",
        "_any_commit",
        "_rolled_outcome",
        "_descriptive",
        "_inference",
        "_covariate_adjusted",
        "_power",
        "_guardrails",
        "_by_commit",
        "_report",
    ):
        monkeypatch.setattr(holdout, name, _raise)


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_operational_mode_calls_no_outcome_reader_or_inference_stage(
    make_log: Callable[[int], HoldoutLog],
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
) -> None:
    log = make_log(1)
    expected = _bytes(_operational(log, **options))
    _forbid_outcome_computation(monkeypatch)
    assert _bytes(_operational(log, **options)) == expected
    # The patch bites: the full analysis trips it at once.
    with pytest.raises(AssertionError, match="outcome-derived"):
        _full(log, **options)


_VALUES: list[Any] = [None, True, False, -1, 0, 3, 2.5, float("nan"), float("inf")]
_VALUES += ["7", [], {}]


def _grid(fields: tuple[str, ...]) -> Iterator[dict[str, Any]]:
    """Outcomes over every absent-or-value choice for each of ``fields``."""
    absent = object()
    choices = [absent, None, True, -1, 0, 5, float("nan"), "5"]
    for combo in itertools.product(choices, repeat=len(fields)):
        yield {f: v for f, v in zip(fields, combo, strict=True) if v is not absent}


def test_outcome_presence_agrees_with_every_reader() -> None:
    """``_outcome_present`` is ``reader(outcome) is not None`` on a grid of values."""
    assert set(holdout._OUTCOME_FIELDS) == set(holdout.OUTCOMES)
    checked = 0
    for name, reader in holdout.OUTCOMES.items():
        fields = holdout._OUTCOME_FIELDS[name]
        for outcome in _grid(fields):
            assert holdout._outcome_present(name, outcome) == (
                reader(outcome) is not None
            ), (name, outcome)
            checked += 1
        for value in _VALUES:
            outcome = dict.fromkeys(fields, value)
            assert holdout._outcome_present(name, outcome) == (
                reader(outcome) is not None
            ), (name, outcome)
            checked += 1
    assert checked > 700  # hand-read floor: the token outcome alone is 8**4


def test_outcome_presence_reads_no_field_outside_its_map() -> None:
    """A field the map does not name cannot make an outcome present."""
    others = {"commits": 4, "prs_created": 2, "tool_errors": 1, "ended_on_error": True}
    assert not holdout._outcome_present("log1p_turns", others)
    assert not holdout._outcome_present("log1p_tokens", others)


# ---------------------------------------------------------------------------
# What the report says
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_operational_figures_equal_the_full_reports(
    make_log: Callable[[int], HoldoutLog], options: dict[str, Any]
) -> None:
    """Each shared figure is the one the full report prints, to the byte."""
    log = make_log(2)
    operational = _operational(log, **options).model_dump()
    full = _full(log, **options).model_dump()
    for key in (
        "status",
        "window_days",
        "effective_window_days",
        "since",
        "until",
        "task_window_since",
        "task_window_days",
        "rate",
        "outcome",
        "itt",
        "settle_hours",
        "rows",
        "funnel",
        "exclusions",
        "scan",
    ):
        assert operational[key] == full[key], key
    shared = operational["descriptive"].keys() - {"sessions", "not_measurable"}
    for key in shared:
        assert operational["descriptive"][key] == full["descriptive"][key], key
    sessions = operational["descriptive"]["sessions"]
    for key in sessions.keys() - {"note", "not_measurable"}:
        assert sessions[key] == full["descriptive"]["sessions"][key], key
    for item, adjustment in zip(
        operational["covariate_coverage"],
        full["covariate_adjusted"]["adjustments"],
        strict=True,
    ):
        assert item == {
            "name": adjustment["name"],
            "preregistered": adjustment["preregistered"],
            "n_used": adjustment["n_used"],
            "n_missing_covariate": adjustment["n_missing_covariate"],
        }
    # Notes: the full report's, less the power and covariate-fit ones, plus ours.
    # The window note names MDE horizons in the full report; ours names none.
    named = "figures per 30 days and the task MDE horizons"
    assert any(named in note for note in full["notes"])
    assert not any("MDE" in note for note in operational["notes"])
    expected = [note.replace(named, "figures per 30 days") for note in full["notes"]]
    assert operational["notes"][-1] == holdout._OPERATIONAL_NOTE
    assert all(note in expected for note in operational["notes"][:-1])
    assert holdout._OPERATIONAL_NOTE not in full["notes"]


def test_the_funnel_counts_a_task_as_analysed_by_presence_alone(
    make_log: Callable[[int], HoldoutLog],
) -> None:
    """Of 24 eligible tasks, 4 cut-offs and 1 lacking its field leave 19."""
    funnel = _operational(make_log(1)).funnel
    assert funnel.eligible == TASKS
    assert funnel.cut_offs_excluded == 4
    assert funnel.outcome_missing == 1
    assert funnel.analysed == TASKS - 5
    itt = _operational(make_log(1), itt=True).funnel
    assert (itt.cut_offs_excluded, itt.outcome_missing, itt.analysed) == (0, 1, 23)


def test_by_commit_is_counts_of_eligible_tasks_and_absent_unless_asked(
    make_log: Callable[[int], HoldoutLog],
) -> None:
    log = make_log(1)
    assert "by_commit" not in _operational(log).model_dump()
    table = _operational(log, by_commit=True).by_commit
    assert table is not None
    # The sub-task the seed adds after the loop carries no stamp, so it forms a
    # build of its own, first seen between the two stamped ones.
    assert [row.build for row in table.rows] == [
        build_label("0.0.0", COMMIT_A),
        NO_PROVENANCE_LABEL,
        build_label("0.0.0", COMMIT_B),
    ]
    served = sum(row.served for row in table.rows)
    withheld = sum(row.withheld for row in table.rows)
    report = _operational(log, by_commit=True)
    arms = report.descriptive.eligible_by_arm
    assert (served, withheld) == (arms.served, arms.withheld)
    for row in table.rows:
        assert set(row.model_dump()) == {
            "build",
            "first_seen",
            "last_seen",
            "served",
            "withheld",
        }
        assert isinstance(row.served, int)
        assert isinstance(row.withheld, int)
    assert table.units_spanning_builds == 0


def test_no_two_figures_subtract_to_a_cut_off_count_by_arm(
    make_log: Callable[[int], HoldoutLog],
) -> None:
    """Only withheld tasks are cut off here, so an arm split of cut-offs would show.

    Counts per arm are over eligible tasks, which include the cut-offs, and
    the analysed count is pooled; the full report's analysed split is what
    would reveal the cut-offs by arm.
    """
    log = make_log(1)
    report = _operational(log, by_commit=True)
    full = _full(log)
    assert full.descriptive.cut_offs_by_arm.served == 0
    assert full.descriptive.cut_offs_by_arm.withheld == 4
    arms = report.descriptive.eligible_by_arm
    assert arms.withheld == full.descriptive.eligible_by_arm.withheld
    dumped = json.dumps(report.model_dump())
    assert "analysed_by_arm" not in dumped
    assert "cut_offs_by_arm" not in dumped


def test_options_are_validated_as_the_full_analysis_does(
    make_log: Callable[[int], HoldoutLog],
) -> None:
    log = make_log(1)
    for options in ({"days": 0}, {"outcome": "nonsense"}, {"rate": 2.0}):
        with pytest.raises(holdout.HoldoutAnalysisError):
            _operational(log, **options)
        with pytest.raises(holdout.HoldoutAnalysisError):
            _full(log, **options)
    with pytest.raises(holdout.HoldoutAnalysisError, match="settle-hours"):
        _operational(log, settle_hours=-1.0)
