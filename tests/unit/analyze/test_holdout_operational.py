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
* an event log whose joins and sweeps are allowlists: any key the report does
  not read, by ``.get``, subscript, iteration or copy, and any ``len``,
  ``repr``, ``==``, ``pop`` or ``setdefault`` of a payload or a block in it,
  raises, as does a retrieval counter of a join that named a pack. The log
  offers no attribute but ``get_events``;
* an allowlisted event log: joins, sweeps and one first pack by id are the only
  reads it answers, so a later pack, the number of packs, the volume of pack
  rows, the feedback stream and a raw connection cannot reach a figure;
* one store grown night by night, whose stored reports must equal a retro
  ``--until`` report and must not differ between stores that differ only in
  post-treatment values.

The rule they pin: the mode reads the joins, the sweeps and each join's first
pack, and nothing else from the pack stream. What no fixture can pin is a read
that stays quiet on every store the suite builds, such as a threshold on a
count (of re-emitted joins, say) that no fixture reaches: that is outside what a
suite of this kind catches, and review has to. The one disclosed exception is
whether a join carries an outcome block, which the fixtures do not move. A
join's ``pack_ids`` has to stay a ``list`` subclass (the report tests for one),
so its gate refuses the list's methods one by one rather than by listing keys.

Every id, commit and word the fixtures write is synthetic.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator, Mapping
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
    "errored retrievals on every task": PostTreatment(retrieval_errors=3),
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
        retrieval_errors=3,
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
    "errored retrievals on every task",
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


def _refuse(self: Any, *_args: Any, **_kwargs: Any) -> Any:
    msg = f"gated read: {type(self).__name__} was read past what the first pack fixes"
    raise AssertionError(msg)


class _Gate(Mapping[str, Any]):
    """A payload that answers the keys its class lists and refuses every other read.

    A ``Mapping`` rather than a ``dict``: ``get``, ``in``, ``items`` and ``==``
    are all built on ``__getitem__``, ``__iter__`` and ``__len__``, which the
    gate refuses or filters, and ``pop``, ``setdefault`` and ``update`` are not
    there to call (a name the class lacks raises as a read). The listed keys
    are the only way in, so no read gets past the gate because nobody thought
    to deny it. ``len``, ``repr``, ``==`` and truth refuse too, since each says
    something about the contents.
    """

    #: Keys that read whatever the payload holds.
    OPEN: frozenset[str] = frozenset()

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = dict(data)

    def _readable(self, key: str) -> bool:
        return key in self.OPEN

    def _refusal(self, key: str) -> str:
        return f"gated read: {type(self).__name__}[{key!r}]"

    def _wrap(self, key: str, value: Any) -> Any:
        return value

    def __getitem__(self, key: str) -> Any:
        if not self._readable(key):
            raise AssertionError(self._refusal(key))
        return self._wrap(key, self._data[key])

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):  # protocol probes (copy, pickle) ask politely
            raise AttributeError(name)
        msg = f"gated read: {type(self).__name__}.{name}"
        raise AssertionError(msg)

    __iter__ = __len__ = __bool__ = __repr__ = __eq__ = _refuse  # type: ignore[assignment]


class _Sealed(_Gate):
    """An outcome block that raises on any read of its contents."""

    def _refusal(self, key: str) -> str:
        return "gated read: the outcome block's contents"


class _TimingGate(_Gate):
    """``retrieval_timing``, readable only at the two flags set before the arm."""

    OPEN = frozenset(
        {"hook_feedback_before_first_retrieval", "commit_before_first_retrieval"}
    )

    def _refusal(self, key: str) -> str:
        return f"gated read: retrieval_timing[{key!r}] follows the first retrieval"


class _CovariatesGate(_Gate):
    """``covariates``, readable only at the two keys the report uses."""

    OPEN = frozenset({"brief_chars", "agent_type_digest"})


class _PackIds(list[Any]):
    """A join's ``pack_ids``, readable at index 0 and nowhere else.

    It stays a ``list`` because the report tests for one, so the list's other
    methods are refused one by one.
    """

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, int) and index == 0:
            return super().__getitem__(0)
        msg = "gated read: pack_ids past the first pack"
        raise AssertionError(msg)

    __iter__ = __len__ = __bool__ = __reversed__ = __contains__ = _refuse  # type: ignore[assignment]
    index = count = copy = pop = remove = insert = append = extend = _refuse  # type: ignore[assignment]
    sort = reverse = clear = __setitem__ = __delitem__ = _refuse  # type: ignore[assignment]
    __add__ = __radd__ = __iadd__ = __mul__ = __rmul__ = __imul__ = _refuse  # type: ignore[assignment]
    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = __repr__ = _refuse  # type: ignore[assignment]


