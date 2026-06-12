"""Unit smoke + math pin for the degraded-retrieval convergence scenario.

The scenario's load-bearing claim is that the dual loop measurably
improves retrieval over time on a degraded corpus. The integration
test pins that climb against an in-memory SQLite registry; the math
tests pin the per-quarter trajectory helper independently so a
refactor that quietly breaks the slicing won't pass with a degenerate
trajectory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eval.scenarios.agent_loop_convergence_degraded.scenario import (
    _DISTRACTOR_DOCS,
    DEFAULT_DISTRACTORS_PER_DOMAIN,
    USEFUL_DELTA_CLIMB_THRESHOLD,
    _quarter_trajectory,
    _RoundResult,
    run,
)

from trellis.stores.registry import StoreRegistry


@pytest.fixture
def sqlite_registry(tmp_path: Path):
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    with StoreRegistry(config=config, stores_dir=tmp_path) as registry:
        yield registry


# ---------------------------------------------------------------------------
# Distractor pool invariants — corpus design must keep distractors out
# of the grader's coverage check, otherwise the scenario silently
# inflates its own success.
# ---------------------------------------------------------------------------


def test_distractor_pool_is_sized_correctly() -> None:
    """At least DEFAULT_DISTRACTORS_PER_DOMAIN entries per domain.

    The default scenario plants 15/domain; the pool needs to support
    that without exhausting. If the pool ever shrinks below the
    default, the scenario silently plants fewer distractors and the
    convergence climb gets weaker — catch that here.
    """
    for domain, docs in _DISTRACTOR_DOCS.items():
        assert len(docs) >= DEFAULT_DISTRACTORS_PER_DOMAIN, (
            f"{domain} pool has only {len(docs)} entries, "
            f"below the default plant of {DEFAULT_DISTRACTORS_PER_DOMAIN}"
        )


def test_distractor_doc_ids_are_disjoint_from_real_entities() -> None:
    """No distractor doc_id may collide with ``doc:<entity_id>``.

    The grader counts an item as covering an entity by exact doc_id
    match. If a distractor used a real-entity doc_id, the agent would
    silently get coverage credit for off-topic content and the loop
    couldn't distinguish them from real entities.
    """
    from eval.generators.trace_generator import DOMAIN_TEMPLATES

    real_doc_ids = {f"doc:{entity}" for t in DOMAIN_TEMPLATES for entity in t.entities}
    distractor_ids = {
        doc_id for docs in _DISTRACTOR_DOCS.values() for doc_id, _ in docs
    }
    assert real_doc_ids.isdisjoint(distractor_ids)


def test_distractor_doc_ids_are_unique_within_pool() -> None:
    """No two distractors may share a doc_id.

    Document store ``put`` is idempotent on doc_id — a duplicate would
    silently overwrite, producing fewer planted distractors than
    advertised. Pin uniqueness so the planting count is honest.
    """
    distractor_ids = [
        doc_id for docs in _DISTRACTOR_DOCS.values() for doc_id, _ in docs
    ]
    assert len(distractor_ids) == len(set(distractor_ids))


# ---------------------------------------------------------------------------
# End-to-end smoke — run a short version of the scenario and verify the
# expected metric shape + climb signal. Long enough to give the loop
# something to work with, short enough for a unit test.
# ---------------------------------------------------------------------------


def test_short_run_produces_expected_metric_shape(
    sqlite_registry: StoreRegistry,
) -> None:
    """Sanity-check that all expected metrics surface, status is sane,
    and the corpus was wired correctly. Doesn't gate the climb (that's
    the next test) — this is a wiring-fail catcher."""
    report = run(
        sqlite_registry,
        seed=0,
        rounds=40,
        feedback_batch_size=10,
        useful_delta_climb_threshold=-1.0,  # not gating climb here
    )

    assert report.name == "agent_loop_convergence_degraded"
    assert report.status in {"pass", "regress"}
    assert report.metrics["rounds"] == 40.0
    assert report.metrics["pack_max_items"] == 4.0
    expected_distractors = float(DEFAULT_DISTRACTORS_PER_DOMAIN * len(_DISTRACTOR_DOCS))
    assert report.metrics["distractors_planted"] == expected_distractors

    for key in (
        "convergence.useful_delta",
        "convergence.useful_first_quarter_mean",
        "convergence.useful_last_quarter_mean",
        "convergence.quarters.useful_q1_mean",
        "convergence.quarters.useful_q2_mean",
        "convergence.quarters.useful_q3_mean",
        "convergence.quarters.useful_q4_mean",
        "loops.effectiveness_runs",
        "loops.noise_items_tagged_total",
    ):
        assert key in report.metrics, f"missing {key}"


def test_default_run_demonstrates_useful_fraction_climb(
    sqlite_registry: StoreRegistry,
) -> None:
    """The load-bearing assertion: the dual loop climbs useful-fraction
    on a degraded corpus.

    Status ``pass`` requires ``useful_delta >= 0.10`` per the scenario's
    own gate. We additionally pin that at least *some* noise tags were
    applied — a passing useful_delta with zero noise tagging would mean
    the success path was an accident of the corpus, not the loop's work.

    This is the test that proves Trellis's "improves with use" claim on
    a controlled corpus. If it ever flakes or starts failing, treat it
    as a real regression in the dual-loop contract — not test noise.
    """
    report = run(sqlite_registry, seed=0)

    assert report.status == "pass", (
        f"degraded scenario returned status={report.status} — "
        f"useful_delta={report.metrics['convergence.useful_delta']}"
    )
    assert report.metrics["convergence.useful_delta"] >= USEFUL_DELTA_CLIMB_THRESHOLD
    assert report.metrics["loops.noise_items_tagged_total"] > 0, (
        "no noise items were tagged — the climb (if any) wasn't the loop's doing"
    )


def test_per_domain_useful_fraction_metrics_present(
    sqlite_registry: StoreRegistry,
) -> None:
    """Each of the three domains must surface a per-domain useful
    fraction so a degraded climb can be inspected per-domain — useful
    when one domain converges and another doesn't."""
    report = run(
        sqlite_registry,
        seed=0,
        rounds=40,
        feedback_batch_size=10,
        useful_delta_climb_threshold=-1.0,
    )
    for domain in ("software_engineering", "data_pipeline", "customer_support"):
        assert f"per_domain.{domain}.useful_fraction_mean" in report.metrics, (
            f"missing per_domain.{domain}.useful_fraction_mean"
        )


