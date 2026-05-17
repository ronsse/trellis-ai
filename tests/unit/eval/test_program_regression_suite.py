"""Unit tests for the program_regression_suite scenario.

Phase 5B exercised ``_assert_axis_a`` and the ``run()`` profile gate.
Phase 5C (this file) extends coverage to the remaining eight axis
helpers and to ``_call_satellite`` so every regression-assertion path
gets hit by a unit test instead of waiting for the full 50-round
operator run.

Axes B, C, E delegate to the generic helpers ``_assert_delta_threshold``
and ``_assert_last_quarter_threshold`` rather than carrying a dedicated
``_assert_axis_b/c/e`` symbol. The tests below pin the generic-helper
behaviour against each axis's track label + threshold so the per-axis
contract still has coverage even though the implementation factors
through one helper. Axes D, F, G, H, I have named helpers and get
happy-path + regress tests apiece. ``_call_satellite`` has tests for
each of its four code paths (import-error / lookup-error /
execute-error / success).

The suite's end-to-end run takes minutes (50 rounds + 4 satellites)
and is operator-invoked, not part of ``pytest tests/`` — these tests
exercise the helpers directly so the CI gate stays fast.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest
from eval.runner import Finding, ScenarioReport
from eval.scenarios._convergence_common import _AxisTrack
from eval.scenarios.program_convergence.scenario import _RoundResult
from eval.scenarios.program_regression_suite.scenario import (
    DEFAULT_CORPUS_PROFILE,
    ROUND_D_CUTOFF,
    ROUND_F_MIDPOINT,
    ROUND_G_CUTOFF,
    ROUND_I_CUTOFF,
    THRESHOLD_A_DELTA_BY_PROFILE,
    THRESHOLD_B_DELTA,
    THRESHOLD_C_LAST_QUARTER,
    THRESHOLD_D_PER_SEED_BY_R25,
    THRESHOLD_E_LAST_QUARTER,
    THRESHOLD_G_BY_R30,
    THRESHOLD_H_MAX_PER_ROUND,
    THRESHOLD_I_BY_R40,
    _assert_axis_a,
    _assert_axis_d,
    _assert_axis_f,
    _assert_axis_g,
    _assert_axis_h,
    _assert_axis_i,
    _assert_delta_threshold,
    _assert_last_quarter_threshold,
    _call_satellite,
    run,
)


def _make_round(
    round_index: int,
    *,
    pack_quality: float,
    success: bool = True,
    useful_item_fraction: float = 0.5,
    advisory_hit_rate: float = 0.5,
    observation_enrichment: float = 0.0,
    provenance_queryability: float = 1.0,
    extraction_failure_clusters: float = 0.0,
    schema_evolution_candidates: float = 0.0,
    meta_trace_density: float = 0.0,
    self_authored_proposals: float = 0.0,
) -> _RoundResult:
    """Build a ``_RoundResult`` with each axis individually addressable.

    Phase 5B fixed pack_quality + neutral defaults for the seven other
    axes. Phase 5C tests need to drive each axis independently, so
    every ``axis_*`` field is keyword-overridable with a documented
    neutral default that lands inside each axis's PASS band.
    """
    return _RoundResult(
        round_index=round_index,
        domain="synthetic",
        pack_id=f"pack-{round_index}",
        items_served=10,
        items_referenced=5,
        coverage_fraction=0.5,
        weighted_score=pack_quality,
        success=success,
        axis_pack_quality=pack_quality,
        axis_useful_item_fraction=useful_item_fraction,
        axis_advisory_hit_rate=advisory_hit_rate,
        axis_observation_enrichment=observation_enrichment,
        axis_provenance_queryability=provenance_queryability,
        axis_extraction_failure_clusters=extraction_failure_clusters,
        axis_schema_evolution_candidates=schema_evolution_candidates,
        axis_meta_trace_density=meta_trace_density,
        axis_self_authored_proposals=self_authored_proposals,
    )


def _rising_axis_a_rounds(
    *, start: float, end: float, count: int = 8
) -> list[_RoundResult]:
    """Build ``count`` rounds with axis A rising linearly from start to end.

    With ``count == 8`` the quarter window size is ``8 // 4 == 2`` so the
    first-quarter mean is ``mean(rounds[0], rounds[1])`` and the
    last-quarter mean is ``mean(rounds[6], rounds[7])``. Picking the
    start/end values controls the delta the helper sees exactly.
    """
    step = (end - start) / (count - 1)
    return [_make_round(i, pack_quality=start + step * i) for i in range(count)]


# ---------------------------------------------------------------------------
# Profile threshold map sanity
# ---------------------------------------------------------------------------


def test_default_profile_is_synthetic() -> None:
    """Default matches CI's deterministic corpus — switching is opt-in."""
    assert DEFAULT_CORPUS_PROFILE == "synthetic"