class _JoinGate(_Gate):
    """A join, readable at the keys fixed before the arm and nowhere else.

    The retrieval counters (``retrieval_results``, ``retrieval_errors``,
    ``pack_ids_unparsed``) are readable only on a join that names no pack, where
    nothing was retrieved to count; on one that retrieved they are the session's
    whole run. ``outcome`` and the nested blocks come back gated. An empty join
    is falsy as a ``dict`` is, because the report writes ``payload or {}``.
    """

    OPEN = frozenset(
        {
            "pack_ids",
            "parent_session_id",
            "source_system",
            "outcome",
            "retrieval_timing",
            "covariates",
        }
    )
    ONLY_WITHOUT_PACKS = frozenset(
        {"retrieval_results", "retrieval_errors", "pack_ids_unparsed"}
    )

    def _readable(self, key: str) -> bool:
        return key in self.OPEN or (
            key in self.ONLY_WITHOUT_PACKS and not self._data.get("pack_ids")
        )

    def _refusal(self, key: str) -> str:
        if key in self.ONLY_WITHOUT_PACKS:
            return f"gated read: {key} of a join that retrieved"
        return f"gated read: a join's {key!r}"

    def _wrap(self, key: str, value: Any) -> Any:
        wrappers: dict[str, Callable[[Any], Any]] = {
            "outcome": _Sealed,
            "retrieval_timing": _TimingGate,
            "covariates": _CovariatesGate,
            "pack_ids": _PackIds,
        }
        expected = list if key == "pack_ids" else dict
        if key in wrappers and isinstance(value, expected):
            return wrappers[key](value)
        return value

    def __bool__(self) -> bool:
        return bool(self._data)


class _SweepGate(_Gate):
    """A sweep payload, readable at the keys the sweep reader uses.

    ``sessions_parsed`` and ``sessions_errored`` are how a sweep says it wrote
    nothing: a sweep that parsed no session and lost one to an error did not
    finish any task's join, so it settles nothing. They count sessions, not
    outcomes.
    """

    OPEN = frozenset(
        {"dry_run", "source_system", "sessions_parsed", "sessions_errored"}
    )

    def _refusal(self, key: str) -> str:
        return f"gated read: a sweep's {key!r}"

    def __bool__(self) -> bool:
        return bool(self._data)


class _GatedLog:
    """Hands the analysis every join and sweep with its payload gated.

    Only ``get_events`` is offered; any other attribute raises, so a raw
    connection or a count is not a way round the gates.
    """

    def __init__(self, inner: SQLiteEventLog) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        msg = f"gated read: log attribute {name!r}"
        raise AssertionError(msg)

    def get_events(self, *args: Any, **kwargs: Any) -> list[Event]:
        gates = {
            EventType.CAPTURE_SESSION_PACKS: _JoinGate,
            EventType.CAPTURE_SWEEP_COMPLETED: _SweepGate,
        }
        return [
            event.model_copy(
                update={"payload": gates[event.event_type](event.payload or {})}
            )
            if event.event_type in gates
            else event
            for event in self._inner.get_events(*args, **kwargs)
        ]


