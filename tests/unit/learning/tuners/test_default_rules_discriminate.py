"""``DEFAULT_RULES`` must be able to discriminate, and that is provable.

A tuning rule has two ways to be useless and they look identical from
the outside — the tuner runs, reports success, and proposes nothing
interesting:

* **constant false** — no cell ever clears the sample floors, so the
  threshold is never consulted.  This is what #560 produced: filling in
  the ``domain`` / ``intent_family`` axes split one 52-row cell per
  component into 24, the largest holding 10, against a floor of 30.
* **constant true** — every qualifying cell clears the threshold, so
  firing carries no information.  This is what the *same* rule set did
  before those axes were filled, when one cell per component held every
  row and every rule fired on 100% of cells.

Neither state is a measurement, and a suite that only asks "does this
rule fire on a cell I built to make it fire?" cannot tell either one
from a working rule.  So these tests run the **shipped** predicate
(:meth:`TuningRule.applies`) over the **measured** production cell
population and require it to separate that population — and then prove
the separation check itself is not vacuous by feeding it rules that are
constants by construction.

The population is a snapshot, not a live probe: the 24 GraphSearch
learning-axis cells on the reference deployment over the 30 days to
2026-09-12.  Its job is to pin that the shipped thresholds discriminated
over a real distribution rather than a hand-built one; it is not a claim
about production today.  Re-derive it (``docs`` on #562, or the query in
the PR) before retuning.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from trellis.learning.tuners import DEFAULT_RULES, TuningRule
from trellis.learning.tuners.rule_tuner import AggregatedOutcomes
from trellis.schemas.outcome import (
    COMPONENT_ID_BY_SOURCE_STRATEGY,
    GRAPH_SEARCH_COMPONENT_ID,
    PACK_BUILDER_COMPONENT_ID,
    RRF_RERANKER_COMPONENT_ID,
)
from trellis.schemas.parameters import ParameterScope

#: Component ids an ``OutcomeEvent`` can actually carry, derived rather
#: than listed: the per-strategy fan-out stamps whatever
#: ``COMPONENT_ID_BY_SOURCE_STRATEGY`` maps to, and the pack-level bridge
#: stamps the pack builder.  A rule targeting anything else cannot match
#: an aggregate no matter what the data does.
REACHABLE_COMPONENT_IDS = frozenset(COMPONENT_ID_BY_SOURCE_STRATEGY.values()) | {
    PACK_BUILDER_COMPONENT_ID
}

#: Every GraphSearch cell measured on the reference deployment over the
#: 30 days to 2026-09-12, as ``(rows, items_served, items_referenced)``.
#: 53 pack-targeted feedback events fanned out to 152 per-strategy
#: outcome rows; GraphSearch's share is 52 rows over these 24 cells,
#: 205 servings and 27 citations — a base rate of 0.1317.
MEASURED_GRAPH_CELLS: tuple[tuple[int, int, int], ...] = (
    (10, 42, 1),
    (4, 21, 4),
    (4, 17, 3),
    (2, 12, 3),
    (2, 10, 2),
    (2, 10, 3),
    (2, 10, 0),
    (1, 9, 1),
    (2, 8, 2),
    (2, 8, 0),
    (1, 7, 1),
    (5, 6, 1),
    (2, 5, 2),
    (1, 5, 0),
    (3, 4, 0),
    (1, 4, 1),
    (1, 4, 0),
    (1, 4, 1),
    (1, 4, 0),
    (1, 4, 0),
    (1, 3, 0),
    (1, 3, 0),
    (1, 3, 2),
    (1, 2, 0),
)


def _cells(
    component_id: str = GRAPH_SEARCH_COMPONENT_ID,
) -> list[AggregatedOutcomes]:
    """Rebuild the measured population as real aggregates.

    Each cell gets a distinct ``domain`` so they stay separate scopes,
    mirroring the axes #559/#560 fill in.
    """
    return [
        AggregatedOutcomes(
            scope=ParameterScope(component_id=component_id, domain=f"d{i}"),
            count=rows,
            items_served_total=served,
            items_referenced_total=referenced,
        )
        for i, (rows, served, referenced) in enumerate(MEASURED_GRAPH_CELLS)
    ]


def _separate(
    rule: TuningRule, cells: list[AggregatedOutcomes]
) -> tuple[int, int, int]:
    """Return ``(total, qualifying, firing)`` for a rule over a population.

    ``qualifying`` counts cells that clear *both* floors — kept distinct
    from ``firing`` so an unreachable floor and an unreachable threshold
    cannot be confused for each other.  Note this calls the shipped
    ``applies`` for the firing count rather than re-implementing the
    comparison, so a rule that stops working stops passing.
    """
    qualifying = [
        c
        for c in cells
        if c.count >= rule.min_sample_size
        and c.items_served_total >= rule.min_items_served
    ]
    firing = [c for c in cells if rule.applies(c)]
    return len(cells), len(qualifying), len(firing)


# ---------------------------------------------------------------------------
# Reachability — a rule that cannot match any row is not a rule
# ---------------------------------------------------------------------------


def test_default_rules_is_not_empty():
    # Every other test here divides by this population; a rule set that
    # merely shrank to nothing would satisfy all of them vacuously.
    assert len(DEFAULT_RULES) >= 1


@pytest.mark.parametrize("rule", DEFAULT_RULES, ids=lambda r: r.name)
def test_default_rule_targets_a_reachable_component(rule: TuningRule):
    assert rule.target_component_id in REACHABLE_COMPONENT_IDS


def test_reachability_check_rejects_a_reranker_rule():
    """Vacuity guard for the test above.

    Rerankers are deliberately absent from
    ``COMPONENT_ID_BY_SOURCE_STRATEGY`` — a reranker reorders every
    candidate and serves none under its own name — so a rule targeting
    one can never match.  ``rrf_low_success_reduce_smoothing`` shipped in
    ``DEFAULT_RULES`` in exactly that state.  If this assertion ever
    fails, the map gained rerankers and the test above stopped being a
    constraint.
    """
    assert RRF_RERANKER_COMPONENT_ID not in REACHABLE_COMPONENT_IDS


# ---------------------------------------------------------------------------
# A rate rule must floor on the rate's own denominator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", DEFAULT_RULES, ids=lambda r: r.name)
def test_rate_keyed_rules_floor_on_their_denominator(rule: TuningRule):
    """``min_sample_size`` counts rows; ``reference_rate`` divides by servings.

    Flooring a rate rule on row count alone admits cells whose rate is
    computed over a handful of items — on the measured population, a
    cell of 4 servings and 0 citations, where zero is what GraphSearch's
    own base rate produces 57% of the time.
    """
    if rule.condition_key == "reference_rate":
        assert rule.min_items_served >= 1, (
            f"{rule.name} keys on reference_rate but floors only on row count"
        )


def test_denominator_floor_excludes_the_measured_noise_cell():
    """The floor is load-bearing, not decorative.

    Without it the shipped rule fires on the 4-serving / 0-citation
    cell — proposing a parameter change off an absence of evidence,
    which is the shape that broke the demotion gate (#336).
    """
    (rule,) = [r for r in DEFAULT_RULES if r.condition_key == "reference_rate"]
    noise_cell = AggregatedOutcomes(
        scope=ParameterScope(component_id=rule.target_component_id, domain="noise"),
        count=3,
        items_served_total=4,
        items_referenced_total=0,
    )

    assert not rule.applies(noise_cell)

    unfloored = replace(rule, min_items_served=0)
    assert unfloored.applies(noise_cell)


# ---------------------------------------------------------------------------
# Discrimination over the measured population
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", DEFAULT_RULES, ids=lambda r: r.name)
def test_default_rule_separates_the_measured_population(rule: TuningRule):
    total, qualifying, firing = _separate(rule, _cells(rule.target_component_id))

    assert qualifying >= 1, (
        f"{rule.name}: no cell in {total} clears its floors — the rule is a "
        "constant false and its threshold is never consulted"
    )
    assert firing >= 1, (
        f"{rule.name}: fires on none of {qualifying} qualifying cells — "
        "constant false at the threshold"
    )
    assert firing < qualifying, (
        f"{rule.name}: fires on all {qualifying} qualifying cells — constant "
        "true, so firing carries no information"
    )


def test_the_shipped_graph_rule_fires_on_exactly_the_measured_outlier():
    """Pin the measured verdict, not just that *some* separation exists.

    Of the two cells clearing both floors, the rule fires on the one
    serving 42 graph items for a single citation — which under
    GraphSearch's own 0.1317 base rate has probability 0.020 — and
    spares the one citing at 0.1905, above base.
    """
    (rule,) = [r for r in DEFAULT_RULES if r.condition_key == "reference_rate"]
    total, qualifying, firing = _separate(rule, _cells())

    assert (total, qualifying, firing) == (24, 2, 1)


@pytest.mark.parametrize(
    ("threshold", "label"),
    [(0.0, "constant false"), (1.0, "constant true")],
)
def test_separation_check_catches_a_constant_rule(threshold: float, label: str):
    """Vacuity guard for the discrimination tests.

    A check that cannot fail proves nothing, so run it against rules
    that are constants by construction over this same population and
    require it to say so.  0.0 is below every measured rate (nothing
    fires); 1.0 is above every one (everything does).
    """
    (shipped,) = [r for r in DEFAULT_RULES if r.condition_key == "reference_rate"]
    constant = replace(shipped, condition_value=threshold)

    _total, qualifying, firing = _separate(constant, _cells())

    assert qualifying >= 1  # the floors still admit cells; only the threshold broke
    if threshold == 0.0:
        assert firing == 0, f"expected {label}"
    else:
        assert firing == qualifying, f"expected {label}"


def test_separation_check_catches_an_unreachable_floor():
    """The other constant: a floor no cell clears.

    30 was the shipped ``min_sample_size`` and the largest measured cell
    holds 10 rows, so this is the exact state #560 created rather than a
    hypothetical one.
    """
    (shipped,) = [r for r in DEFAULT_RULES if r.condition_key == "reference_rate"]
    unreachable = replace(shipped, min_sample_size=30)

    _total, qualifying, firing = _separate(unreachable, _cells())

    assert qualifying == 0
    assert firing == 0
