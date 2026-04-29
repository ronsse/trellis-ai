"""Unit tests for the multi-backend feedback-loop scenario.

CI-runnable subset: exercises the SQLite-only path (no live Postgres /
Neo4j). The cross-backend semantics are validated by running the
scenario against `.env`-configured backends and inspecting the report;
that's not what these tests are for.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from eval.scenarios.multi_backend_feedback.scenario import (
    _EXACT_MATCH_LOOP_KEYS,
    _TOLERANCE_MATCH_KEYS,
    CONVERGENCE_DELTA_TOLERANCE,
    _BackendRun,
    _diff_runs,
    run,
)


@pytest.fixture
def sqlite_only(monkeypatch) -> None:
    """Ensure the env has no live-backend creds for these tests."""
    for var in (
        "TRELLIS_KNOWLEDGE_PG_DSN",
        "TRELLIS_PG_DSN",
        "TRELLIS_NEO4J_URI",
        "TRELLIS_NEO4J_USER",
        "TRELLIS_NEO4J_PASSWORD",
        "TRELLIS_NEO4J_DATABASE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_run_sqlite_only_skips_postgres_and_neo4j(sqlite_only) -> None:
    """With no env vars set, only SQLite runs — and the scenario passes."""
    registry = MagicMock()
    report = run(
        registry,
        seed=0,
        rounds=8,
        feedback_batch_size=4,
        traces_per_domain=3,
        entities_per_trace=2,
    )

    assert report.name == "multi_backend_feedback"
    assert report.status == "pass"
    assert report.metrics["backends_compared"] == 1.0
    assert report.metrics["rounds"] == 8.0

    messages = [f.message for f in report.findings]
    assert any("compared backends: sqlite" in m for m in messages)
    assert any("postgres backend skipped" in m for m in messages)
    assert any("neo4j_op_postgres backend skipped" in m for m in messages)


def test_run_emits_per_backend_metrics(sqlite_only) -> None:
    """Per-backend keys land in the metrics dict so a report is auditable."""
    report = run(
        MagicMock(),
        seed=0,
        rounds=4,
        feedback_batch_size=4,
        traces_per_domain=2,
    )
    # SQLite-only run still produces per_backend.* and duration_seconds.*
    # for the one handle exercised.
    per_backend_keys = [k for k in report.metrics if k.startswith("per_backend.sqlite")]
    assert per_backend_keys, "expected per_backend.sqlite.* metrics"
    assert "duration_seconds.sqlite" in report.metrics


def test_run_decision_text_signals_single_backend(sqlite_only) -> None:
    """SQLite-only run ⇒ decision points the operator at the env vars."""
    report = run(MagicMock(), seed=0, rounds=4, feedback_batch_size=4)
    assert "TRELLIS_KNOWLEDGE_PG_DSN" in report.decision


def test_diff_runs_reports_no_findings_on_identical_metrics() -> None:
    """Identical loop output across two backends ⇒ no findings, no metrics."""
    metrics = dict.fromkeys(_EXACT_MATCH_LOOP_KEYS, 1.0)
    metrics.update(dict.fromkeys(_TOLERANCE_MATCH_KEYS, 0.5))
    a = _BackendRun(name="sqlite", metrics=metrics, duration_seconds=0.1)
    b = _BackendRun(name="postgres", metrics=dict(metrics), duration_seconds=0.2)

    findings, diff_metrics = _diff_runs(a, b, tolerance=CONVERGENCE_DELTA_TOLERANCE)

    assert not findings
    # Tolerance keys produce a diff entry per pair (zero, but recorded
    # for the report so a reader sees the comparison was performed).
    for key in _TOLERANCE_MATCH_KEYS:
        assert f"diff.{key}.sqlite_vs_postgres" in diff_metrics
        assert diff_metrics[f"diff.{key}.sqlite_vs_postgres"] == 0.0


def test_diff_runs_flags_loop_counter_mismatch_as_fail() -> None:
    """A mismatch on any loop counter is a hard fail."""
    base = dict.fromkeys(_EXACT_MATCH_LOOP_KEYS, 0.0)
    base.update(dict.fromkeys(_TOLERANCE_MATCH_KEYS, 0.0))

    drifted = dict(base)
    drifted["loops.advisories_suppressed_total"] = 3.0  # PG sees 3, sqlite saw 0

    a = _BackendRun(name="sqlite", metrics=base, duration_seconds=0.1)
    b = _BackendRun(name="postgres", metrics=drifted, duration_seconds=0.1)

    findings, _ = _diff_runs(a, b, tolerance=CONVERGENCE_DELTA_TOLERANCE)

    fail_findings = [f for f in findings if f.severity == "fail"]
    assert len(fail_findings) == 1
    assert "loops.advisories_suppressed_total" in fail_findings[0].message
    assert "sqlite=0.0" in fail_findings[0].message
    assert "postgres=3.0" in fail_findings[0].message


def test_diff_runs_flags_convergence_drift_as_warn() -> None:
    """Drift in convergence delta beyond tolerance ⇒ warn, not fail."""
    base = dict.fromkeys(_EXACT_MATCH_LOOP_KEYS, 0.0)
    base.update(dict.fromkeys(_TOLERANCE_MATCH_KEYS, 0.0))

    drifted = dict(base)
    drifted["convergence.useful_delta"] = 0.5  # well above 0.01 tolerance

    a = _BackendRun(name="sqlite", metrics=base, duration_seconds=0.1)
    b = _BackendRun(name="postgres", metrics=drifted, duration_seconds=0.1)

    findings, diff_metrics = _diff_runs(a, b, tolerance=CONVERGENCE_DELTA_TOLERANCE)

    warn_findings = [f for f in findings if f.severity == "warn"]
    assert len(warn_findings) == 1
    assert "convergence.useful_delta" in warn_findings[0].message
    assert diff_metrics["diff.convergence.useful_delta.sqlite_vs_postgres"] == 0.5


def test_diff_runs_tolerates_small_float_noise() -> None:
    """Tiny float noise (< tolerance) is info-only, no findings."""
    base = dict.fromkeys(_EXACT_MATCH_LOOP_KEYS, 0.0)
    base.update(dict.fromkeys(_TOLERANCE_MATCH_KEYS, 0.5))

    noisy = dict(base)
    noisy["convergence.useful_delta"] = 0.5005  # 0.0005 < 0.01 tolerance

    a = _BackendRun(name="sqlite", metrics=base, duration_seconds=0.1)
    b = _BackendRun(name="postgres", metrics=noisy, duration_seconds=0.1)

    findings, diff_metrics = _diff_runs(a, b, tolerance=CONVERGENCE_DELTA_TOLERANCE)

    assert not findings
    assert (
        diff_metrics["diff.convergence.useful_delta.sqlite_vs_postgres"]
        < CONVERGENCE_DELTA_TOLERANCE
    )


def test_tolerance_constant_is_sane() -> None:
    """Pin the tolerance so a future tightening surfaces in review."""
    assert 0.0 < CONVERGENCE_DELTA_TOLERANCE <= 0.1


def test_invalid_rounds_propagate(sqlite_only) -> None:
    """run_convergence's invariants are surfaced as a fail report by the runner.

    Direct calls re-raise; the runner wraps. We test the direct path.
    """
    with pytest.raises(ValueError, match="rounds must be positive"):
        run(MagicMock(), rounds=0)