@pytest.mark.parametrize("options", VARIANTS, ids=lambda o: "+".join(o) or "default")
def test_operational_mode_reads_no_post_treatment_key_of_a_join(
    make_log: Callable[..., HoldoutLog], options: dict[str, Any]
) -> None:
    """The report is the same when every post-treatment read of a join raises.

    That is the outcome block's contents (its presence is readable), every
    ``retrieval_timing`` key but the two flags set before the arm, every
    ``covariates`` key but the two the report uses, every pack id after the
    first, any other key of a join, and the retrieval counters of a join that
    named a pack. The gates list what is readable, so a read that skips
    ``.get``, or that no one thought to deny, is caught.
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
    """The first pack id of each session's anchor join: the only packs to read.

    The anchor is the earliest join that names a pack.
    """
    earliest: dict[str, str] = {}
    for join in sorted(
        event_log.get_events(
            event_type=EventType.CAPTURE_SESSION_PACKS, order="asc", limit=100_000
        ),
        key=lambda event: (event.occurred_at, event.event_id),
    ):
        ids = (join.payload or {}).get("pack_ids") or []
        if join.entity_id and ids:
            earliest.setdefault(join.entity_id, ids[0])
    return set(earliest.values())


class _FirstPackOnlyLog:
    """An allowlisted event log: joins, sweeps and each session's first pack by id.

    ``get_events`` answers only for the join stream, the sweep stream and a
    pack row fetched by an id some session's anchor join names first. Every
    other read raises: another event type (the feedback stream among them), a
    pack query with no id or another one, a count, a read by event id and any
    attribute the wrapper does not have (a raw connection included). Its own
    attributes are reachable, ``_inner`` among them: a mutant that names the
    wrapper's private handle gets the real log, and only review catches it.
    So a later pack, the number of packs and the volume of pack rows cannot
    reach a figure by any route the log offers to the report. What a join or a
    sweep carries is gated by :class:`_GatedLog`.
    """

    LISTED = frozenset(
        {EventType.CAPTURE_SESSION_PACKS, EventType.CAPTURE_SWEEP_COMPLETED}
    )

    def __init__(self, inner: SQLiteEventLog) -> None:
        self._inner = inner
        self.allowed = _first_pack_ids(inner)
        self.fetched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        msg = f"read outside the allowlist: log attribute {name!r}"
        raise AssertionError(msg)

    def get_events(self, *args: Any, **kwargs: Any) -> list[Event]:
        event_type = kwargs.get("event_type")
        if event_type == EventType.PACK_ASSEMBLED:
            entity = kwargs.get("entity_id")
            if entity not in self.allowed:
                msg = f"read outside the allowlist: {event_type} for entity {entity!r}"
                raise AssertionError(msg)
            self.fetched.append(entity)
        elif event_type not in self.LISTED:
            msg = f"read outside the allowlist: {event_type}"
            raise AssertionError(msg)
        return self._inner.get_events(*args, **kwargs)


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
    with pytest.raises(AssertionError, match="outside the allowlist"):
        holdout.analyze_holdout(guarded, **{**WINDOW, **FAST, **options})


def test_the_pack_stream_guard_catches_a_scan_and_a_later_pack_read(
    make_log: Callable[..., HoldoutLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutants: a pack scan added to the paging helper, a later pack fetched by id."""
    log = make_log(post=CROWDED)
    guarded = _FirstPackOnlyLog(log.event_log)
    real_read, real_fetch = holdout._read_window, holdout._first_pack_row

    def scanning(
        event_log: Any, event_type: Any, since: Any, until: Any, **kwargs: Any
    ) -> Any:
        event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, since=since, until=until, limit=10
        )
        return real_read(event_log, event_type, since, until, **kwargs)

    monkeypatch.setattr(holdout, "_read_window", scanning)
    with pytest.raises(AssertionError, match="outside the allowlist"):
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
    with pytest.raises(AssertionError, match="outside the allowlist"):
        holdout.analyze_holdout_operational(guarded, **WINDOW)


