"""Tests for the holdout analysis's operational mode.

The mode answers "is the experiment running?" and must not let its reader see
how the experiment is going, not from one report and not from the differences
between reports over several windows. Five guards, each of a different kind:

* a walker that fails on any JSON key outside a hand-written allowlist, proved
  against a report with an outcome field leaked into it;
* stores that differ only in values a treatment could have moved (outcome
  magnitudes, which arm the cut-offs and zeros fall in, tool calls, hook
  feedback, later packs at any arm, build or rate, pack rows no join names,
  a join written again with other timing flags, covariates and first pack),
  whose operational reports must be byte-identical at every window, with no
  rate named and with one named (the guard that does not depend on knowing
  which fields are post-treatment);
* the outcome readers and the inference stages patched to raise, so the mode
  is shown not to compute an outcome value rather than to hide one;
* an event log that raises on any read of a post-treatment key of a join, by
  subscript, iteration or copy as much as by ``.get``;
* an event log that raises when the mode reads the pack stream in any way but
  one join's first pack by id, so a later pack, the number of packs and the
  volume of pack rows cannot reach a figure;
* one store grown night by night, whose stored reports must equal a retro
  ``--until`` report and must not differ between stores that differ only in
  post-treatment values.

The rule they pin: the mode reads the joins, the sweeps and each join's first
pack, and nothing else from the pack stream. The one disclosed exception is
whether a join carries an outcome block, which the fixtures do not move.

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

from tests.unit.analyze._holdout_fixture import (
    DAYS,
    MONDAY,
    UNTIL,
    HoldoutLog,
    outcome_payload,
    provenance,
)
from tests.unit.analyze._operational_seed import (
    COMMIT_A,
    COMMIT_B,
    TASKS,
    WINDOW_ENDS,
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
    "since",
    "until",
    "task_window_since",
    "task_window_days",
    "rate",
    "settle_hours",
    "notes",
    # funnel: the sub-agent joins admitted on their first pack, by stage
    "funnel",
    *_under(
        "funnel.",
        "no_outcome no_retrieval no_retrieval_never_called "
        "no_retrieval_errored no_retrieval_unparsed no_retrieval_empty_answer "
        "first_pack_not_found first_pack_old_build other_rate "
        "empty_first_pack pre_treatment_excluded "
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
        "with_first_pack with_first_pack_per_30d eligible eligible_per_30d "
        "note not_measurable",
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
}

#: Allowed paths a report need not carry: free-keyed map entries.
CONDITIONAL = {f"{path}.*" for path in FREE_KEYED}


#: A hand-read count of the operational report's key paths (66), less slack.
FLOOR = 64


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
    assert len(seen) >= FLOOR  # a hand-read floor: the walk visited the tree


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
    "later packs in the same arm": PostTreatment(
        later_packs_everywhere=True, later_pack_same_arm=True
    ),
    "later packs in the other arm": PostTreatment(later_packs_everywhere=True),
    "more packs for the withheld arm": PostTreatment(extra_packs=(0, 5)),
    "more packs for the served arm": PostTreatment(extra_packs=(5, 0)),
    "later packs at another rate": PostTreatment(
        later_packs_everywhere=True, later_pack_rate=0.25
    ),
    "later packs from a build before the holdout": PostTreatment(
        later_packs_everywhere=True, later_pack_unkeyed=True
    ),
    "later packs in the same arm and an unkeyed stream": PostTreatment(
        later_packs_everywhere=True,
        later_pack_same_arm=True,
        later_pack_unkeyed=True,
        stray_rows="unkeyed",
    ),
    "rate 0 rows from another surface": PostTreatment(stray_rows="rate0"),
    "unkeyed rows from another build": PostTreatment(stray_rows="unkeyed"),
    "both kinds of stray row": PostTreatment(stray_rows="both"),
    "withheld joins written again with other fixed fields": PostTreatment(
        reemit_in="withheld", reemit_pretreatment=True
    ),
    "served joins written again with other fixed fields": PostTreatment(
        reemit_in="served", reemit_pretreatment=True
    ),
    "joins written later for the withheld arm": PostTreatment(
        join_lag_minutes=(10, 25)
    ),
    "joins written later for the served arm": PostTreatment(join_lag_minutes=(25, 10)),
    "withheld joins written again": PostTreatment(reemit_in="withheld"),
    "served joins written again": PostTreatment(reemit_in="served"),
    "interrupted in the withheld arm": PostTreatment(interrupted_in="withheld"),
    "interrupted in the served arm": PostTreatment(interrupted_in="served"),
    "all at once": PostTreatment(
        magnitude=2,
        cut_offs_in="served",
        zeros_by_arm=True,
        missing_turns_at=9,
        tool_calls=3,
        first_retrieval_ordinal=1,
        hook_messages=2,
        later_pack_other_build=True,
        later_packs_everywhere=True,
        later_pack_rate=0.25,
        unparsed_pack_ids=1,
        extra_packs=(2, 3),
        join_lag_minutes=(25, 12),
        reemit_in="all",
        reemit_pretreatment=True,
        interrupted_in="all",
        stray_rows="both",
    ),
}
#: Knobs the full report cannot see at its default window: where a join is
#: written, and whether it is written again, move only the join's own time.
NO_FULL_SIGNAL = {
    "joins written later for the withheld arm",
    "joins written later for the served arm",
    "interrupted in the withheld arm",
    "interrupted in the served arm",
}
#: With no rate named, and with the one the first packs were written at.
RATES: tuple[dict[str, Any], ...] = ({}, {"rate": 0.5})


def _full_dump(log: HoldoutLog, **options: Any) -> str:
    """The full report's bytes, or its refusal: either differs between stores."""
    try:
        return json.dumps(_full(log, **options).model_dump())
    except holdout.HoldoutAnalysisError as exc:
        return f"refused: {exc}"


