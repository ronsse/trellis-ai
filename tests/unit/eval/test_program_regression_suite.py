"""Unit tests for the program_regression_suite scenario.

Focused on the Phase 5B addition: the axis A threshold split by corpus
profile. The suite's full end-to-end run takes minutes (50 rounds + 4
satellites) and is operator-invoked, not part of ``pytest tests/`` —
these tests target ``_assert_axis_a`` and the ``run()`` profile
validation directly so the CI gate stays fast.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from eval.scenarios.program_convergence.scenario import _RoundResult
from eval.scenarios.program_regression_suite.scenario import (
    DEFAULT_CORPUS_PROFILE,
    THRESHOLD_A_DELTA_BY_PROFILE,
    _assert_axis_a,
    run,
)


def _make_round(
    round_index: int,
    *,
    pack_quality: float,
    success: bool = True,
) -> _RoundResult:
    """Build a ``_RoundResult`` with axis A pinned and other axes inert.

    Only axis A is exercised by ``_assert_axis_a`` — the other fields
    feed unrelated assertions so we set them to neutral values.
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
        axis_useful_item_fraction=0.5,
        axis_advisory_hit_rate=0.5,
        axis_observation_enrichment=0.0,
        axis_provenance_queryability=1.0,
        axis_extraction_failure_clusters=0.0,
        axis_schema_evolution_candidates=0.0,
        axis_meta_trace_density=0.0,
        axis_self_authored_proposals=0.0,
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