def test_the_guards_catch_a_feedback_read_a_raw_query_and_a_payload_read(
    make_log: Callable[..., HoldoutLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutants: a feedback read, a raw connection, a sweep key, an error count."""
    log = make_log(post=CROWDED)
    guarded, gated = _FirstPackOnlyLog(log.event_log), _GatedLog(log.event_log)
    real_read, real_excluded = holdout._read_window, holdout._pre_treatment_excluded

    def feedback(event_log: Any, *args: Any, **kwargs: Any) -> Any:
        event_log.get_events(event_type=EventType.FEEDBACK_RECORDED, limit=5)
        return real_read(event_log, *args, **kwargs)

    def raw_connection(event_log: Any, *args: Any, **kwargs: Any) -> Any:
        event_log._conn.execute("select count(*) from events")
        return real_read(event_log, *args, **kwargs)

    for mutant in (feedback, raw_connection):
        monkeypatch.setattr(holdout, "_read_window", mutant)
        with pytest.raises(AssertionError, match="outside the allowlist"):
            holdout.analyze_holdout_operational(guarded, **WINDOW)

    def sweep_payload(
        event_log: Any, event_type: Any, *args: Any, **kwargs: Any
    ) -> Any:
        events = real_read(event_log, event_type, *args, **kwargs)
        if event_type == EventType.CAPTURE_SWEEP_COMPLETED and events:
            (events[-1].payload or {}).get("pack_joins_recorded")
        return events

    monkeypatch.setattr(holdout, "_read_window", sweep_payload)
    with pytest.raises(AssertionError, match="gated read: a sweep"):
        holdout.analyze_holdout_operational(gated, **WINDOW)
    monkeypatch.setattr(holdout, "_read_window", real_read)

    def error_count(payload: Any, funnel: Any) -> Any:
        payload.get("retrieval_errors")
        return real_excluded(payload, funnel)

    monkeypatch.setattr(holdout, "_pre_treatment_excluded", error_count)
    with pytest.raises(AssertionError, match="gated read: retrieval_errors"):
        holdout.analyze_holdout_operational(gated, **WINDOW)


def _inside(join: Any, key: str, read: Callable[[Any], Any]) -> Any:
    """``read`` of a nested block, for the joins that carry one."""
    block = join.get(key)
    return None if block is None else read(block)


#: Reads a mutant of the report might add, each with the refusal it must meet.
#: The gates list what the report reads; none of these is on the list.
JOIN_READS: dict[str, tuple[Callable[[Any], Any], str]] = {
    "results of a join that named a pack": (
        lambda join: join.get("retrieval_results"),
        "gated read: retrieval_results of a join that retrieved",
    ),
    "errors of a join that named a pack": (
        lambda join: join["retrieval_errors"],
        "gated read: retrieval_errors of a join that retrieved",
    ),
    "unparsed ids of a join that named a pack": (
        lambda join: join.get("pack_ids_unparsed"),
        "gated read: pack_ids_unparsed of a join that retrieved",
    ),
    "a key nobody lists": (
        lambda join: join.get("made_up_key"),
        "gated read: a join's 'made_up_key'",
    ),
    "a key of the covariates nobody lists": (
        lambda join: _inside(join, "covariates", lambda block: block.get("zzz")),
        "gated read: _CovariatesGate['zzz']",
    ),
    "the size of the outcome block": (
        lambda join: len(join.get("outcome")),
        "gated read: _Sealed was read",
    ),
    "the outcome block compared": (
        lambda join: join.get("outcome") == {},
        "gated read: _Sealed was read",
    ),
    "the outcome block shown": (
        lambda join: repr(join.get("outcome")),
        "gated read: _Sealed was read",
    ),
    "a pop of the error count": (
        lambda join: join.pop("retrieval_errors", 0),
        "gated read: _JoinGate.pop",
    ),
    "a setdefault of the error count": (
        lambda join: join.setdefault("retrieval_errors", 0),
        "gated read: _JoinGate.setdefault",
    ),
    "the size of the join": (len, "gated read: _JoinGate was read"),
    "the join compared": (
        lambda join: join == {},
        "gated read: _JoinGate was read",
    ),
    "the join shown": (repr, "gated read: _JoinGate was read"),
    "the join copied": (dict, "gated read: _JoinGate was read"),
    "the timing block's size": (
        lambda join: _inside(join, "retrieval_timing", len),
        "gated read: _TimingGate was read",
    ),
    "the pack ids compared": (
        lambda join: join.get("pack_ids") == [],
        "gated read: _PackIds was read",
    ),
    "the pack ids popped": (
        lambda join: join.get("pack_ids").pop(),
        "gated read: _PackIds was read",
    ),
}


@pytest.mark.parametrize("read", JOIN_READS)
def test_the_join_gates_refuse_every_read_the_report_does_not_make(
    make_log: Callable[..., HoldoutLog], monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    """Mutants: each read in :data:`JOIN_READS`, added to the eligibility stage.

    The gates are allowlists, so a read that nobody thought to deny is refused
    as much as one that somebody did.
    """
    reader, refusal = JOIN_READS[read]
    real = holdout._pre_treatment_excluded

    def reading(payload: Any, funnel: Any) -> Any:
        reader(payload)
        return real(payload, funnel)

    log = make_log(1)
    assert holdout.analyze_holdout_operational(log.event_log, **WINDOW)
    monkeypatch.setattr(holdout, "_pre_treatment_excluded", reading)
    with pytest.raises(AssertionError, match="gated read") as caught:
        holdout.analyze_holdout_operational(_GatedLog(log.event_log), **WINDOW)
    assert refusal in str(caught.value)


SWEEP_READS: dict[str, tuple[Callable[[Any], Any], str]] = {
    "a pop of a sweep's counter": (
        lambda sweep: sweep.pop("pack_joins_recorded", 0),
        "gated read: _SweepGate.pop",
    ),
    "a setdefault on a sweep": (
        lambda sweep: sweep.setdefault("pack_joins_recorded", 0),
        "gated read: _SweepGate.setdefault",
    ),
    "a key of a sweep nobody lists": (
        lambda sweep: sweep.get("pack_joins_recorded"),
        "gated read: a sweep's 'pack_joins_recorded'",
    ),
    "the size of a sweep": (
        len,
        "gated read: _SweepGate was read",
    ),
}


@pytest.mark.parametrize("read", SWEEP_READS)
def test_the_sweep_gate_refuses_every_read_the_report_does_not_make(
    make_log: Callable[..., HoldoutLog], monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    """Mutants: each read in :data:`SWEEP_READS`, added to the sweep reader."""
    reader, refusal = SWEEP_READS[read]
    real = holdout._latest_sweeps

    def reading(events: Any) -> Any:
        assert events
        reader(events[-1].payload)
        return real(events)

    log = make_log(1)
    monkeypatch.setattr(holdout, "_latest_sweeps", reading)
    with pytest.raises(AssertionError, match="gated read") as caught:
        holdout.analyze_holdout_operational(_GatedLog(log.event_log), **WINDOW)
    assert refusal in str(caught.value)


def test_a_join_gate_lets_the_listed_reads_through() -> None:
    """The gate is no stricter than the report: what the report reads works."""
    join = _JoinGate(
        {
            "pack_ids": ["pack-0001", "pack-0002"],
            "parent_session_id": "parent-a",
            "source_system": "claude-code",
            "outcome": {"assistant_turns": 3},
            "retrieval_timing": {"hook_feedback_before_first_retrieval": False},
            "covariates": {"brief_chars": 9, "agent_type_digest": "a"},
            "retrieval_results": 2,
        }
    )
    assert join  # ``payload or {}`` is how the report tests for an empty payload
    assert join.get("pack_ids")[0] == "pack-0001"
    assert join.get("parent_session_id") == "parent-a"
    assert isinstance(join.get("outcome"), Mapping)
    assert (
        join.get("retrieval_timing").get("hook_feedback_before_first_retrieval")
        is False
    )
    assert "commit_before_first_retrieval" not in join.get("retrieval_timing")
    assert join.get("covariates").get("brief_chars") == 9
    with pytest.raises(
        AssertionError, match="retrieval_results of a join that retrieved"
    ):
        join.get("retrieval_results")
    without_packs = _JoinGate({"pack_ids": [], "retrieval_results": 0})
    assert without_packs.get("retrieval_results") == 0
    assert without_packs.get("pack_ids_unparsed") is None  # listed, not written


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


def test_a_page_of_tied_events_is_refused_as_a_pile_is(
    blank: Callable[[], HoldoutLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly a page of events at one instant cannot be passed either."""
    log = blank()
    for step in range(3):
        log.join(
            f"task-{step}",
            at=MONDAY,
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=5),
        )
    monkeypatch.setattr(holdout, "_PAGE_SIZE", 3)
    with pytest.raises(holdout.HoldoutAnalysisError, match="A page or more"):
        holdout.analyze_holdout_operational(
            log.event_log, days=3, until=MONDAY + timedelta(days=1)
        )


# ---------------------------------------------------------------------------
# A task whose earliest join names no pack, and ties
# ---------------------------------------------------------------------------


def test_a_task_whose_earliest_join_names_no_pack_is_read_from_the_join_that_does(
    blank: Callable[[], HoldoutLog],
) -> None:
    """A sweep before the first retrieval writes a pack-free join; the task stays.

    Ten sub-agent tasks and one main transcript each get a pack-free join a
    minute before their first pack and the join that names it later. Read from
    the first join alone they were ten ``no_retrieval`` tasks, no arm and no
    rate; the full report, which reads the latest join, counts all ten.
    """
    log = blank()
    for step in range(11):
        at = MONDAY + timedelta(hours=step)
        first = log.pack(
            at=at, withheld=step % 2 == 1, items=3, metadata=provenance(COMMIT_A)
        )
        parent = "parent-a" if step < 10 else None
        log.join(
            f"task-{step}",
            at=at - timedelta(minutes=1),
            pack_ids=[],
            parent=parent,
            outcome=outcome_payload(turns=1),
        )
        log.join(
            f"task-{step}",
            at=at + timedelta(minutes=10),
            pack_ids=[first],
            parent=parent,
            outcome=outcome_payload(turns=9),
        )
    log.sweep(at=MONDAY + timedelta(days=2))
    window: dict[str, Any] = {"days": 30, "until": MONDAY + timedelta(days=3)}
    operational = holdout.analyze_holdout_operational(log.event_log, **window)
    full = holdout.analyze_holdout(log.event_log, **window, **FAST)
    assert (
        operational.funnel.no_retrieval,
        operational.funnel.eligible,
        operational.rate,
    ) == (0, 10, 0.5)
    assert (full.funnel.eligible, full.rate) == (10, 0.5)
    arms = operational.descriptive.eligible_by_arm
    assert (arms.served, arms.withheld) == (5, 5)
    assert operational.descriptive.sessions.with_first_pack == 1
    assert (
        operational.descriptive.sessions.eligible == full.descriptive.sessions.eligible
    )


def test_a_pack_free_join_before_the_window_does_not_make_a_session_begin_before_it(
    blank: Callable[[], HoldoutLog],
) -> None:
    """Only a join that names a pack puts a session's first pack before the window."""
    log = blank()
    first = log.pack(
        at=MONDAY + timedelta(days=3),
        withheld=False,
        items=3,
        metadata=provenance(COMMIT_A),
    )
    log.join(
        "task-0",
        at=MONDAY,
        pack_ids=[],
        parent="parent-a",
        outcome=outcome_payload(turns=1),
    )
    log.join(
        "task-0",
        at=MONDAY + timedelta(days=3, minutes=5),
        pack_ids=[first],
        parent="parent-a",
        outcome=outcome_payload(turns=5),
    )
    log.sweep(at=MONDAY + timedelta(days=4))
    window = {"days": 2, "until": MONDAY + timedelta(days=5)}
    assert (
        holdout.analyze_holdout_operational(log.event_log, **window).funnel.eligible
        == 1
    )


def _event_join(
    log: HoldoutLog, event_id: str, session: str, at: Any, pack_ids: list[str]
) -> None:
    """A join with a chosen event id, as an import or a clock tie would write one."""
    log.event_log.append(
        Event(
            event_id=event_id,
            event_type=EventType.CAPTURE_SESSION_PACKS,
            source="worker:session-capture",
            entity_id=session,
            entity_type="capture_session",
            occurred_at=at,
            recorded_at=at,
            payload={
                "pack_ids": pack_ids,
                "retrieval_results": len(pack_ids),
                "retrieval_errors": 0,
                "pack_ids_unparsed": 0,
                "parent_session_id": "parent-a",
                "source_system": "claude-code",
                "outcome": outcome_payload(turns=5),
            },
        )
    )


def test_two_joins_of_a_session_at_one_instant_are_chosen_by_event_id(
    blank: Callable[[], HoldoutLog],
) -> None:
    """The order the log returns tied joins in is not the anchor's choice."""
    reports = []
    for order in ((0, 1), (1, 0)):
        log = blank()
        packs = [
            log.pack(at=MONDAY, withheld=arm, items=3, metadata=provenance(COMMIT_A))
            for arm in (False, True)
        ]
        at = MONDAY + timedelta(minutes=5)
        for position in order:  # event ids 0 and 1, written in either order
            _event_join(log, f"evt-{position}", "task-0", at, [packs[position]])
        log.sweep(at=MONDAY + timedelta(days=1))
        report = holdout.analyze_holdout_operational(
            log.event_log, days=3, until=MONDAY + timedelta(days=2)
        )
        arms = report.descriptive.eligible_by_arm
        assert (arms.served, arms.withheld) == (1, 0)  # evt-0's pack is served
        reports.append(_bytes(report))
    assert reports[0] == reports[1]


def test_by_commit_rows_first_seen_together_are_ordered_by_build_label(
    blank: Callable[[], HoldoutLog],
) -> None:
    """Two builds' first packs at one instant: join order does not order the rows."""
    reports = []
    for order in ((COMMIT_A, COMMIT_B), (COMMIT_B, COMMIT_A)):
        log = blank()
        for step, commit in enumerate(order):
            first = log.pack(
                at=MONDAY, withheld=bool(step), items=3, metadata=provenance(commit)
            )
            log.join(
                f"task-{commit}",
                at=MONDAY + timedelta(minutes=5 + step),
                pack_ids=[first],
                parent="parent-a",
                outcome=outcome_payload(turns=5),
            )
        log.sweep(at=MONDAY + timedelta(days=1))
        report = holdout.analyze_holdout_operational(
            log.event_log, days=3, until=MONDAY + timedelta(days=2), by_commit=True
        )
        assert report.by_commit is not None
        labels = [row.build for row in report.by_commit.rows]
        assert labels == sorted(labels)
        assert len(labels) == 2
        reports.append(labels)
    assert reports[0] == reports[1]


# ---------------------------------------------------------------------------
# A session whose joins name no pack, and the sessions that began before the window
# ---------------------------------------------------------------------------

#: The two ways a join can name no pack: capture never saw a retrieval call, or
#: it saw results it could not read.
PACK_FREE_COUNTERS = [
    pytest.param({"retrieval_results": 0}, "no_retrieval_never_called", id="never"),
    pytest.param(
        {"retrieval_results": 1, "pack_ids_unparsed": 1},
        "no_retrieval_unparsed",
        id="unparsed",
    ),
]


def _pack_free_sessions(
    blank: Callable[[], HoldoutLog], counters: dict[str, Any], *, again: bool
) -> HoldoutLog:
    """Ten sessions whose only join names no pack; ``again`` writes each anew later."""
    log = blank()
    for hours in (12, 40, 60):
        log.sweep(at=MONDAY + timedelta(hours=hours))
    for step in range(10):
        log.join(
            f"task-{step}",
            at=MONDAY + timedelta(hours=step + 1),
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=1),
            **counters,
        )
        if again:
            log.join(
                f"task-{step}",
                at=MONDAY + timedelta(hours=30 + step),
                pack_ids=[],
                parent="parent-a",
                outcome=outcome_payload(turns=9),
                **counters,
            )
    return log