#: A sweep settles a task 30 minutes after its first pack, so each window end in
#: :data:`WINDOW_ENDS` admits the tasks whose first pack is 30 minutes older.
NIGHT: dict[str, Any] = {"settle_hours": 0.5}
#: Window lengths other than the default, short enough that ``since`` crosses
#: events.
SHORT_DAYS = (4, 1)


@pytest.mark.parametrize("name", POST_TREATMENT)
def test_reports_over_stores_that_differ_in_post_treatment_values_are_identical(
    make_log: Callable[..., HoldoutLog], name: str
) -> None:
    """Two stores, one post-treatment value moved, agree at the default window."""
    base, other = make_log(), make_log(post=POST_TREATMENT[name])
    for options in VARIANTS:
        for rate in RATES:
            assert _bytes(_operational(base, **options, **rate)) == _bytes(
                _operational(other, **options, **rate)
            ), (options, rate)
    # The knob is live: the full report over the same two stores differs.
    if name not in NO_FULL_SIGNAL:
        assert _full_dump(base, by_commit=True) != _full_dump(other, by_commit=True)


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
            for rate in RATES:
                same = {"until": end, **NIGHT, **options, **rate}
                assert _bytes(_operational(base, **same)) == _bytes(
                    _operational(other, **same)
                ), (end, options, rate)
    # A shorter ``--days`` moves ``since`` across the events too.
    for end in WINDOW_ENDS[::3]:
        for days in SHORT_DAYS:
            for rate in RATES:
                same = {"until": end, "days": days, **NIGHT, **rate}
                assert _bytes(_operational(base, **same)) == _bytes(
                    _operational(other, **same)
                ), (end, days, rate)


def _grow_nightly(
    source: HoldoutLog, path: Path, nights: list[Any], **extra: Any
) -> list[str]:
    """The reports a store grown event by event gives, one per night.

    Each night the store holds what was written before it and nothing later,
    as a stored nightly report was computed. The same night's retro
    ``--until`` report over the finished store must be those bytes.
    """
    events = sorted(
        source.event_log.get_events(order="asc", limit=100_000),
        key=lambda event: event.occurred_at,
    )
    grown = SQLiteEventLog(path)
    reports: list[str] = []
    taken = 0
    try:
        for night in nights:
            while taken < len(events) and events[taken].occurred_at < night:
                grown.append(events[taken])
                taken += 1
            options = {**WINDOW, **NIGHT, "until": night, "by_commit": True, **extra}
            stored = _bytes(holdout.analyze_holdout_operational(grown, **options))
            assert stored == _bytes(
                holdout.analyze_holdout_operational(source.event_log, **options)
            ), night
            reports.append(stored)
    finally:
        grown.close()
    return reports


