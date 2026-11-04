"""Stage 2 of the precedent gate: each criterion, the null, determinism."""

from __future__ import annotations

import random

import pytest

from trellis.errors import ConfigError
from trellis.learning.precedent_activation import (
    CRITERION_HELPFUL,
    CRITERION_LIFT,
    CRITERION_P_VALUE,
    CRITERION_SESSIONS,
    CRITERION_UNHELPFUL,
    ActivationThresholds,
    GradedPack,
    ServedItem,
    evaluate_target,
    evaluate_targets,
    within_pack_permutation_p,
)

TARGET = "target"


def _pack(
    pack_id: str,
    *,
    session: str | None,
    size: int = 20,
    helpful: tuple[str, ...] = (),
    unhelpful: tuple[str, ...] = (),
    extra_helpful: int = 0,
    target: bool = True,
) -> GradedPack:
    """A pack of ``size`` items; ``extra_helpful`` other items are cited helpful."""
    others = [f"{pack_id}-o{i}" for i in range(size - (1 if target else 0))]
    served = ([TARGET] if target else []) + others
    cited = (*helpful, *others[:extra_helpful])
    return GradedPack(
        pack_id=pack_id,
        session_id=session,
        served=tuple(ServedItem(i, rank=n + 1) for n, i in enumerate(served)),
        helpful=frozenset(cited),
        unhelpful=frozenset(unhelpful),
    )


def _helpful_in(n: int, *, sessions: int | None = None, size: int = 60):
    """``n`` packs citing the target helpful, big enough that chance is slim."""
    sessions = n if sessions is None else sessions
    return [
        _pack(f"p{i}", session=f"s{i % sessions}", size=size, helpful=(TARGET,))
        for i in range(n)
    ]


def _failed(packs: list[GradedPack], **limits: float) -> str | None:
    return evaluate_target(TARGET, packs, ActivationThresholds(**limits)).first_failed


# --------------------------------------------------------------- the criteria


def test_a_target_that_meets_every_criterion_passes() -> None:
    result = evaluate_target(TARGET, _helpful_in(4))

    assert result.passes
    assert result.first_failed is None
    assert (result.helpful, result.unhelpful, result.servings) == (4, 0, 4)
    assert result.distinct_helpful_sessions == 4
    assert result.p_value is not None
    assert result.p_value < 0.05
    assert result.lift is not None
    assert result.lift >= 1.2


def test_helpful_boundary_is_three() -> None:
    assert _failed(_helpful_in(2, sessions=2)) == CRITERION_HELPFUL
    assert _failed(_helpful_in(3, sessions=3)) is None


def test_unhelpful_must_stay_below_helpful() -> None:
    def with_unhelpful(u: int) -> list[GradedPack]:
        bad = [
            _pack(f"u{i}", session=f"x{i}", size=60, unhelpful=(TARGET,))
            for i in range(u)
        ]
        return [*_helpful_in(4), *bad]

    assert _failed(with_unhelpful(3)) is None  # u = h - 1
    assert _failed(with_unhelpful(4)) == CRITERION_UNHELPFUL  # u = h
    assert _failed(with_unhelpful(5)) == CRITERION_UNHELPFUL


def test_distinct_sessions_boundary_is_two() -> None:
    assert _failed(_helpful_in(4, sessions=1)) == CRITERION_SESSIONS
    assert _failed(_helpful_in(4, sessions=2)) is None


def test_sessionless_helpful_packs_never_count_as_a_session() -> None:
    packs = [_pack(f"p{i}", session=None, size=60, helpful=(TARGET,)) for i in range(4)]

    result = evaluate_target(TARGET, packs)

    assert result.distinct_helpful_sessions == 0
    assert result.sessionless_helpful == 4
    assert result.first_failed == CRITERION_SESSIONS


def _lenient(**limits: float) -> ActivationThresholds:
    """Thresholds with the P bound out of the way, to isolate the lift."""
    return ActivationThresholds(max_p_value=1.0, **limits)