def test_threshold_map_contains_synthetic_and_real() -> None:
    """Both profiles surface, with the documented numeric values."""
    assert THRESHOLD_A_DELTA_BY_PROFILE["synthetic"] == 0.05
    assert THRESHOLD_A_DELTA_BY_PROFILE["real"] == 0.15


def test_synthetic_threshold_strictly_below_real() -> None:
    """Synthetic must be looser than real — otherwise the split is pointless."""
    assert (
        THRESHOLD_A_DELTA_BY_PROFILE["synthetic"] < THRESHOLD_A_DELTA_BY_PROFILE["real"]
    )


# ---------------------------------------------------------------------------
# _assert_axis_a — profile selection
# ---------------------------------------------------------------------------


def test_assert_axis_a_synthetic_passes_with_delta_above_synthetic_threshold() -> None:
    """A ~0.086 lift PASSES synthetic (0.05) but is below real (0.15).

    With ``start=0.90``, ``end=1.0``, ``count=8`` the linear interpolation
    yields a first-quarter mean of ~0.907 and last-quarter mean of
    ~0.993 → delta ~0.086. That's the band the production synthetic
    corpus operates in.
    """
    rounds = _rising_axis_a_rounds(start=0.90, end=1.0, count=8)
    result = _assert_axis_a(rounds, profile="synthetic")
    assert result.label == "A_pack_quality"
    assert result.passed is True
    assert result.detail["profile"] == "synthetic"
    assert result.detail["threshold"] == 0.05
    assert "profile=synthetic" in result.expected_message
    # The actual delta must comfortably clear the synthetic floor.
    assert result.detail["delta"] > 0.05


def test_assert_axis_a_real_fails_when_lift_is_synthetic_sized() -> None:
    """Same ~0.086 lift FAILS under real (0.15) — proves the split bites."""
    rounds = _rising_axis_a_rounds(start=0.90, end=1.0, count=8)
    result = _assert_axis_a(rounds, profile="real")
    assert result.passed is False
    assert result.detail["profile"] == "real"
    assert result.detail["threshold"] == 0.15
    assert "profile=real" in result.expected_message
    # delta must be below 0.15 but above 0.05 — that's the whole point.
    assert 0.05 < result.detail["delta"] < 0.15


def test_assert_axis_a_real_passes_with_real_sized_lift() -> None:
    """A ~0.43 lift PASSES under real — sanity check the real branch works.

    ``start=0.35``, ``end=0.85`` yields delta ~0.428, comfortably above
    the plan §4.2 0.15 target for noisy real corpora.
    """
    rounds = _rising_axis_a_rounds(start=0.35, end=0.85, count=8)
    result = _assert_axis_a(rounds, profile="real")
    assert result.passed is True
    assert result.detail["profile"] == "real"
    assert result.detail["delta"] > 0.15


def test_assert_axis_a_synthetic_fails_on_flat_curve() -> None:
    """Synthetic 0.05 floor still catches a genuinely flat curve.

    A near-flat axis A curve (delta well below 0.05) should still
    regress under the synthetic profile — the threshold is loose,
    not absent.
    """
    rounds = _rising_axis_a_rounds(start=0.94, end=0.945, count=8)
    result = _assert_axis_a(rounds, profile="synthetic")
    assert result.passed is False
    assert result.detail["profile"] == "synthetic"
    assert result.detail["delta"] < 0.05


# ---------------------------------------------------------------------------
# run() — profile validation (top-level entry point)
# ---------------------------------------------------------------------------