@pytest.mark.parametrize("name", POST_TREATMENT)
def test_a_store_grown_night_by_night_gives_the_same_reports_whatever_follows(
    make_log: Callable[..., HoldoutLog], tmp_path: Path, name: str
) -> None:
    """Stored nightly reports are byte-identical across post-treatment values."""
    nights = WINDOW_ENDS[::2]
    for number, rate in enumerate(RATES):
        base = _grow_nightly(
            make_log(), tmp_path / f"grown-base-{number}.db", nights, **rate
        )
        other = _grow_nightly(
            make_log(post=POST_TREATMENT[name]),
            tmp_path / f"grown-other-{number}.db",
            nights,
            **rate,
        )
        assert base == other, rate
        assert len(set(base)) >= 10  # a hand-read floor: eligibility grows 10+ times


def test_a_task_is_admitted_on_its_first_pack_and_the_full_report_on_its_latest_join(
    tmp_path: Path,
) -> None:
    """One task, joined again after a sweep: the two reports admit it differently."""
    event_log = SQLiteEventLog(tmp_path / "differ.db")
    log = HoldoutLog(event_log)
    try:
        start = MONDAY
        pack = log.pack(
            at=start, withheld=False, items=3, metadata=provenance(COMMIT_A)
        )
        for hours in (1, 3):
            log.join(
                "task-000",
                at=start + timedelta(hours=hours),
                pack_ids=[pack],
                parent="parent-a",
                outcome=outcome_payload(turns=5),
            )
        log.sweep(at=start + timedelta(hours=4))
        window = {"days": 3, "until": start + timedelta(days=1), "settle_hours": 3.0}
        operational = holdout.analyze_holdout_operational(event_log, **window)
        full = holdout.analyze_holdout(event_log, **window, **FAST)
    finally:
        event_log.close()
    assert operational.funnel.eligible == 1
    assert full.funnel.eligible == 0
    assert full.funnel.unfinished == 1
    assert any("first pack" in note for note in operational.notes)


def test_the_windows_admit_tasks_a_few_at_a_time(
    make_log: Callable[..., HoldoutLog],
) -> None:
    """The window ends are not vacuous: eligibility grows across them step by step."""
    log = make_log()
    counts = [
        _operational(log, until=end, **NIGHT).funnel.eligible for end in WINDOW_ENDS
    ]
    assert counts == sorted(counts)
    assert counts[0] == 0
    assert counts[-1] == TASKS
    assert len(set(counts)) >= 10  # a hand-read floor: 12 first packs over 13 ends


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
        # The full report's scans, join-time admission and roll-up: never this mode's.
        "_gather",
        "_latest_joins",
        "_find_pack",
        "_task_window_start",
        "_notes",
        "_arrival",
        "_finished",
        "_tally_sessions",
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


def _refuse(self: Any, *_args: Any, **_kwargs: Any) -> Any:
    msg = f"gated read: {type(self).__name__} was read past what the first pack fixes"
    raise AssertionError(msg)


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

    def __contains__(self, key: object) -> bool:
        return self.get(str(key), None) is not None

    items = keys = values = __iter__ = copy = _refuse  # type: ignore[assignment]


class _PackIds(list[Any]):
    """A join's ``pack_ids``, readable at index 0 and nowhere else."""

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, int) and index == 0:
            return super().__getitem__(0)
        msg = "gated read: pack_ids past the first pack"
        raise AssertionError(msg)

    __iter__ = __len__ = __reversed__ = __contains__ = _refuse  # type: ignore[assignment]
    index = count = copy = _refuse  # type: ignore[assignment]