@pytest.mark.parametrize(("counters", "bucket"), PACK_FREE_COUNTERS)
def test_a_pack_free_join_written_again_cannot_count_a_session_in_a_later_window(
    blank: Callable[[], HoldoutLog], counters: dict[str, Any], bucket: str
) -> None:
    """A session is counted in the window its first join falls in and in no other.

    Each session's first join sits in the first day and is settled by the sweep
    that ends it; a second join, written a day later with other values, would
    be the earliest join the second night reads, and its sweep settles it.
    Without a skip for a session with a join before the window, the pooled
    ``no_retrieval`` count of the second night moves with that second write.
    """
    once = _pack_free_sessions(blank, counters, again=False)
    twice = _pack_free_sessions(blank, counters, again=True)
    nights = [MONDAY + timedelta(hours=hours) for hours in (24, 48, 72)]
    options: dict[str, Any] = {"days": 1, "settle_hours": 0.5}
    seen = []
    for end in [*nights, MONDAY + timedelta(hours=44)]:
        reports = [
            holdout.analyze_holdout_operational(
                log.event_log, until=end, **options, by_commit=True
            )
            for log in (once, twice)
        ]
        assert _bytes(reports[0]) == _bytes(reports[1]), end
        seen.append(reports[1])
    first, second, third, finding = seen
    assert getattr(first.funnel, bucket) == first.funnel.no_retrieval == 10
    assert second.funnel.no_retrieval == third.funnel.no_retrieval == 0
    assert finding.funnel.no_retrieval == 0  # +44h: only the second writes are in it


