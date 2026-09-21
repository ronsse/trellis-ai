"""Smoke tests for the ``trellis metrics`` CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.ops import record_outcome
from trellis.schemas.outcome import GRAPH_SEARCH_COMPONENT_ID
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.outcome import SQLiteOutcomeStore
from trellis.stores.sqlite.parameter import SQLiteParameterStore
from trellis.stores.sqlite.tuner_state import SQLiteTunerStateStore
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch) -> Iterator[dict[str, Path]]:
    """Point the CLI at a temp data dir and pre-seed the ops stores."""
    stores_dir = tmp_path / "data" / "stores"
    stores_dir.mkdir(parents=True)

    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))

    # Reset any CLI-level cached registry.
    from trellis_cli import stores as cli_stores

    cli_stores._reset_registry()

    # Pre-populate stores by instantiating directly at the same paths the
    # CLI registry will use.
    outcome_store = SQLiteOutcomeStore(stores_dir / "outcomes.db")
    param_store = SQLiteParameterStore(stores_dir / "parameters.db")
    tuner_state = SQLiteTunerStateStore(stores_dir / "tuner_state.db")
    event_log = SQLiteEventLog(stores_dir / "events.db")

    try:
        yield {
            "stores_dir": stores_dir,
            "outcome_store": outcome_store,
            "param_store": param_store,
            "tuner_state": tuner_state,
            "event_log": event_log,
        }
    finally:
        outcome_store.close()
        param_store.close()
        tuner_state.close()
        event_log.close()
        cli_stores._reset_registry()


def _seed_failing_outcomes(
    outcome_store: SQLiteOutcomeStore,
    *,
    n: int = 40,
    component_id: str = "retrieve.strategies.KeywordSearch",
    domain: str = "orders",
) -> None:
    """Seed a cell for the ``metrics outcomes`` aggregation tests.

    Deliberately *not* shaped to trip any ``DEFAULT_RULES`` entry — these
    tests pin how outcomes roll up into cells, and a fixture that also
    happened to fire a rule would couple them to the rule roster.
    """
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(n):
        record_outcome(
            outcome_store,
            component_id=component_id,
            success=i % 10 == 0,  # ~10% success
            latency_ms=12.0,
            domain=domain,
            intent_family="plan",
            occurred_at=base + timedelta(seconds=i),
        )


def _seed_uncited_graph_outcomes(
    outcome_store: SQLiteOutcomeStore,
    *,
    n: int = 10,
    served_each: int = 5,
    referenced_total: int = 1,
) -> None:
    """Seed a cell the shipped rule actually fires on.

    Mirrors the production cell ``DEFAULT_RULES`` was calibrated against
    — many graph items served, almost none cited — so these tests pin the
    CLI wiring (exit code, JSON shape, persistence, promotion state) with
    a fixture the *current* roster can reach.  The previous fixture was
    shaped for ``keyword_low_success_rate_boost_recency``, retired in
    #562; when that rule went, four of these tests failed and a fifth
    (``proposals_status_filter``, an ``all()`` over an empty list) passed
    vacuously.

    ``50`` servings clears ``min_items_served=20``, ``10`` rows clears
    ``min_sample_size=3``, and a reference rate of ``1/50 = 0.02`` sits
    under the ``0.07`` threshold.
    """
    base = datetime.now(UTC) - timedelta(hours=1)
    for i in range(n):
        record_outcome(
            outcome_store,
            component_id=GRAPH_SEARCH_COMPONENT_ID,
            # The pack's bit, fanned out — the rule keys on the reference
            # rate, not on this.
            success=i % 5 == 0,
            latency_ms=12.0,
            domain="orders",
            intent_family="plan",
            items_served=served_each,
            items_referenced=1 if i < referenced_total else 0,
            occurred_at=base + timedelta(seconds=i),
        )


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


def test_metrics_outcomes_json(cli_env):
    _seed_failing_outcomes(cli_env["outcome_store"])

    result = runner.invoke(app, ["metrics", "outcomes", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["outcomes_scanned"] == 40
    assert len(payload["cells"]) == 1
    cell = payload["cells"][0]
    assert cell["scope"]["component_id"] == "retrieve.strategies.KeywordSearch"
    assert cell["count"] == 40
    assert cell["success_rate"] == pytest.approx(0.1, abs=0.05)


def test_metrics_outcomes_empty(cli_env):
    result = runner.invoke(app, ["metrics", "outcomes", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["outcomes_scanned"] == 0
    assert payload["cells"] == []


# ---------------------------------------------------------------------------
# tune
# ---------------------------------------------------------------------------


def test_metrics_tune_emits_proposals(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])

    result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["tuner_name"] == "rule_tuner"
    assert payload["proposals_persisted"] >= 1
    first = payload["proposals"][0]
    assert first["scope"]["component_id"] == GRAPH_SEARCH_COMPONENT_ID


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


def test_metrics_proposals_lists_stored(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    runner.invoke(app, ["metrics", "tune", "--format", "json"])

    result = runner.invoke(app, ["metrics", "proposals", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) >= 1


def test_metrics_proposals_status_filter(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    runner.invoke(app, ["metrics", "tune", "--format", "json"])

    result = runner.invoke(
        app, ["metrics", "proposals", "--status", "pending", "--format", "json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # An ``all()`` over an empty list is True, so the filter has to be
    # shown something to filter before it can be shown to filter right.
    assert len(payload) >= 1
    assert all(p["status"] == "pending" for p in payload)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def test_metrics_versions_for_scope_without_history(cli_env):
    result = runner.invoke(
        app,
        [
            "metrics",
            "versions",
            "retrieve.strategies.KeywordSearch",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["active_version"] is None
    assert payload["versions"] == []


def test_metrics_versions_after_seed(cli_env):
    cli_env["param_store"].put(
        ParameterSet(
            scope=ParameterScope(
                component_id="retrieve.strategies.KeywordSearch", domain="a"
            ),
            values={"recency_half_life_days": 20.0},
        )
    )
    result = runner.invoke(
        app,
        [
            "metrics",
            "versions",
            "retrieve.strategies.KeywordSearch",
            "--domain",
            "a",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["active_version"] is not None
    assert len(payload["versions"]) == 1


# ---------------------------------------------------------------------------
# promote dry-run + commit
# ---------------------------------------------------------------------------


def test_metrics_promote_dry_run_by_default(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    result = runner.invoke(app, ["metrics", "promote", proposal_id, "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True

    # Proposal should still be pending (no mutation).
    assert cli_env["tuner_state"].get_proposal(proposal_id).status == "pending"


def test_metrics_promote_commit_changes_state(cli_env):
    _seed_uncited_graph_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.stdout)["proposals"][0]["proposal_id"]

    result = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            proposal_id,
            "--commit",
            "--min-sample-size",
            "5",
            "--min-effect-size",
            "0.01",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "promoted"
    assert payload["params_version"] is not None

    assert cli_env["tuner_state"].get_proposal(proposal_id).status == "promoted"


def test_metrics_promote_missing_proposal(cli_env):
    result = runner.invoke(
        app,
        [
            "metrics",
            "promote",
            "prop_nonexistent",
            "--commit",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "skipped"
    assert payload["reason"] == "proposal_not_found"