class _JoinGate(dict[str, Any]):
    """A join whose post-treatment keys raise when read, however they are read."""

    def get(self, key: str, default: Any = None) -> Any:
        value = super().get(key, default)
        if key == "outcome" and isinstance(value, dict):
            return _Sealed(value)
        if key == "retrieval_timing" and isinstance(value, dict):
            return _TimingGate(value)
        if key == "pack_ids" and isinstance(value, list):
            return _PackIds(value)
        if key == "pack_ids_unparsed" and super().get("pack_ids"):
            msg = "gated read: pack_ids_unparsed of a join that retrieved"
            raise AssertionError(msg)
        return value

    def __getitem__(self, key: str) -> Any:
        if key not in self:
            raise KeyError(key)
        return self.get(key)

    items = keys = values = __iter__ = copy = _refuse  # type: ignore[assignment]


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
    ``retrieval_timing`` key but the two flags set before the arm, every pack
    id after the first, and the unparsed-pack-id count of a join that
    retrieved. Subscripts, iteration, ``dict()`` and ``len`` raise as ``.get``
    does, so a read that skips ``.get`` is caught.
    """
    log = make_log()
    expected = _bytes(_operational(log, **options))
    gated = _GatedLog(log.event_log)
    report = holdout.analyze_holdout_operational(gated, **{**WINDOW, **options})
    assert _bytes(report) == expected
    for end in WINDOW_ENDS[::6]:
        assert _bytes(
            holdout.analyze_holdout_operational(
                gated, **{**WINDOW, **NIGHT, **options, "until": end}
            )
        ) == _bytes(_operational(log, until=end, **NIGHT, **options)), end
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
        "since",
        "until",
        "task_window_since",
        "task_window_days",
        "rate",
        "settle_hours",
    ):
        assert operational[key] == full[key], key
    # The report carries no scan: it pages the joins and sweeps to completion.
    assert {"scan", "effective_window_days"}.isdisjoint(operational)
    # Everything is swept and nothing is joined twice at this window, so the two
    # admissions agree here; they part when a join is written again.
    for key, value in operational["funnel"].items():
        assert value == full["funnel"][key], key
    assert operational["exclusions"] == full["exclusions"][:2]
    shared = {"eligible_tasks", "eligible_per_30d", "eligible_by_arm"}
    shared |= {"top_parent_share"}
    for key in shared:
        assert operational["descriptive"][key] == full["descriptive"][key], key
    tasks = [
        c for c in operational["descriptive"]["arm_ratio"] if c["population"] == "tasks"
    ]
    assert tasks == [
        c for c in full["descriptive"]["arm_ratio"] if c["population"] == "tasks"
    ]
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
    # Notes: ours end with the operational note, which names the structure.
    assert operational["notes"][-1] == holdout._OPERATIONAL_NOTE
    assert not any("MDE" in note for note in operational["notes"])
    note = holdout._OPERATIONAL_NOTE
    assert "does not scan PACK_ASSEMBLED" in note
    assert "can differ from the full report's" in note
    assert "One disclosed exception" in note
    assert note not in full["notes"]


def test_the_funnel_stops_at_eligibility_and_counts_a_missing_outcome_block(
    make_log: Callable[..., HoldoutLog],
) -> None:
    """24 tasks are eligible; the one join without an outcome block is dropped first."""
    funnel = _operational(make_log()).funnel
    assert funnel.eligible == TASKS
    assert funnel.no_outcome == 1
    assert {
        "joins_scanned",
        "sessions",
        "main_sessions",
        "outside_window",
        "unfinished",
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


def test_the_arm_ratio_runs_on_first_packs_and_no_pack_row_count_is_reported(
    make_log: Callable[..., HoldoutLog],
) -> None:
    """``assigned`` is one first pack per join at the rate; ``rows`` is gone."""
    report = _operational(make_log(), by_commit=True)
    dumped = report.model_dump()
    assert "rows" not in dumped
    assert {"scan", "effective_window_days"}.isdisjoint(dumped)
    checks = report.descriptive.arm_ratio
    assert [c.population for c in checks[:2]] == ["assigned", "tasks"]
    assert all(c.population.startswith("assigned 20") for c in checks[2:])
    # 24 eligible tasks, the sub-agent join with no outcome block, the main one.
    assert checks[0].n == TASKS + 2
    assert sum(c.n for c in checks[2:]) == checks[0].n
    assert checks[1].n == TASKS


def test_main_transcripts_are_counted_on_their_own_first_pack(
    make_log: Callable[..., HoldoutLog],
) -> None:
    sessions = _operational(make_log()).descriptive.sessions
    assert sessions.with_first_pack == 1
    assert sessions.eligible == 1
    assert set(sessions.model_dump()).isdisjoint(
        {"rolled_up", "not_finished", "without_main_join", "reaching_retrieval"}
    )


# ---------------------------------------------------------------------------
# Guard 5: the pack stream is never scanned
# ---------------------------------------------------------------------------


def _first_pack_ids(event_log: SQLiteEventLog) -> set[str]:
    """The first pack id of each session's earliest join: the only packs to read."""
    earliest: dict[str, str] = {}
    for join in sorted(
        event_log.get_events(
            event_type=EventType.CAPTURE_SESSION_PACKS, order="asc", limit=100_000
        ),
        key=lambda event: event.occurred_at,
    ):
        ids = (join.payload or {}).get("pack_ids") or []
        if join.entity_id and ids:
            earliest.setdefault(join.entity_id, ids[0])
    return set(earliest.values())