def test_a_pack_free_join_unsettled_at_the_window_end_is_missed_that_night_and_after(
    blank: Callable[[], HoldoutLog],
) -> None:
    """The cost of the skip, pinned: a nightly series undercounts a late pack-free join.

    A join written less than the settle time before a window's end has no
    sweep after it in that window, so that night's report leaves it out, and
    the next night's report does not read it either: it is older than that
    window, and the sessions with a join older than the window are the ones
    left alone. One wide window reads it. The count depends on the time the
    join was written, which no treatment moves, never on a value one could.
    """
    log = blank()
    for step in range(4):
        log.join(
            f"task-{step}",
            at=MONDAY + timedelta(hours=20 + step),
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=1),
        )
    log.sweep(at=MONDAY + timedelta(hours=23, minutes=12))
    log.sweep(at=MONDAY + timedelta(hours=30))
    options: dict[str, Any] = {"settle_hours": 0.5}

    def counted(days: int, hours: int) -> int:
        return holdout.analyze_holdout_operational(
            log.event_log, days=days, until=MONDAY + timedelta(hours=hours), **options
        ).funnel.no_retrieval

    assert counted(days=1, hours=24) == 3  # the join at +23h has no sweep after it yet
    assert counted(days=1, hours=48) == 0  # and it is older than the next window
    assert counted(days=2, hours=48) == 4  # one wide window reads all four