def test_run_rejects_invalid_profile() -> None:
    """Unknown profile → loud ValueError, no silent fallback to synthetic."""
    registry = MagicMock()
    with pytest.raises(ValueError, match="unknown corpus profile"):
        run(registry, profile="hybrid")  # type: ignore[arg-type]


def test_run_rejects_empty_profile_string() -> None:
    """Empty string → loud ValueError. POC directive: loud on misuse."""
    registry = MagicMock()
    with pytest.raises(ValueError, match="unknown corpus profile"):
        run(registry, profile="")  # type: ignore[arg-type]


def test_run_validates_profile_before_running_master() -> None:
    """Profile validation fires before ``_drive_master`` — fail fast.

    A typo in the profile name shouldn't waste 50 rounds of compute.
    The ValueError must raise before any expensive work; checking that
    the registry was never touched (no attribute access on the mock)
    proves the validation runs first.
    """
    registry = MagicMock()
    with pytest.raises(ValueError):
        run(registry, profile="bogus")  # type: ignore[arg-type]
    # If validation ran first the master driver never reached the
    # registry. ``_verify_axis_machinery`` would have accessed
    # ``operational.event_log`` — assert it didn't.
    registry.operational.event_log.__bool__.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers for the remaining axis tests
# ---------------------------------------------------------------------------


def _track_from_values(label: str, values: list[float]) -> _AxisTrack:
    """Build an ``_AxisTrack`` populated with one record per value.

    The track's first-/last-quarter math is what
    ``_assert_delta_threshold`` and ``_assert_last_quarter_threshold``
    read; feeding a small sequence (>= 4 values) is enough to drive
    deterministic quarter means in a unit test.
    """
    track = _AxisTrack(axis=label)
    for round_index, value in enumerate(values):
        track.record(round_index, value)
    return track


# ---------------------------------------------------------------------------
# Axis B — useful-item fraction lift via _assert_delta_threshold
# Axis B has no named helper; it routes through _assert_delta_threshold
# with the B track + THRESHOLD_B_DELTA (0.10). The contract under test is
# the generic helper's behaviour at axis-B's threshold band.
# ---------------------------------------------------------------------------


def test_assert_axis_b_passes_with_delta_above_threshold() -> None:
    """A 0.0 → 0.4 lift comfortably clears the axis-B 0.10 threshold."""
    track = _track_from_values(
        "B_useful_item_fraction", [0.0, 0.05, 0.1, 0.2, 0.3, 0.35, 0.38, 0.42]
    )
    result = _assert_delta_threshold(track, "B_useful_item_fraction", THRESHOLD_B_DELTA)
    assert result.label == "B_useful_item_fraction"
    assert result.passed is True
    # The helper omits a profile when called without one; axes B/C/E
    # are profile-agnostic per plan §4.2.
    assert "profile" not in result.expected_message
    assert result.detail["delta"] >= THRESHOLD_B_DELTA
    assert result.detail["threshold"] == THRESHOLD_B_DELTA


def test_assert_axis_b_regresses_when_lift_below_threshold() -> None:
    """A flat curve (delta ~0) fails the axis-B 0.10 threshold."""
    track = _track_from_values(
        "B_useful_item_fraction", [0.3, 0.31, 0.32, 0.31, 0.30, 0.31, 0.32, 0.33]
    )
    result = _assert_delta_threshold(track, "B_useful_item_fraction", THRESHOLD_B_DELTA)
    assert result.passed is False
    assert result.detail["delta"] < THRESHOLD_B_DELTA


# ---------------------------------------------------------------------------
# Axis C — advisory hit rate via _assert_last_quarter_threshold
# Axis C has no named helper; it routes through _assert_last_quarter_threshold
# with the C track + THRESHOLD_C_LAST_QUARTER. The contract under test is
# "last-quarter mean clears the absolute bar". Unit D1 (2026-05-16)
# dropped the threshold to 0.0 — see the constant's docstring for the
# rationale (synthetic corpus doesn't yet exercise the per-item
# provenance path that the tightened axis C measures).
# ---------------------------------------------------------------------------


