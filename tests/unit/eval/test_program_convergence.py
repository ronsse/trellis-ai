"""Unit smoke for the program_convergence master scenario.

Exercises end-to-end round execution, axis-substrate verification,
and the strict-mode error path against an in-memory SQLite registry.
No live backends.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from eval.scenarios._convergence_common import (
    NINE_AXIS_LABELS,
    _AxisRecord,
    _AxisTrack,
    _build_multi_axis_stats,
    _multi_axis_metrics,
    _NineAxisRound,
)
from eval.scenarios.program_convergence.scenario import (
    DEFAULT_ANALYZER_CADENCE,
    SCENARIO_NAME,
    ProgramConvergenceError,
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


def test_run_against_sqlite_emits_all_nine_axes(
    sqlite_registry: StoreRegistry,
) -> None:
    """End-to-end smoke: every axis lands in the metrics dict."""
    report = run(
        sqlite_registry,
        seed=0,
        rounds=8,
        feedback_batch_size=4,
        analyzer_cadence=4,
        traces_per_domain=3,
    )

    assert report.name == SCENARIO_NAME
    assert report.status == "pass"

    # Each axis must surface three metrics: first_quarter_mean,
    # last_quarter_mean, delta. That's 9 * 3 = 27 keys minimum.
    axis_keys = [k for k in report.metrics if k.startswith("axis.")]
    assert len(axis_keys) >= len(NINE_AXIS_LABELS) * 3, (
        f"expected at least {len(NINE_AXIS_LABELS) * 3} axis metric "
        f"keys, got {len(axis_keys)}: {axis_keys}"
    )
    for label in NINE_AXIS_LABELS:
        for suffix in ("first_quarter_mean", "last_quarter_mean", "delta"):
            key = f"axis.{label}.{suffix}"
            assert key in report.metrics, f"missing metric {key!r}"

    # Composite finding must carry every axis delta in detail.
    composite = next(
        f for f in report.findings
        if "multi-axis summary" in f.message
    )
    assert set(composite.detail["axis_deltas"]) == set(NINE_AXIS_LABELS)


def test_run_is_deterministic(sqlite_registry: StoreRegistry, tmp_path: Path) -> None:
    """Same seed must produce identical axis metrics — POC determinism."""
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
    rep1 = run(sqlite_registry, seed=7, rounds=6, traces_per_domain=3)

    # Fresh registry for the second run so prior state doesn't bleed.
    with StoreRegistry(config=config, stores_dir=tmp_path / "second") as reg2:
        rep2 = run(reg2, seed=7, rounds=6, traces_per_domain=3)

    axis_metrics_1 = {
        k: v for k, v in rep1.metrics.items() if k.startswith("axis.")
    }
    axis_metrics_2 = {
        k: v for k, v in rep2.metrics.items() if k.startswith("axis.")
    }
    assert axis_metrics_1 == axis_metrics_2


def test_run_raises_when_event_log_missing() -> None:
    """Strict mode — bare registry without an EventLog fails loud, not silently."""
    fake_registry = MagicMock()
    fake_registry.operational.event_log = None
    fake_registry.knowledge.graph_store = MagicMock()

    with pytest.raises(ProgramConvergenceError, match="EventLog"):
        run(fake_registry, rounds=1)


def test_run_raises_when_graph_store_missing() -> None:
    """Strict mode — registry without a GraphStore must fail loud, not silently."""
    fake_registry = MagicMock()
    fake_registry.operational.event_log = MagicMock()
    fake_registry.knowledge.graph_store = None

    with pytest.raises(ProgramConvergenceError, match="GraphStore"):
        run(fake_registry, rounds=1)


def test_axis_track_first_last_delta() -> None:
    """``_AxisTrack`` math matches ``_quarter_means`` semantics.

    With 8 records the window size is ``len // 4 == 2``, so the
    first-quarter mean is ``mean(0, 1) == 0.5`` and the last-quarter
    mean is ``mean(6, 7) == 6.5``. Same arithmetic the dual-loop
    scenarios use — we re-assert it here to lock the composition
    contract.
    """
    track = _AxisTrack(axis="A_test")
    for i in range(8):
        track.record(i, float(i))
    assert track.first_quarter_mean() == 0.5
    assert track.last_quarter_mean() == 6.5
    assert track.delta() == 6.0


def test_multi_axis_metrics_keys_are_stable() -> None:
    """``_multi_axis_metrics`` emits exactly the documented key shape."""
    rounds = [
        _NineAxisRound(
            round_index=i,
            weighted_score=float(i),
            items_served=10,
            items_referenced=i,
            coverage_fraction=0.5,
            success=i % 2 == 0,
            axis_pack_quality=float(i),
            axis_useful_item_fraction=i / 10,
            axis_advisory_hit_rate=0.5,
            axis_observation_enrichment=float(i),
            axis_provenance_queryability=1.0,
            axis_extraction_failure_clusters=10.0 - i,
            axis_schema_evolution_candidates=float(i),
            axis_meta_trace_density=1.0,
            axis_self_authored_proposals=float(i),
        )
        for i in range(8)
    ]
    stats = _build_multi_axis_stats(rounds)
    metrics = _multi_axis_metrics(stats)
    for label in NINE_AXIS_LABELS:
        for suffix in ("first_quarter_mean", "last_quarter_mean", "delta"):
            assert f"axis.{label}.{suffix}" in metrics


def test_default_analyzer_cadence_is_positive() -> None:
    """Sanity — the cadence default must be >= 1 or the modulo logic breaks."""
    assert DEFAULT_ANALYZER_CADENCE >= 1


def test_axis_record_is_frozen() -> None:
    """``_AxisRecord`` must be frozen so accidental mutation can't drift history."""
    record = _AxisRecord(axis="x", round_index=0, value=1.0)
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        record.value = 2.0  # type: ignore[misc]