# ---------------------------------------------------------------------------
# Direct math tests — the trajectory helper deserves the same pin as
# _quarter_means in the baseline scenario. Adversarial-input coverage.
# ---------------------------------------------------------------------------


def _make_round(items_referenced: int, items_served: int = 4) -> _RoundResult:
    return _RoundResult(
        round_index=0,
        domain="x",
        pack_id="p",
        items_served=items_served,
        items_referenced=items_referenced,
        coverage_fraction=0.5,
        weighted_score=0.5,
        success=False,
    )


class TestQuarterTrajectory:
    """Pin :func:`_quarter_trajectory` against adversarial inputs."""

    def test_empty_returns_four_zeros(self) -> None:
        assert _quarter_trajectory([]) == [0.0, 0.0, 0.0, 0.0]

    def test_single_value_collapses_to_full_mean(self) -> None:
        """Below the four-sample threshold every quarter falls back to
        the full-sample mean — climb (q4-q1) is zero, not noise."""
        rounds = [_make_round(items_referenced=2, items_served=4)]
        traj = _quarter_trajectory(rounds)
        assert traj == [0.5, 0.5, 0.5, 0.5]

    def test_three_values_collapse_to_full_mean(self) -> None:
        rounds = [
            _make_round(items_referenced=1),
            _make_round(items_referenced=2),
            _make_round(items_referenced=3),
        ]
        traj = _quarter_trajectory(rounds)
        # mean of [0.25, 0.5, 0.75] = 0.5
        assert traj == [0.5, 0.5, 0.5, 0.5]

    def test_four_values_one_per_quarter(self) -> None:
        """At exactly four samples, quarter=1 so each slot pulls one
        round — the four trajectory values equal the four useful
        fractions in order."""
        rounds = [
            _make_round(items_referenced=0),  # 0/4 = 0.0
            _make_round(items_referenced=1),  # 0.25
            _make_round(items_referenced=2),  # 0.5
            _make_round(items_referenced=3),  # 0.75
        ]
        traj = _quarter_trajectory(rounds)
        assert traj == pytest.approx([0.0, 0.25, 0.5, 0.75])

    def test_eight_values_two_per_quarter(self) -> None:
        rounds = [_make_round(items_referenced=i) for i in range(8)]
        # useful = [0, 0.25, 0.5, 0.75, 1.0, 1.25?, ...] — items_referenced
        # > items_served gives >1, fine for a math test.
        traj = _quarter_trajectory(rounds)
        # quarter=2: indices [0:2], [2:4], [4:6], [6:8]
        # means: (0+0.25)/2, (0.5+0.75)/2, (1.0+1.25)/2, (1.5+1.75)/2
        assert traj[0] == pytest.approx(0.125)
        assert traj[1] == pytest.approx(0.625)
        assert traj[2] == pytest.approx(1.125)
        assert traj[3] == pytest.approx(1.625)

    def test_remainder_lands_in_last_quarter(self) -> None:
        """When rounds isn't divisible by 4, the extras must land in
        the last quarter — otherwise the run's tail (where convergence
        is supposed to be visible) gets discarded."""
        # 5 rounds: quarter=1, slices [0:1], [1:2], [2:3], [3:5]
        # Last quarter sees rounds 3 and 4.
        # Useful fractions: 0/4, 1/4, 2/4, 3/4, 4/4
        rounds = [_make_round(items_referenced=i) for i in range(5)]
        traj = _quarter_trajectory(rounds)
        assert traj[0] == pytest.approx(0.0)
        assert traj[1] == pytest.approx(0.25)
        assert traj[2] == pytest.approx(0.5)
        # last quarter avg = mean(0.75, 1.0) = 0.875
        assert traj[3] == pytest.approx(0.875)

    def test_zero_items_served_treated_as_zero_useful(self) -> None:
        """A pack with no items has no useful fraction to compute — the
        helper must guard against ZeroDivisionError, treating the round
        as 0.0 useful so an empty-pack failure doesn't inflate the
        climb."""
        rounds = [_make_round(items_referenced=0, items_served=0)] * 4
        traj = _quarter_trajectory(rounds)
        assert traj == [0.0, 0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_pack_max_items_must_be_positive(sqlite_registry: StoreRegistry) -> None:
    with pytest.raises(ValueError, match="pack_max_items must be positive"):
        run(sqlite_registry, pack_max_items=0)


def test_distractors_per_domain_must_be_positive(
    sqlite_registry: StoreRegistry,
) -> None:
    with pytest.raises(ValueError, match="distractors_per_domain must be positive"):
        run(sqlite_registry, distractors_per_domain=0)


def test_invalid_rounds_propagate(sqlite_registry: StoreRegistry) -> None:
    with pytest.raises(ValueError, match="rounds must be positive"):
        run(sqlite_registry, rounds=0)
    with pytest.raises(ValueError, match="feedback_batch_size must be positive"):
        run(sqlite_registry, feedback_batch_size=0)


def test_module_constants_sane() -> None:
    assert DEFAULT_DISTRACTORS_PER_DOMAIN > 0
    assert USEFUL_DELTA_CLIMB_THRESHOLD > 0  # must be a positive climb gate