def test_assert_axis_c_passes_when_last_quarter_clears_threshold() -> None:
    """Last-quarter mean of 0.8 clears the axis-C threshold (post-D1: 0.0).

    The synthetic baseline lands at 0.0 today (no advisories carry the
    ``entity_id`` C1 provenance needs to stamp items), so the threshold
    is 0.0 and any non-negative last-quarter mean passes. A 0.85 mean
    locks in that the helper still asserts ``last_quarter_mean >=
    threshold`` rather than equality.
    """
    track = _track_from_values(
        "C_advisory_hit_rate", [0.2, 0.3, 0.4, 0.5, 0.7, 0.75, 0.8, 0.85]
    )
    result = _assert_last_quarter_threshold(
        track, "C_advisory_hit_rate", THRESHOLD_C_LAST_QUARTER
    )
    assert result.label == "C_advisory_hit_rate"
    assert result.passed is True
    assert result.detail["last_quarter_mean"] >= THRESHOLD_C_LAST_QUARTER


def test_assert_axis_c_regresses_when_last_quarter_below_explicit_bar() -> None:
    """Helper still flips ``passed=False`` when an explicit bar isn't met.

    With ``THRESHOLD_C_LAST_QUARTER`` now at 0.0, the synthetic value
    can no longer naturally fall below the production bar — so this
    test exercises the helper's regress branch directly by passing an
    explicit non-zero threshold (matching the pre-D1 0.6 calibration).
    Locks in that ``_assert_last_quarter_threshold`` continues to drive
    the regress finding when a downstream consumer (e.g., the
    operator-supplied stricter bar mooted in TODO.md) feeds in a higher
    threshold.
    """
    explicit_higher_bar = 0.6
    track = _track_from_values(
        "C_advisory_hit_rate", [0.3, 0.32, 0.34, 0.33, 0.35, 0.36, 0.34, 0.35]
    )
    result = _assert_last_quarter_threshold(
        track, "C_advisory_hit_rate", explicit_higher_bar
    )
    assert result.passed is False
    assert result.detail["last_quarter_mean"] < explicit_higher_bar


# ---------------------------------------------------------------------------
# Axis D — observations per seed by round 25
# ---------------------------------------------------------------------------


def test_assert_axis_d_passes_when_observations_clear_per_seed_threshold() -> None:
    """50 obs across 30 rounds / 4 seeds = 12.5 per seed → clears 10.0.

    Only the first ROUND_D_CUTOFF+1 rounds count toward the cutoff; we
    distribute 50 observations across rounds 0..25 (within the window)
    so the per-seed math lands well above the threshold.
    """
    # 26 rounds * ~2 obs each ≈ 52 total → /4 seeds = 13, passes.
    rounds = [
        _make_round(i, pack_quality=0.5, observation_enrichment=2.0)
        for i in range(ROUND_D_CUTOFF + 1)
    ]
    result = _assert_axis_d(rounds, seed_entity_count=4)
    assert result.label == "D_observation_enrichment"
    assert result.passed is True
    assert result.detail["per_seed_average"] >= THRESHOLD_D_PER_SEED_BY_R25
    assert result.detail["seed_entity_count"] == 4


def test_assert_axis_d_regresses_when_per_seed_below_threshold() -> None:
    """Tiny per-round counts → below the 10.0-per-seed threshold."""
    rounds = [
        _make_round(i, pack_quality=0.5, observation_enrichment=0.1)
        for i in range(ROUND_D_CUTOFF + 1)
    ]
    result = _assert_axis_d(rounds, seed_entity_count=4)
    assert result.passed is False
    assert result.detail["per_seed_average"] < THRESHOLD_D_PER_SEED_BY_R25


def test_assert_axis_d_handles_empty_seed_entities() -> None:
    """``seed_entity_count <= 0`` short-circuits to a regress finding.

    Division-by-zero defence: if the corpus generator returned no
    seed entities the per-seed math is undefined, so the helper must
    fail loud rather than emit ``inf``.
    """
    rounds = [_make_round(i, pack_quality=0.5) for i in range(ROUND_D_CUTOFF + 1)]
    result = _assert_axis_d(rounds, seed_entity_count=0)
    assert result.passed is False
    assert result.actual == 0.0
    assert "no seed entities" in result.detail["reason"]