def test_a_settled_session_whose_joins_all_name_no_pack_counts_as_no_retrieval(
    blank: Callable[[], HoldoutLog],
) -> None:
    """The full report counts such a session, and so does the operational one."""
    log = blank()
    for step in range(4):
        log.join(
            f"task-{step}",
            at=MONDAY + timedelta(hours=step),
            pack_ids=[],
            parent="parent-a",
            outcome=outcome_payload(turns=1),
        )
    log.sweep(at=MONDAY + timedelta(days=1))
    window: dict[str, Any] = {"days": 30, "until": MONDAY + timedelta(days=3)}
    operational = holdout.analyze_holdout_operational(log.event_log, **window)
    full = holdout.analyze_holdout(log.event_log, **window, **FAST)
    assert operational.funnel.no_retrieval == full.funnel.no_retrieval == 4
    assert operational.funnel.eligible == full.funnel.eligible == 0
    unsettled = holdout.analyze_holdout_operational(
        log.event_log, days=30, until=MONDAY + timedelta(hours=1)
    )
    assert unsettled.funnel.no_retrieval == 0  # no sweep has followed the joins yet


#: What a session's joins before the window named, oldest first.
BEGAN_BEFORE = {
    "a pack, then none": ("named", "pack-free"),
    "none, then a pack": ("pack-free", "named"),
    "a pack alone": ("named",),
}