def test_lift_boundary_is_one_point_two() -> None:
    # Target helpful in all three packs; each pack of 6 cites 5 helpful, so the
    # chance rate is 5/6 and the lift is 1 / (5/6) = 1.2.
    packs = [
        _pack(f"p{i}", session=f"s{i}", size=6, helpful=(TARGET,), extra_helpful=4)
        for i in range(3)
    ]
    lift = evaluate_target(TARGET, packs, _lenient()).lift

    assert lift == pytest.approx(1.2)
    assert evaluate_target(TARGET, packs, _lenient(min_lift=1.19)).passes
    just_over = evaluate_target(TARGET, packs, _lenient(min_lift=1.21))
    assert just_over.first_failed == CRITERION_LIFT


def test_lift_exactly_on_the_bound_passes_and_just_over_fails() -> None:
    # Packs of two with one label: chance rate 0.5, lift exactly 2.0.
    packs = _helpful_in(3, size=2)

    assert evaluate_target(TARGET, packs, _lenient(min_lift=2.0)).passes
    over = evaluate_target(TARGET, packs, _lenient(min_lift=2.0000001))
    assert over.first_failed == CRITERION_LIFT


def test_lift_fails_after_p_passes() -> None:
    result = evaluate_target(
        TARGET, _helpful_in(4, size=60), ActivationThresholds(min_lift=70.0)
    )

    assert result.lift == pytest.approx(60.0)
    assert result.p_value is not None
    assert result.p_value < 0.05
    assert result.first_failed == CRITERION_LIFT


def test_p_value_well_below_the_bound_passes_and_above_it_fails() -> None:
    # Three of three in packs of 20 with one label each: chance is (1/20)^3, so
    # P sits at its floor 1 / (1 + R).
    thin = evaluate_target(
        TARGET, _helpful_in(3, size=20), ActivationThresholds(resamples=2000)
    )
    assert thin.p_value == pytest.approx(1 / 2001, abs=0.002)
    assert thin.passes

    # Packs of two with one label: chance is 1/8, well above 0.05.
    small = evaluate_target(
        TARGET, _helpful_in(3, size=2), ActivationThresholds(resamples=4000)
    )
    assert small.first_failed == CRITERION_P_VALUE
    assert small.p_value == pytest.approx(0.125, abs=0.03)


def test_the_p_bound_is_strict() -> None:
    packs = _helpful_in(3, size=2)
    base = ActivationThresholds(resamples=4000)
    p = evaluate_target(TARGET, packs, base).p_value
    assert p is not None

    # The same seed gives the same P, so a bound placed exactly on it must fail
    # (``P < bound``), and one just above must pass.
    on_bound = ActivationThresholds(resamples=4000, max_p_value=p)
    assert evaluate_target(TARGET, packs, on_bound).first_failed == CRITERION_P_VALUE
    above = ActivationThresholds(resamples=4000, max_p_value=min(1.0, p + 1e-6))
    assert evaluate_target(TARGET, packs, above).first_failed != CRITERION_P_VALUE


# ------------------------------------------------------- the structured null


def _structured_fixture() -> list[GradedPack]:
    """The target is helpful in three small, heavily cited packs.

    Every pack it sits in has two items and one helpful label, so chance alone
    makes it helpful half the time there. Many large, thinly cited packs it was
    never served in pull the window's global helpful rate to a few percent.
    """
    small = [
        _pack(f"small{i}", session=f"s{i}", size=2, helpful=(TARGET,)) for i in range(3)
    ]
    large = [
        _pack(f"big{i}", session=f"b{i}", size=50, extra_helpful=1, target=False)
        for i in range(40)
    ]
    return [*small, *large]


