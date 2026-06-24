"""Live dashboard ↔ eval parity (assessment §6 item 9).

The Step-3 assessment (``docs/plans/2026-06-17-step3-assessment.md`` §2)
establishes that the metrics dashboard and the eval convergence metrics
are the *same numbers* only under a stated condition: a single-UTC-day,
fully-joined run. Elsewhere the parity is asserted "by construction"
(both read the same events / share ``join_pack_feedback``).

This test closes the gap by *rendering* the dashboard against the exact
EventLog a convergence scenario emitted and comparing the dashboard's
values to the eval's in-process metrics — a live render-compare rather
than a construction argument. It runs the scenario in regime-shift mode
so the compared values are non-trivial (``round_success_rate`` 0.5,
``round_useful_fraction_overall`` ~0.43) rather than a vacuous 1.0.

The single-UTC-day assumption is the §2 condition itself, not a test
flaw: a sub-second scenario run lands in one calendar day, so the
dashboard produces one bucket whose value must equal the corpus-wide eval
scalar. (A run straddling UTC midnight would split into per-day points
that individually differ — exactly the documented divergence.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eval.scenarios.agent_loop_convergence.scenario import run

from trellis.retrieve.metrics_timeseries import (
    GROUP_BY_NONE,
    METRIC_PACK_SUCCESS_RATE,
    METRIC_REFERENCE_RATE,
    TimeseriesResult,
    compute_timeseries,
)
from trellis.stores.registry import StoreRegistry

_SQLITE_CONFIG = {
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


def _repool_ratio(result: TimeseriesResult) -> tuple[float, int]:
    """Recombine a ratio series' daily buckets into one pooled scalar.

    ``value`` is a per-bucket ratio and ``sample_count`` its denominator
    count, so ``Σ(value·count) / Σ(count)`` reconstructs the corpus-wide
    ratio independent of how many UTC-day buckets the run spanned. (Only
    valid for ``pack_success_rate``, whose ``sample_count`` *is* the
    ratio's denominator — one joined feedback per pack.)
    """
    points = [p for series in result.series for p in series.points]
    numerator = sum(p.value * p.sample_count for p in points)
    denominator = sum(p.sample_count for p in points)
    return (numerator / denominator if denominator else 0.0), denominator


def test_dashboard_renders_same_numbers_as_eval(tmp_path: Path) -> None:
    """Dashboard metrics == eval metrics for a single-UTC-day, joined run."""
    with StoreRegistry(config=_SQLITE_CONFIG, stores_dir=tmp_path) as registry:
        report = run(
            registry,
            seed=0,
            rounds=30,
            feedback_batch_size=5,
            # Regime shift makes the compared values non-trivial.
            regime_shift_round=15,
            advisory_min_sample_size=2,
            convergence_delta_regress_threshold=-1.0,
        )
        event_log = registry.operational.event_log
        psr = compute_timeseries(
            event_log, metric=METRIC_PACK_SUCCESS_RATE, group_by=GROUP_BY_NONE
        )
        ref = compute_timeseries(
            event_log, metric=METRIC_REFERENCE_RATE, group_by=GROUP_BY_NONE
        )

    # pack_success_rate: repool across buckets (robust) → eval scalar.
    dashboard_success_rate, feedback_count = _repool_ratio(psr)
    assert feedback_count == 30  # one joined feedback per round, all in window
    assert dashboard_success_rate == pytest.approx(
        report.metrics["round_success_rate"], abs=1e-4
    )
    # Non-trivial guard: regime shift must actually have driven failures,
    # otherwise the equality above is the vacuous 1.0 == 1.0.
    assert report.metrics["round_success_rate"] < 1.0

    # reference_rate: ungrouped → a single "all" series; one UTC-day bucket.
    assert len(ref.series) == 1
    assert ref.series[0].group_key == "all"
    points = ref.series[0].points
    assert len(points) == 1, "sub-second run must land in one UTC day"
    assert points[0].value == pytest.approx(
        report.metrics["round_useful_fraction_overall"], abs=1e-4
    )