@pytest.mark.parametrize("before", BEGAN_BEFORE.values(), ids=BEGAN_BEFORE.keys())
def test_a_session_with_any_pack_naming_join_before_the_window_is_not_counted(
    blank: Callable[[], HoldoutLog], before: tuple[str, ...]
) -> None:
    """Whichever of its earlier joins names the pack, the session began before.

    The window's own join names a new first pack. Reading only the earliest join
    before the window, or only the latest, misses one of the orders.
    """
    log = blank()
    since = MONDAY + timedelta(days=5)
    window: dict[str, Any] = {"days": 5, "until": MONDAY + timedelta(days=10)}
    older = log.pack(
        at=since - timedelta(days=2),
        withheld=False,
        items=3,
        metadata=provenance(COMMIT_A),
    )
    newer = log.pack(
        at=since + timedelta(hours=1),
        withheld=False,
        items=3,
        metadata=provenance(COMMIT_A),
    )
    for step, kind in enumerate(before):
        log.join(
            "task-0",
            at=since - timedelta(days=2 - step),
            pack_ids=[older] if kind == "named" else [],
            parent="parent-a",
            outcome=outcome_payload(turns=1),
        )
    log.join(
        "task-0",
        at=since + timedelta(hours=2),
        pack_ids=[newer],
        parent="parent-a",
        outcome=outcome_payload(turns=5),
    )
    log.sweep(at=MONDAY + timedelta(days=9))
    assert (
        holdout.analyze_holdout_operational(log.event_log, **window).funnel.eligible
        == 0
    )


def test_a_join_exactly_at_the_window_start_is_inside_the_window(
    blank: Callable[[], HoldoutLog],
) -> None:
    """The window is closed at its start: a join at ``since`` did not come before it."""
    log = blank()
    since = MONDAY + timedelta(days=5)
    first = log.pack(at=since, withheld=False, items=3, metadata=provenance(COMMIT_A))
    log.join(
        "task-0",
        at=since,
        pack_ids=[first],
        parent="parent-a",
        outcome=outcome_payload(turns=5),
    )
    log.sweep(at=MONDAY + timedelta(days=9))
    report = holdout.analyze_holdout_operational(
        log.event_log, days=5, until=MONDAY + timedelta(days=10)
    )
    assert report.funnel.eligible == 1


def test_a_rate_no_first_pack_was_written_at_is_named_by_first_pack_here_and_row_there(
    blank: Callable[[], HoldoutLog],
) -> None:
    """The operational note names what it holds; the full report's is unchanged."""
    log = blank()
    _task(log, "task-0", MONDAY)
    window: dict[str, Any] = {"days": 30, "until": MONDAY + timedelta(days=3)}
    operational = holdout.analyze_holdout_operational(
        log.event_log, rate=0.25, **window
    )
    full = holdout.analyze_holdout(log.event_log, rate=0.25, **window, **FAST)
    assert "No first pack in the window was written at rate 0.25." in operational.notes
    assert "No row in the window was written at rate 0.25." in full.notes
    # The helper's own default is the full report's wording.
    _, notes = holdout._resolve_rate(holdout._Rows(), 0.25)
    assert notes == ["No row in the window was written at rate 0.25."]