def _global_rate_p(packs: list[GradedPack], observed: int, servings: int) -> float:
    """What a pack-blind null reports: one global helpful rate for every draw."""
    cited = sum(p.helpful_served for p in packs)
    total = sum(p.size for p in packs)
    rate = cited / total
    rng = random.Random(0)  # noqa: S311
    resamples = 4000
    hits = sum(
        1
        for _ in range(resamples)
        if sum(rng.random() < rate for _ in range(servings)) >= observed
    )
    return (1 + hits) / (1 + resamples)


def test_the_null_keeps_pack_sizes_and_citation_counts() -> None:
    packs = _structured_fixture()

    within = evaluate_target(TARGET, packs, ActivationThresholds(resamples=4000))
    blind = _global_rate_p(packs, observed=3, servings=3)

    # Chance explains three helpful labels in three two-item packs an eighth of
    # the time; a global rate of about 2% says it almost never does.
    assert within.p_value == pytest.approx(0.125, abs=0.03)
    assert blind < 0.01
    assert within.first_failed == CRITERION_P_VALUE
    assert within.passes is False


def test_the_baseline_is_the_packs_own_chance_not_the_global_rate() -> None:
    result = evaluate_target(TARGET, _structured_fixture())

    assert result.baseline_rate == pytest.approx(0.5)
    assert result.target_rate == pytest.approx(1.0)
    assert result.lift == pytest.approx(2.0)


# ------------------------------------------------------ determinism, isolation


def test_the_same_inputs_give_the_same_p_value() -> None:
    packs = _structured_fixture()

    first = evaluate_target(TARGET, packs).p_value
    second = evaluate_target(TARGET, list(reversed(packs))).p_value

    assert first == second


def test_a_targets_p_does_not_depend_on_the_other_targets() -> None:
    packs = _structured_fixture()
    alone = evaluate_targets([TARGET], packs)[TARGET].p_value
    crowd = evaluate_targets(["big0-o0", "big1-o1", TARGET, "big2-o2"], packs)[
        TARGET
    ].p_value

    assert alone == crowd


def test_the_seed_changes_the_draws() -> None:
    packs = _structured_fixture()

    one = evaluate_target(TARGET, packs, ActivationThresholds(resamples=500, seed=1))
    two = evaluate_target(TARGET, packs, ActivationThresholds(resamples=500, seed=2))

    assert one.p_value != two.p_value


def test_the_global_random_state_is_neither_read_nor_moved() -> None:
    packs = _structured_fixture()
    random.seed(1234)
    expected_next = random.random()  # noqa: S311
    random.seed(1234)

    evaluate_target(TARGET, packs)
    after = random.random()  # noqa: S311

    assert after == expected_next

    random.seed(99)
    a = evaluate_target(TARGET, packs).p_value
    random.seed(7)
    b = evaluate_target(TARGET, packs).p_value
    assert a == b


# ------------------------------------------ sectioned packs, helpful wins, shape


def test_a_sectioned_pack_counts_once_per_id_with_section_local_ranks() -> None:
    sectioned = GradedPack(
        pack_id="sec",
        session_id="s1",
        served=(
            ServedItem(TARGET, rank=1, section="lessons"),
            ServedItem("a", rank=2, section="lessons"),
            ServedItem("b", rank=1, section="context"),
            ServedItem(TARGET, rank=2, section="context"),
        ),
        helpful=frozenset({TARGET}),
        unhelpful=frozenset(),
    )

    result = evaluate_target(TARGET, [sectioned])

    assert sectioned.size == 3
    assert result.servings == 1
    assert result.helpful == 1
    assert result.baseline_rate == pytest.approx(1 / 3)


def test_helpful_wins_over_unhelpful_on_one_pack() -> None:
    pack = _pack("p", session="s", size=10, helpful=(TARGET,), unhelpful=(TARGET,))

    result = evaluate_target(TARGET, [pack])

    assert (result.helpful, result.unhelpful) == (1, 0)
    assert pack.verdict(TARGET) == "helpful"


