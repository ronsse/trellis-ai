"""Unit smoke for the skill-loop-convergence scenario (issue #249).

Exercises the reference-driver build against an in-memory SQLite
registry: opt-in gating, kwarg validation, the three axis curves'
expected shapes (P climbs, Q lifts, R falls-then-plateaus), the real
events the loop must leave behind, and determinism.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eval.scenarios.skill_loop_convergence.scenario import (
    OPT_IN_ENV_VAR,
    _validate_run_kwargs,
    run,
)

from trellis.stores.base.event_log import EventType
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

#: Small-but-meaningful knobs: enough periods for two evolution
#: checkpoints and a visible plateau, tiny corpus for speed.
_RUN_KWARGS = {
    "seed": 0,
    "periods": 6,
    "nodes_per_period": 2,
    "docs_per_node": 4,
    "periods_per_evolution": 2,
    "initial_variant_pool": 4,
    "traces_per_domain": 2,
    "entities_per_trace": 2,
}


@pytest.fixture
def sqlite_registry(tmp_path: Path):
    with StoreRegistry(config=_SQLITE_CONFIG, stores_dir=tmp_path) as registry:
        yield registry


@pytest.fixture
def opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OPT_IN_ENV_VAR, "1")


class TestGating:
    def test_skips_without_env_var(
        self, sqlite_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(OPT_IN_ENV_VAR, raising=False)
        report = run(sqlite_registry)
        assert report.status == "skip"
        assert OPT_IN_ENV_VAR in report.findings[0].message
        # No work done: the registry's event log stays empty.
        assert sqlite_registry.operational.event_log.count() == 0

    def test_empty_env_var_still_skips(
        self, sqlite_registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(OPT_IN_ENV_VAR, "")
        assert run(sqlite_registry).status == "skip"


class TestKwargValidation:
    def test_rejects_nonpositive_periods(self) -> None:
        with pytest.raises(ValueError, match="periods must be positive"):
            _validate_run_kwargs(
                periods=0, periods_per_evolution=1, nodes_per_period=1
            )

    def test_rejects_evolution_cadence_beyond_periods(self) -> None:
        with pytest.raises(ValueError, match="periods_per_evolution"):
            _validate_run_kwargs(
                periods=3, periods_per_evolution=4, nodes_per_period=1
            )

    def test_rejects_docs_within_panel_budget(self) -> None:
        # docs_per_node must exceed the panel budget or consolidation has
        # no measurable headroom.
        with pytest.raises(ValueError, match="docs_per_node"):
            _validate_run_kwargs(
                periods=2,
                periods_per_evolution=1,
                nodes_per_period=1,
                docs_per_node=2,
            )

    def test_rejects_degenerate_variant_pool(self) -> None:
        with pytest.raises(ValueError, match="initial_variant_pool"):
            _validate_run_kwargs(
                periods=2,
                periods_per_evolution=1,
                nodes_per_period=1,
                initial_variant_pool=1,
            )


class TestAxisShapes:
    @pytest.fixture
    def report(self, sqlite_registry: StoreRegistry, opted_in: None):
        return run(sqlite_registry, **_RUN_KWARGS)

    def test_report_shape_and_status(self, report) -> None:
        assert report.name == "skill_loop_convergence"
        assert report.status == "pass"
        for key in (
            "coverage_final",
            "baseline_score",
            "final_score",
            "final_lift",
            "survival_final",
            "variants_culled",
            "stability_delta",
        ):
            assert key in report.metrics, key

    def test_axis_p_coverage_climbs_to_full(self, report) -> None:
        curve = report.convergence_stats["coverage"]
        coverage = curve["coverage"]
        assert coverage == sorted(coverage), "coverage must be non-decreasing"
        assert coverage[-1] == 1.0
        assert curve["seed_node_count"] == (
            _RUN_KWARGS["periods"] * _RUN_KWARGS["nodes_per_period"]
        )

    def test_axis_q_lift_positive_and_monotonic(self, report) -> None:
        curve = report.convergence_stats["lift"]
        lift = curve["lift"]
        assert lift[-1] > 0.0, "consolidation must lift panel pack quality"
        assert lift == sorted(lift), "lift must be non-decreasing as coverage grows"
        # The panel improving must not disturb unrelated retrieval.
        assert abs(report.metrics["stability_delta"]) < 0.05

    def test_axis_r_survival_falls_then_plateaus(self, report) -> None:
        curve = report.convergence_stats["survival"]
        survival = curve["survival_rate"]
        assert survival[0] == 1.0, "pool starts intact"
        assert survival[-1] < 1.0, "measured scores must cull weak variants"
        assert survival == sorted(survival, reverse=True), "survival never recovers"
        # Plateau: no culls after the pool converges.
        assert curve["per_period_culled"][-1] == 0

    def test_axis_r_is_labeled_reference_only(self, report) -> None:
        # The R axis must not be citable as F5 evidence — the report
        # carries the disclaimer as a finding.
        assert any("reference evolver" in f.message for f in report.findings)


class TestRealSubsystems:
    def test_loop_leaves_real_events_and_versions(
        self, sqlite_registry: StoreRegistry, opted_in: None
    ) -> None:
        report = run(sqlite_registry, **_RUN_KWARGS)
        assert report.status == "pass"

        # Axis Q rode the production evaluator hook: real
        # PACK_QUALITY_SCORED events landed in the real EventLog — at
        # least one per panel scenario per period.
        event_log = sqlite_registry.operational.event_log
        panel_size = _RUN_KWARGS["periods"] * _RUN_KWARGS["nodes_per_period"]
        scored = event_log.count(event_type=EventType.PACK_QUALITY_SCORED)
        assert scored >= panel_size * _RUN_KWARGS["periods"]

        # Enrichment went through the governed pipeline: the node has a
        # new SCD-2 version carrying the description, with the sparse
        # seed version preserved in history.
        graph = sqlite_registry.knowledge.graph_store
        node_id = "skill:0000:node:000"
        node = graph.get_node(node_id)
        assert node is not None
        assert "description" in node["properties"]
        history = graph.get_node_history(node_id)
        assert len(history) == 2
        assert "description" not in history[-1]["properties"]

        # The consolidated summary landed in the real document store.
        doc = sqlite_registry.knowledge.document_store.get(
            f"doc:enriched:{node_id}"
        )
        assert doc is not None


class TestDeterminism:
    def test_same_seed_same_curves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(OPT_IN_ENV_VAR, "1")
        reports = []
        for i in range(2):
            with StoreRegistry(
                config=_SQLITE_CONFIG, stores_dir=tmp_path / f"run{i}"
            ) as registry:
                reports.append(run(registry, **_RUN_KWARGS))
        a, b = reports
        assert a.convergence_stats["coverage"] == b.convergence_stats["coverage"]
        assert a.convergence_stats["lift"] == b.convergence_stats["lift"]
        assert a.convergence_stats["survival"] == b.convergence_stats["survival"]
        assert a.metrics == b.metrics