# ---------------------------------------------------------------------------
# Axis E — provenance queryability via _assert_last_quarter_threshold
# ---------------------------------------------------------------------------


def test_assert_axis_e_passes_when_last_quarter_at_one() -> None:
    """1.0-flat last quarter clears the axis-E 1.0 bar (post-Item 2)."""
    track = _track_from_values(
        "E_provenance_queryability", [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    )
    result = _assert_last_quarter_threshold(
        track, "E_provenance_queryability", THRESHOLD_E_LAST_QUARTER
    )
    assert result.label == "E_provenance_queryability"
    assert result.passed is True
    assert result.detail["last_quarter_mean"] == 1.0


def test_assert_axis_e_regresses_when_last_quarter_below_one() -> None:
    """A pre-Item-2 (or regressed) axis-E sitting at 0.5 fails the 1.0 bar."""
    track = _track_from_values(
        "E_provenance_queryability", [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    )
    result = _assert_last_quarter_threshold(
        track, "E_provenance_queryability", THRESHOLD_E_LAST_QUARTER
    )
    assert result.passed is False
    assert result.detail["last_quarter_mean"] < THRESHOLD_E_LAST_QUARTER


# ---------------------------------------------------------------------------
# Axis F — extraction-failure cluster decay
# ---------------------------------------------------------------------------


def test_assert_axis_f_passes_when_final_round_below_midpoint() -> None:
    """A declining cluster count passes — 10 @ r25, 3 at final round."""
    rounds = []
    # Pad to >ROUND_F_MIDPOINT rounds so the helper reaches the active branch.
    for i in range(ROUND_F_MIDPOINT + 10):
        clusters = 10.0 if i == ROUND_F_MIDPOINT else 3.0
        rounds.append(
            _make_round(i, pack_quality=0.5, extraction_failure_clusters=clusters)
        )
    result = _assert_axis_f(rounds)
    assert result.label == "F_extraction_failure_clusters"
    assert result.passed is True
    assert result.detail[f"round_{ROUND_F_MIDPOINT}_count"] == 10.0
    assert result.detail["final_round_count"] == 3.0
    assert result.detail["delta"] < 0


def test_assert_axis_f_regresses_when_final_round_exceeds_midpoint() -> None:
    """Growing cluster count fails — 3 @ r25, 10 at final round."""
    rounds = []
    for i in range(ROUND_F_MIDPOINT + 10):
        clusters = 3.0 if i == ROUND_F_MIDPOINT else 10.0
        rounds.append(
            _make_round(i, pack_quality=0.5, extraction_failure_clusters=clusters)
        )
    result = _assert_axis_f(rounds)
    assert result.passed is False
    assert result.detail["delta"] > 0


def test_assert_axis_f_handles_insufficient_rounds() -> None:
    """Fewer rounds than the midpoint short-circuits to a regress finding."""
    rounds = [_make_round(i, pack_quality=0.5) for i in range(ROUND_F_MIDPOINT - 1)]
    result = _assert_axis_f(rounds)
    assert result.passed is False
    assert result.actual == 0.0
    assert "insufficient rounds" in result.detail["reason"]


# ---------------------------------------------------------------------------
# Axis G — schema-evolution candidates by round 30
# ---------------------------------------------------------------------------


def test_assert_axis_g_passes_when_candidates_clear_threshold() -> None:
    """At least one candidate event in rounds 0..30 clears the bar."""
    rounds = [
        _make_round(
            i,
            pack_quality=0.5,
            schema_evolution_candidates=1.0 if i == 15 else 0.0,
        )
        for i in range(ROUND_G_CUTOFF + 5)
    ]
    result = _assert_axis_g(rounds)
    assert result.label == "G_schema_evolution_candidates"
    assert result.passed is True
    assert result.detail["total_candidates_by_r30"] >= THRESHOLD_G_BY_R30


def test_assert_axis_g_regresses_when_no_candidates_in_window() -> None:
    """Zero candidates by round 30 → fails."""
    rounds = [
        _make_round(i, pack_quality=0.5, schema_evolution_candidates=0.0)
        for i in range(ROUND_G_CUTOFF + 5)
    ]
    result = _assert_axis_g(rounds)
    assert result.passed is False
    assert result.detail["total_candidates_by_r30"] == 0.0


# ---------------------------------------------------------------------------
# Axis H — meta-trace density cap
# ---------------------------------------------------------------------------


def test_assert_axis_h_passes_when_max_per_round_under_cap() -> None:
    """Bounded meta-trace density passes the sampling-cap regression test."""
    rounds = [
        _make_round(i, pack_quality=0.5, meta_trace_density=10.0) for i in range(20)
    ]
    result = _assert_axis_h(rounds)
    assert result.label == "H_meta_trace_density"
    assert result.passed is True
    assert result.detail["max_per_round"] <= THRESHOLD_H_MAX_PER_ROUND


def test_assert_axis_h_regresses_when_one_round_exceeds_cap() -> None:
    """A single round breaching the cap fails the whole assertion."""
    rounds = [
        _make_round(i, pack_quality=0.5, meta_trace_density=10.0) for i in range(20)
    ]
    # One bad apple — Item 6's sampling cap broke this round.
    rounds[5] = _make_round(5, pack_quality=0.5, meta_trace_density=120.0)
    result = _assert_axis_h(rounds)
    assert result.passed is False
    assert result.detail["max_per_round"] > THRESHOLD_H_MAX_PER_ROUND


# ---------------------------------------------------------------------------
# Axis I — self-authored proposal ratio
# ---------------------------------------------------------------------------


def test_assert_axis_i_passes_when_proposals_match_cluster_peak() -> None:
    """Proposal/cluster ratio ≥ 1.0 within window passes."""
    rounds = []
    for i in range(ROUND_I_CUTOFF + 5):
        clusters = 5.0 if i <= ROUND_I_CUTOFF else 0.0
        # 6 proposals spread across the window — ratio = 6/5 ≥ 1.0.
        proposals = 1.0 if i in {2, 8, 12, 20, 30, 38} else 0.0
        rounds.append(
            _make_round(
                i,
                pack_quality=0.5,
                extraction_failure_clusters=clusters,
                self_authored_proposals=proposals,
            )
        )
    result = _assert_axis_i(rounds)
    assert result.label == "I_self_authored_proposals"
    assert result.passed is True
    assert result.detail["ratio"] >= THRESHOLD_I_BY_R40


def test_assert_axis_i_regresses_when_proposals_below_cluster_peak() -> None:
    """Cluster peak of 10 with only 3 proposals → ratio 0.3 < 1.0, fails."""
    rounds = []
    for i in range(ROUND_I_CUTOFF + 5):
        clusters = 10.0 if i <= ROUND_I_CUTOFF else 0.0
        proposals = 1.0 if i in {5, 15, 25} else 0.0
        rounds.append(
            _make_round(
                i,
                pack_quality=0.5,
                extraction_failure_clusters=clusters,
                self_authored_proposals=proposals,
            )
        )
    result = _assert_axis_i(rounds)
    assert result.passed is False
    assert result.detail["ratio"] < THRESHOLD_I_BY_R40


def test_assert_axis_i_handles_silent_cluster_surfacer() -> None:
    """Zero cluster peak → regress with a descriptive reason.

    If axis F never rises, the seeder may be silent — division-by-zero
    defence emits a regress Finding rather than ``inf`` proposals per
    cluster.
    """
    rounds = [
        _make_round(
            i,
            pack_quality=0.5,
            extraction_failure_clusters=0.0,
            self_authored_proposals=1.0,
        )
        for i in range(ROUND_I_CUTOFF + 5)
    ]
    result = _assert_axis_i(rounds)
    assert result.passed is False
    assert result.detail["cluster_peak_by_r40"] == 0.0
    assert "no clusters surfaced" in result.detail["reason"]


# ---------------------------------------------------------------------------
# _call_satellite — import / lookup / execute / success paths
# ---------------------------------------------------------------------------


def test_call_satellite_returns_fail_on_import_error() -> None:
    """A non-existent module surfaces as a ``stage=import`` failure.

    The helper catches ``Exception`` not just ``ImportError`` because
    some satellites may raise at module-import time for reasons other
    than missing imports (e.g., decorator side-effects). Pinning the
    return shape here regression-protects the operator-facing detail.
    """
    passed, detail = _call_satellite("eval.scenarios.does_not_exist_xyz")
    assert passed is False
    assert detail["module"] == "eval.scenarios.does_not_exist_xyz"
    assert detail["stage"] == "import"
    assert "ModuleNotFoundError" in detail["error"]
    assert "traceback" in detail


def test_call_satellite_returns_fail_when_module_has_no_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module without a callable ``run`` attribute fails at ``stage=lookup``."""
    module_name = "test_satellite_no_run"
    fake = ModuleType(module_name)
    # No ``run`` attribute at all.
    monkeypatch.setitem(sys.modules, module_name, fake)
    passed, detail = _call_satellite(module_name)
    assert passed is False
    assert detail["module"] == module_name
    assert detail["stage"] == "lookup"
    assert "no callable run" in detail["error"]


def test_call_satellite_returns_fail_when_run_is_not_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-callable ``run`` attribute fails at ``stage=lookup``.

    Some satellites might shadow ``run`` with a string/constant by
    mistake; the helper must reject that case rather than crashing
    inside ``run()``.
    """
    module_name = "test_satellite_run_not_callable"
    fake = ModuleType(module_name)
    fake.run = "i am not callable"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake)
    passed, detail = _call_satellite(module_name)
    assert passed is False
    assert detail["stage"] == "lookup"


def test_call_satellite_returns_fail_when_run_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A satellite whose ``run()`` raises fails at ``stage=execute``.

    Exception type + message + traceback are surfaced so the operator
    sees what broke without chasing logs.
    """
    module_name = "test_satellite_run_raises"
    fake = ModuleType(module_name)

    def _explode() -> Any:
        msg = "satellite blew up"
        raise RuntimeError(msg)

    fake.run = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake)
    passed, detail = _call_satellite(module_name)
    assert passed is False
    assert detail["stage"] == "execute"
    assert "RuntimeError" in detail["error"]
    assert "satellite blew up" in detail["error"]
    assert "traceback" in detail


def test_call_satellite_success_returns_passed_with_scenario_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A satellite returning a passing ``ScenarioReport`` surfaces ``passed=True``."""
    module_name = "test_satellite_passes"
    fake = ModuleType(module_name)

    def _ok() -> ScenarioReport:
        return ScenarioReport(
            name="ok",
            status="pass",
            metrics={},
            findings=[],
            decision="ok",
        )

    fake.run = _ok  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake)
    passed, detail = _call_satellite(module_name)
    assert passed is True
    assert detail["module"] == module_name
    assert detail["stage"] == "completed"
    assert detail["status"] == "pass"