def test_a_citation_of_an_item_the_pack_did_not_serve_is_not_in_the_null() -> None:
    pack = GradedPack(
        pack_id="p",
        session_id="s",
        served=(ServedItem(TARGET), ServedItem("a")),
        helpful=frozenset({TARGET, "never-served"}),
        unhelpful=frozenset(),
    )

    assert pack.helpful_served == 1
    assert evaluate_target(TARGET, [pack]).baseline_rate == pytest.approx(0.5)


def test_a_target_served_nowhere_fails_at_helpful_with_no_rates() -> None:
    result = evaluate_target("ghost", _helpful_in(4))

    assert result.servings == 0
    assert result.first_failed == CRITERION_HELPFUL
    assert result.target_rate is None
    assert result.lift is None


def test_later_criteria_are_not_computed_for_an_earlier_failure() -> None:
    result = evaluate_target(TARGET, _helpful_in(2, sessions=2))

    assert result.first_failed == CRITERION_HELPFUL
    assert result.p_value is None


# --------------------------------------------------------------- thresholds


def test_the_defaults_are_the_plan() -> None:
    t = ActivationThresholds()

    assert (t.min_helpful, t.min_distinct_sessions) == (3, 2)
    assert (t.max_p_value, t.min_lift, t.resamples) == (0.05, 1.2, 2000)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_helpful", 0),
        ("min_helpful", True),
        ("min_helpful", 2.5),
        ("min_distinct_sessions", 0),
        ("max_p_value", 0.0),
        ("max_p_value", 1.5),
        ("max_p_value", float("nan")),
        ("min_lift", 0.99),
        ("min_lift", float("inf")),
        ("resamples", 0),
        ("resamples", 10),  # cannot reach P < 0.05: 1 / 11 > 0.05
        ("resamples", 10**9),
        ("seed", 1.5),
    ],
)
def test_nonsense_thresholds_are_refused_with_the_setting_named(
    field: str, value: object
) -> None:
    with pytest.raises(ConfigError) as caught:
        ActivationThresholds(**{field: value})

    assert caught.value.setting == f"precedent_activation.{field}"


def test_thresholds_are_frozen() -> None:
    t = ActivationThresholds()
    with pytest.raises(AttributeError):
        t.min_helpful = 1  # type: ignore[misc]


def test_the_bare_permutation_is_seeded_and_bounded() -> None:
    pools = [(2, 1), (2, 1), (2, 1)]

    first = within_pack_permutation_p(
        3,
        pools,
        resamples=4000,
        rng=random.Random("k"),  # noqa: S311
    )
    again = within_pack_permutation_p(
        3,
        pools,
        resamples=4000,
        rng=random.Random("k"),  # noqa: S311
    )
    nothing_observed = within_pack_permutation_p(
        0,
        pools,
        resamples=100,
        rng=random.Random(1),  # noqa: S311
    )

    assert first == again
    assert first == pytest.approx(0.125, abs=0.03)
    assert nothing_observed == 1.0  # every resample is at least zero


def test_the_null_is_per_pack_not_a_rate_pooled_over_the_targets_packs() -> None:
    # Helpful in three two-item packs (one label each) and unjudged in one large
    # pack. Chance helps it in each small pack half the time, so three of four is
    # unremarkable: exactly 1/8 + 3/8 * 1/100, about 0.129. A rate pooled over the
    # four packs, 4 / 106, would make the same three look like a 2-in-10,000 event
    # and pass the 0.05 bound.
    packs = [
        *(_pack(f"s{i}", session=f"s{i}", size=2, helpful=(TARGET,)) for i in range(3)),
        _pack("big", session="b", size=100, extra_helpful=1),
    ]

    result = evaluate_target(TARGET, packs, ActivationThresholds(resamples=4000))

    assert result.helpful == 3
    assert result.p_value == pytest.approx(0.1287, abs=0.03)
    assert result.first_failed == CRITERION_P_VALUE