class _FirstPackOnlyLog:
    """An event log that refuses any read of the pack stream but one first pack by id.

    A query for pack rows by type, with no id or with one that no session's
    earliest join names first, raises, as does a count or a read by event id,
    so a later pack, the number of packs and the volume of pack rows cannot
    reach a figure by any route the log offers.
    """

    def __init__(self, inner: SQLiteEventLog) -> None:
        self._inner = inner
        self.allowed = _first_pack_ids(inner)
        self.fetched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_events(self, *args: Any, **kwargs: Any) -> list[Event]:
        event_type = kwargs.get("event_type")
        if event_type is None or event_type == EventType.PACK_ASSEMBLED:
            entity = kwargs.get("entity_id")
            if event_type is None or entity not in self.allowed:
                msg = f"pack stream read: {event_type} for entity {entity!r}"
                raise AssertionError(msg)
            self.fetched.append(entity)
        return self._inner.get_events(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        msg = "pack stream read: a count"
        raise AssertionError(msg)

    def get_event(self, *args: Any, **kwargs: Any) -> Event | None:
        msg = "pack stream read: an event by id"
        raise AssertionError(msg)


#: A store with every kind of pack the mode must not see.
CROWDED = PostTreatment(
    later_packs_everywhere=True, extra_packs=(2, 2), stray_rows="both"
)


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_operational_mode_reads_only_each_joins_first_pack_from_the_pack_stream(
    make_log: Callable[..., HoldoutLog], options: dict[str, Any]
) -> None:
    """The report is the same when every other read of a pack row raises."""
    log = make_log(post=CROWDED)
    guarded = _FirstPackOnlyLog(log.event_log)
    report = holdout.analyze_holdout_operational(guarded, **{**WINDOW, **options})
    assert _bytes(report) == _bytes(_operational(log, **options))
    assert set(guarded.fetched) <= guarded.allowed
    assert len(guarded.fetched) >= TASKS  # the first packs were read, not skipped
    for end in WINDOW_ENDS[::6]:
        night = {**WINDOW, **NIGHT, **options, "until": end}
        assert _bytes(holdout.analyze_holdout_operational(guarded, **night)) == _bytes(
            _operational(log, until=end, **NIGHT, **options)
        ), end
    # The guard bites: the full analysis scans the pack stream and trips it.
    with pytest.raises(AssertionError, match="pack stream read"):
        holdout.analyze_holdout(guarded, **{**WINDOW, **FAST, **options})


def test_the_pack_stream_guard_catches_a_scan_and_a_later_pack_read(
    make_log: Callable[..., HoldoutLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutants: a pack scan added to the paging helper, a later pack fetched by id."""
    log = make_log(post=CROWDED)
    guarded = _FirstPackOnlyLog(log.event_log)
    real_read, real_fetch = holdout._read_window, holdout._first_pack_row

    def scanning(event_log: Any, event_type: Any, since: Any, until: Any) -> Any:
        event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, since=since, until=until, limit=10
        )
        return real_read(event_log, event_type, since, until)

    monkeypatch.setattr(holdout, "_read_window", scanning)
    with pytest.raises(AssertionError, match="pack stream read"):
        holdout.analyze_holdout_operational(guarded, **WINDOW)
    monkeypatch.setattr(holdout, "_read_window", real_read)

    later = next(
        event.entity_id
        for event in log.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=100_000
        )
        if event.entity_id not in guarded.allowed
    )

    def reading_later(event_log: Any, pack_id: str) -> Any:
        real_fetch(event_log, later)
        return real_fetch(event_log, pack_id)

    monkeypatch.setattr(holdout, "_first_pack_row", reading_later)
    with pytest.raises(AssertionError, match="pack stream read"):
        holdout.analyze_holdout_operational(guarded, **WINDOW)


# ---------------------------------------------------------------------------
# The cases the seed does not reach
# ---------------------------------------------------------------------------


@pytest.fixture
def blank(tmp_path: Path) -> Iterator[Callable[[], HoldoutLog]]:
    """A factory of empty stores."""
    opened: list[SQLiteEventLog] = []

    def make() -> HoldoutLog:
        event_log = SQLiteEventLog(tmp_path / f"blank-{len(opened)}.db")
        opened.append(event_log)
        return HoldoutLog(event_log)

    yield make
    for event_log in opened:
        event_log.close()


def _task(
    log: HoldoutLog,
    name: str,
    at: Any,
    *,
    rate: float = 0.5,
    later: tuple[float | None, ...] = (),
    join_at: Any = None,
    sweep: bool = True,
) -> list[str]:
    """One sub-agent task: its first pack, the later ones named, and its join."""
    first = log.pack(
        at=at, withheld=False, items=3, rate=rate, metadata=provenance(COMMIT_A)
    )
    ids = [first]
    for step, later_rate in enumerate(later, start=1):
        ids.append(
            log.pack(
                at=at + timedelta(minutes=step),
                withheld=None if later_rate is None else False,
                items=3,
                rate=later_rate or 0.5,
                metadata=provenance(COMMIT_A),
            )
        )
    log.join(
        name,
        at=join_at or at + timedelta(minutes=10),
        pack_ids=ids,
        parent="parent-a",
        outcome=outcome_payload(turns=5),
    )
    if sweep:
        log.sweep(at=at + timedelta(minutes=40))
    return ids


def test_a_later_pack_at_another_rate_cannot_raise_several_rates(
    blank: Callable[[], HoldoutLog],
) -> None:
    """Later packs are not read, so the rate is the first packs' and is not refused."""
    log = blank()
    for step in range(3):
        _task(log, f"task-{step}", MONDAY + timedelta(hours=step), later=(0.25,))
    window = {"days": 3, "until": MONDAY + timedelta(days=1), "settle_hours": 0.5}
    report = holdout.analyze_holdout_operational(log.event_log, **window)
    assert (report.rate, report.funnel.eligible, report.funnel.other_rate) == (
        0.5,
        3,
        0,
    )
    with pytest.raises(holdout.HoldoutAnalysisError, match="Several holdout rates"):
        holdout.analyze_holdout(log.event_log, **window, **FAST)


def test_a_first_pack_at_another_rate_counts_under_other_rate(
    blank: Callable[[], HoldoutLog],
) -> None:
    log = blank()
    _task(log, "task-0", MONDAY)
    _task(log, "task-1", MONDAY + timedelta(hours=1))
    _task(log, "task-2", MONDAY + timedelta(hours=2), rate=0.25)
    window = {"days": 3, "until": MONDAY + timedelta(days=1), "settle_hours": 0.5}
    with pytest.raises(holdout.HoldoutAnalysisError, match="Several holdout rates"):
        holdout.analyze_holdout_operational(log.event_log, **window)
    report = holdout.analyze_holdout_operational(log.event_log, rate=0.5, **window)
    assert (report.funnel.eligible, report.funnel.other_rate) == (2, 1)


def test_a_window_holding_only_a_later_pack_is_the_window_without_it(
    blank: Callable[[], HoldoutLog],
) -> None:
    """A lone later pack in the window sets no rate and no unkeyed note."""
    with_later, without = blank(), blank()
    for log in (with_later, without):
        first = log.pack(
            at=MONDAY, withheld=False, items=3, metadata=provenance(COMMIT_A)
        )
        pack_ids = [first]
        if log is with_later:
            pack_ids.append(
                log.pack(
                    at=MONDAY + timedelta(hours=26),
                    withheld=None,
                    items=3,
                    rate=0.25,
                    metadata=provenance(COMMIT_A),
                )
            )
        log.join(
            "task-0",
            at=MONDAY + timedelta(minutes=10),
            pack_ids=pack_ids,
            parent="parent-a",
            outcome=outcome_payload(turns=5),
        )
        log.sweep(at=MONDAY + timedelta(minutes=40))
    # One day ending 30 hours on holds the later pack and no first pack.
    until = MONDAY + timedelta(hours=30)
    window = {"days": 1, "until": until, "settle_hours": 0.5}
    held = with_later.event_log.get_events(
        event_type=EventType.PACK_ASSEMBLED,
        since=until - timedelta(days=1),
        until=until,
        limit=10,
    )
    assert len(held) == 1  # the later pack, alone
    assert _bytes(
        holdout.analyze_holdout_operational(with_later.event_log, **window)
    ) == _bytes(holdout.analyze_holdout_operational(without.event_log, **window))
    # The knob is live: the full report reads that row and differs.
    assert (
        holdout.analyze_holdout(with_later.event_log, **window, **FAST).model_dump()
        != holdout.analyze_holdout(without.event_log, **window, **FAST).model_dump()
    )


def test_a_task_whose_join_is_written_after_its_sweep_enters_when_it_exists(
    blank: Callable[[], HoldoutLog],
) -> None:
    """Admission is the first pack's: a join written late is read from that night.

    The sweep settled the first pack 40 minutes on, the join was written an
    hour on. Admitting on the join's time instead would need a later sweep.
    """
    log = blank()
    _task(log, "task-0", MONDAY, join_at=MONDAY + timedelta(hours=1))
    window = {"days": 3, "settle_hours": 0.5}
    before = MONDAY + timedelta(minutes=50)
    after = MONDAY + timedelta(minutes=90)
    assert (
        holdout.analyze_holdout_operational(
            log.event_log, until=before, **window
        ).funnel.eligible
        == 0
    )  # the join does not exist yet
    assert (
        holdout.analyze_holdout_operational(
            log.event_log, until=after, **window
        ).funnel.eligible
        == 1
    )


def test_a_session_that_began_before_the_window_is_not_counted_on_a_later_join(
    blank: Callable[[], HoldoutLog],
) -> None:
    """A join written again inside the window cannot move the session's first pack."""
    plain, again = blank(), blank()
    for log in (plain, again):
        first = _task(log, "task-0", MONDAY, sweep=False)
        other = log.pack(
            at=MONDAY + timedelta(days=3),
            withheld=True,
            items=3,
            metadata=provenance(COMMIT_B),
        )
        log.sweep(at=MONDAY + timedelta(days=4))
        if log is again:
            log.join(
                "task-0",
                at=MONDAY + timedelta(days=3, hours=1),
                pack_ids=[other, *first],
                parent="parent-a",
                outcome=outcome_payload(turns=9),
            )
    window = {"days": 2, "until": MONDAY + timedelta(days=4), "settle_hours": 0.5}
    reports = [
        holdout.analyze_holdout_operational(log.event_log, **window)
        for log in (plain, again)
    ]
    assert _bytes(reports[0]) == _bytes(reports[1])
    assert reports[0].funnel.eligible == 0


def test_a_join_with_no_first_pack_row_is_windowed_on_its_join_time(
    blank: Callable[[], HoldoutLog],
) -> None:
    """A stated limit: with no first pack to anchor on, the join's own time decides."""
    log = blank()
    for name, at in (("task-0", MONDAY), ("task-1", MONDAY + timedelta(days=5))):
        log.join(
            name,
            at=at,
            pack_ids=[f"pack-gone-{name}"],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
        )
    window = {"days": 2, "until": MONDAY + timedelta(days=6)}
    report = holdout.analyze_holdout_operational(log.event_log, **window)
    assert report.funnel.first_pack_not_found == 1  # only the join inside the window
    assert report.funnel.eligible == 0
    assert any("no PACK_ASSEMBLED row" in note for note in report.notes)


def test_the_joins_and_sweeps_are_paged_to_completion(
    make_log: Callable[..., HoldoutLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page far smaller than the window gives the report a whole read gives."""
    log = make_log()
    expected = [
        _bytes(_operational(log, until=end, **NIGHT, by_commit=True))
        for end in WINDOW_ENDS[::5]
    ]
    monkeypatch.setattr(holdout, "_PAGE_SIZE", 5)
    paged = [
        _bytes(_operational(log, until=end, **NIGHT, by_commit=True))
        for end in WINDOW_ENDS[::5]
    ]
    assert paged == expected


def test_a_timestamp_shared_by_more_events_than_a_page_is_refused(
    blank: Callable[[], HoldoutLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timestamp cursor cannot pass such a pile, and a short window must not pass."""
    log = blank()
    for step in range(7):
        log.join(
            f"task-{step}",
            at=MONDAY,
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
        )
    monkeypatch.setattr(holdout, "_PAGE_SIZE", 3)
    with pytest.raises(holdout.HoldoutAnalysisError, match="share one timestamp"):
        holdout.analyze_holdout_operational(
            log.event_log, days=3, until=MONDAY + timedelta(days=1)
        )