def test_call_satellite_treats_dict_status_as_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dict-shaped satellite return is read for its ``status`` key.

    Two satellite shapes co-exist (``ScenarioReport`` vs plain dict);
    ``_call_satellite`` must respect both. A dict with status="fail"
    surfaces as ``passed=False`` so the suite catches the regression
    rather than silently coercing to pass.
    """
    module_name = "test_satellite_dict_fail"
    fake = ModuleType(module_name)

    def _dict_fail() -> dict[str, Any]:
        return {"status": "fail", "extra": 1}

    fake.run = _dict_fail  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake)
    passed, detail = _call_satellite(module_name)
    assert passed is False
    assert detail["status"] == "fail"
    assert detail["stage"] == "completed"


def test_call_satellite_treats_scenario_report_regress_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A satellite returning ``status="regress"`` is surfaced as failed.

    Pins the contract that anything in ``{"fail", "regress", "error"}``
    flips ``passed`` to False. This is the only path that lets the
    regression suite catch a misbehaving satellite without re-running
    that satellite's own assertions.
    """
    module_name = "test_satellite_regress"
    fake = ModuleType(module_name)

    def _regress() -> ScenarioReport:
        return ScenarioReport(
            name="regress",
            status="regress",
            metrics={},
            findings=[Finding(severity="fail", message="oops", detail={})],
            decision="regress",
        )

    fake.run = _regress  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake)
    passed, detail = _call_satellite(module_name)
    assert passed is False
    assert detail["status"] == "regress"
