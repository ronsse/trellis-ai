"""Tests for the holdout analysis's operational mode.

The mode answers "is the experiment running?" and must not let its reader see
how the experiment is going, not from one report and not from the differences
between reports over several windows. Four guards, each of a different kind:

* a walker that fails on any JSON key outside a hand-written allowlist, proved
  against a report with an outcome field leaked into it;
* stores that differ only in values a treatment could have moved (outcome
  magnitudes, which arm the cut-offs and zeros fall in, tool calls, hook
  feedback, later packs), whose operational reports must be byte-identical at
  every window (the guard that does not depend on knowing which fields are
  post-treatment);
* the outcome readers and the inference stages patched to raise, so the mode
  is shown not to compute an outcome value rather than to hide one;
* an event log that raises on any read of a post-treatment key of a join.

Every id, commit and word the fixtures write is synthetic.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.unit.analyze._holdout_fixture import DAYS, MONDAY, UNTIL, HoldoutLog
from tests.unit.analyze._operational_seed import (
    COMMIT_A,
    COMMIT_B,
    TASKS,
    PostTreatment,
    seed_store,
)
from trellis.analyze import holdout
from trellis.ops.serving_builds import NO_PROVENANCE_LABEL, build_label
from trellis.stores.base.event_log import Event, EventType
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
    "settle_hours",
    "notes",
    # rows: the PACK_ASSEMBLED rows scanned, by holdout key and rate
    "rows",
    "rows.scanned",
    "rows.without_holdout_key",
    "rows.by_rate",
    "rows.at_rate",
    "rows.withheld_at_rate",
    # funnel: every stage count up to the first pack's eligibility, pooled
    "funnel",
    *_under(
        "funnel.",
        "joins_scanned sessions main_sessions no_outcome no_retrieval "
        "no_retrieval_never_called no_retrieval_errored "
        "no_retrieval_unparsed no_retrieval_empty_answer "
        "first_pack_not_found outside_window first_pack_old_build "
        "other_rate empty_first_pack unfinished pre_treatment_excluded "
        "pre_treatment_excluded_after_commit eligible pre_treatment_unknown",
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
        "eligible_tasks eligible_per_30d eligible_by_arm arm_ratio "
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
    # covariate coverage: eligible tasks carrying a covariate, with no fit
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
def make_log(tmp_path: Path) -> Iterator[Callable[..., HoldoutLog]]:
    """A factory of stores seeded with :func:`seed_store`."""
    opened: list[SQLiteEventLog] = []

    def make(magnitude: int = 1, post: PostTreatment | None = None) -> HoldoutLog:
        event_log = SQLiteEventLog(tmp_path / f"events-{len(opened)}.db")
        opened.append(event_log)
        log = HoldoutLog(event_log)
        seed_store(log, magnitude=magnitude, post=post)
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
    make_log: Callable[..., HoldoutLog],
) -> None:
    """No key outside the allowlist, and none of the allowlist goes unused."""
    payload = _operational(make_log(1), by_commit=True).model_dump()
    seen = set(_walk(payload))
    assert seen - ALLOWED - CONDITIONAL == set()
    assert (ALLOWED - seen) == set(), "allowlist names a key the report lacks"
    assert len(seen) >= 80  # a hand-read floor (81 listed): the walk visited the tree


def test_the_key_walker_flags_a_leaked_outcome_field(
    make_log: Callable[..., HoldoutLog],
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
    make_log: Callable[..., HoldoutLog], options: dict[str, Any]
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


#: Stores that differ from the default only in values a treatment could have
#: moved. Each keeps every arm, build, size, timing flag and covariate a first
#: pack settles, and which joins carry an outcome block at all.
POST_TREATMENT: dict[str, PostTreatment] = {
    "magnitudes": PostTreatment(magnitude=3),
    "cut-offs moved to the served arm": PostTreatment(cut_offs_in="served"),
    "no cut-offs": PostTreatment(cut_offs_in="none"),
    "zeros swapped between arms": PostTreatment(zeros_by_arm=True),
    "missing turns moved to a withheld task": PostTreatment(missing_turns_at=9),
    "tool calls": PostTreatment(tool_calls=400, first_retrieval_ordinal=399),
    "hook feedback": PostTreatment(hook_messages=4),
    "later packs from another build": PostTreatment(later_pack_other_build=True),
    "unparsed pack ids": PostTreatment(unparsed_pack_ids=2),
    "all at once": PostTreatment(
        magnitude=2,
        cut_offs_in="served",
        zeros_by_arm=True,
        missing_turns_at=9,
        tool_calls=3,
        first_retrieval_ordinal=1,
        hook_messages=2,
        later_pack_other_build=True,
        unparsed_pack_ids=1,
    ),
}
#: Window ends that admit the seed's tasks a few at a time.
WINDOW_ENDS = [MONDAY + timedelta(minutes=40 * step) for step in range(13)] + [
    MONDAY + timedelta(hours=9 * step) for step in range(1, 40)
]


@pytest.mark.parametrize("name", POST_TREATMENT)
def test_reports_over_stores_that_differ_in_post_treatment_values_are_identical(
    make_log: Callable[..., HoldoutLog], name: str
) -> None:
    """Two stores, one post-treatment value moved, agree at the default window."""
    base, other = make_log(), make_log(post=POST_TREATMENT[name])
    for options in VARIANTS:
        assert _bytes(_operational(base, **options)) == _bytes(
            _operational(other, **options)
        ), options
    # The knob is live: the full report over the same two stores differs.
    assert json.dumps(_full(base, by_commit=True).model_dump()) != json.dumps(
        _full(other, by_commit=True).model_dump()
    )


@pytest.mark.parametrize("name", POST_TREATMENT)
def test_differencing_windows_recovers_no_post_treatment_value_by_arm(
    make_log: Callable[..., HoldoutLog], name: str
) -> None:
    """Moving ``--until`` admits tasks one at a time; the reports must still agree.

    The deltas between two windows' reports are what a reader of stored nightly
    output has. Were any figure to move with a post-treatment value, the
    deltas of two such stores would differ at the window that admits the
    task that carries it. Byte-identity at every window rules that out.
    """
    base, other = make_log(), make_log(post=POST_TREATMENT[name])
    for end in WINDOW_ENDS:
        for options in ({}, {"by_commit": True}):
            assert _bytes(_operational(base, until=end, **options)) == _bytes(
                _operational(other, until=end, **options)
            ), (end, options)


def test_the_windows_admit_tasks_a_few_at_a_time(
    make_log: Callable[..., HoldoutLog],
) -> None:
    """The window ends are not vacuous: eligibility grows across them step by step."""
    log = make_log()
    counts = [_operational(log, until=end).funnel.eligible for end in WINDOW_ENDS]
    assert counts == sorted(counts)
    assert counts[0] == 0
    assert counts[-1] == TASKS
    assert len(set(counts)) >= 10  # a hand-read floor: 24 tasks over 52 ends


def test_outcome_and_itt_are_accepted_and_change_nothing(
    make_log: Callable[..., HoldoutLog],
) -> None:
    log = make_log()
    expected = _bytes(_operational(log, by_commit=True))
    for options in (
        {"itt": True},
        {"outcome": "commits"},
        {"outcome": "any_commit", "itt": True},
    ):
        assert _bytes(_operational(log, by_commit=True, **options)) == expected


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
        "_unit",
        "_post_hoc",
    ):
        monkeypatch.setattr(holdout, name, _raise)


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_operational_mode_calls_no_outcome_reader_or_inference_stage(
    make_log: Callable[..., HoldoutLog],
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


class _Sealed(dict[str, Any]):
    """An outcome block that raises on any read of its contents."""

    def _refuse(self, *_args: Any, **_kwargs: Any) -> Any:
        msg = "gated read: the outcome block's contents"
        raise AssertionError(msg)

    __getitem__ = get = items = keys = values = __iter__ = __contains__ = _refuse
    pop = setdefault = copy = _refuse


class _TimingGate(dict[str, Any]):
    """``retrieval_timing``, readable only at the two flags set before the arm."""

    ALLOWED = frozenset(
        {"hook_feedback_before_first_retrieval", "commit_before_first_retrieval"}
    )

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self.ALLOWED:
            msg = f"gated read: retrieval_timing[{key!r}] follows the first retrieval"
            raise AssertionError(msg)
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)


class _JoinGate(dict[str, Any]):
    """A join whose post-treatment keys raise when read."""

    def get(self, key: str, default: Any = None) -> Any:
        value = super().get(key, default)
        if key == "outcome" and isinstance(value, dict):
            return _Sealed(value)
        if key == "retrieval_timing" and isinstance(value, dict):
            return _TimingGate(value)
        if key == "pack_ids_unparsed" and super().get("pack_ids"):
            msg = "gated read: pack_ids_unparsed of a join that retrieved"
            raise AssertionError(msg)
        return value


class _GatedLog:
    """Hands the analysis every join with its post-treatment keys gated."""

    def __init__(self, inner: SQLiteEventLog) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_events(self, *args: Any, **kwargs: Any) -> list[Event]:
        events = self._inner.get_events(*args, **kwargs)
        return [
            event.model_copy(update={"payload": _JoinGate(event.payload or {})})
            if event.event_type == EventType.CAPTURE_SESSION_PACKS
            else event
            for event in events
        ]


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_operational_mode_reads_no_post_treatment_key_of_a_join(
    make_log: Callable[..., HoldoutLog], options: dict[str, Any]
) -> None:
    """The report is the same when every post-treatment read of a join raises.

    That is the outcome block's contents (its presence is readable), every
    ``retrieval_timing`` key but the two flags set before the arm, and the
    unparsed-pack-id count of a join that retrieved.
    """
    log = make_log()
    expected = _bytes(_operational(log, **options))
    gated = _GatedLog(log.event_log)
    report = holdout.analyze_holdout_operational(gated, **{**WINDOW, **options})
    assert _bytes(report) == expected
    # The gate bites: the full analysis reads those keys and trips it.
    with pytest.raises(AssertionError, match="gated read"):
        holdout.analyze_holdout(gated, **{**WINDOW, **FAST, **options})


# ---------------------------------------------------------------------------
# What the report says
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_operational_figures_equal_the_full_reports(
    make_log: Callable[..., HoldoutLog], options: dict[str, Any]
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
        "settle_hours",
        "rows",
        "scan",
    ):
        assert operational[key] == full[key], key
    # The funnel and exclusions are the full report's up to the cut-off stage.
    for key, value in operational["funnel"].items():
        assert value == full["funnel"][key], key
    assert operational["exclusions"] == full["exclusions"][:2]
    shared = operational["descriptive"].keys() - {"sessions", "not_measurable"}
    for key in shared:
        assert operational["descriptive"][key] == full["descriptive"][key], key
    sessions = operational["descriptive"]["sessions"]
    for key in sessions.keys() - {"note", "not_measurable"}:
        assert sessions[key] == full["descriptive"]["sessions"][key], key
    # Coverage is over every eligible task, where the full report's is over the
    # analysed ones, so only the names and the total can agree.
    eligible = operational["descriptive"]["eligible_tasks"]
    for item, adjustment in zip(
        operational["covariate_coverage"],
        full["covariate_adjusted"]["adjustments"],
        strict=True,
    ):
        assert item["name"] == adjustment["name"]
        assert item["preregistered"] == adjustment["preregistered"]
        assert item["n_used"] + item["n_missing_covariate"] == eligible
        assert item["n_used"] > 0
    # Notes: the full report's, less the power and covariate-fit ones, plus ours.
    # The window note names MDE horizons in the full report; ours names none.
    named = "figures per 30 days and the task MDE horizons"
    assert any(named in note for note in full["notes"])
    assert not any("MDE" in note for note in operational["notes"])
    expected = [note.replace(named, "figures per 30 days") for note in full["notes"]]
    assert operational["notes"][-1] == holdout._OPERATIONAL_NOTE
    assert all(note in expected for note in operational["notes"][:-1])
    assert holdout._OPERATIONAL_NOTE not in full["notes"]


def test_the_funnel_stops_at_eligibility_and_counts_a_missing_outcome_block(
    make_log: Callable[..., HoldoutLog],
) -> None:
    """24 tasks are eligible; the one join without an outcome block is dropped first."""
    funnel = _operational(make_log()).funnel
    assert funnel.eligible == TASKS
    assert funnel.no_outcome == 1
    assert {
        "cut_offs_excluded",
        "outcome_missing",
        "analysed",
        "post_hoc_measured",
        "post_hoc_after_work",
        "pre_treatment_hook_feedback_seen",
        "eligible_with_unparsed_pack_ids",
    }.isdisjoint(funnel.model_dump())


def test_by_commit_is_counts_of_eligible_tasks_and_absent_unless_asked(
    make_log: Callable[..., HoldoutLog],
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


#: Names of the figures the mode does not carry: each follows the first pack.
REMOVED = (
    "cut_off",
    "cut-off",
    "outcome_missing",
    "analysed",
    "packs_per_task",
    "tasks_with_several_packs",
    "tasks_with_both_arms",
    "post_hoc",
    "pre_treatment_hook_feedback_seen",
    "eligible_with_unparsed_pack_ids",
    "units_spanning_builds",
)


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_no_figure_that_follows_the_first_pack_is_named_in_the_report(
    make_log: Callable[..., HoldoutLog], options: dict[str, Any]
) -> None:
    """Neither a key nor a note names a cut-off, analysed or later-pack figure."""
    dumped = json.dumps(_operational(make_log(), **options).model_dump())
    assert [name for name in REMOVED if name in dumped] == []


def test_options_are_validated_as_the_full_analysis_does(
    make_log: Callable[..., HoldoutLog],
) -> None:
    log = make_log(1)
    for options in (
        {"days": 0},
        {"outcome": "nonsense"},
        {"rate": 2.0},
        {"permutations": 0},
        {"bootstraps": 0},
        {"power_sims": 0},
        {"planned_mde": 0},
        {"planned_n": 0},
        {"seed": -1},
    ):
        with pytest.raises(holdout.HoldoutAnalysisError):
            _operational(log, **options)
        with pytest.raises(holdout.HoldoutAnalysisError):
            _full(log, **options)
    with pytest.raises(holdout.HoldoutAnalysisError, match="settle-hours"):
        _operational(log, settle_hours=-1.0)
