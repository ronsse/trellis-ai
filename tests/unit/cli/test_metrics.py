"""Smoke tests for the ``trellis metrics`` CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.ops import record_outcome
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
    domain: str = "sportsbook",
) -> None:
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


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


def test_metrics_outcomes_json(cli_env):
    _seed_failing_outcomes(cli_env["outcome_store"])

    result = runner.invoke(app, ["metrics", "outcomes", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["outcomes_scanned"] == 40
    assert len(payload["cells"]) == 1
    cell = payload["cells"][0]
    assert cell["scope"]["component_id"] == "retrieve.strategies.KeywordSearch"
    assert cell["count"] == 40
    assert cell["success_rate"] == pytest.approx(0.1, abs=0.05)


def test_metrics_outcomes_empty(cli_env):
    result = runner.invoke(app, ["metrics", "outcomes", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["outcomes_scanned"] == 0
    assert payload["cells"] == []


# ---------------------------------------------------------------------------
# tune
# ---------------------------------------------------------------------------


def test_metrics_tune_emits_proposals(cli_env):
    _seed_failing_outcomes(cli_env["outcome_store"])

    result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tuner_name"] == "rule_tuner"
    assert payload["proposals_persisted"] >= 1
    first = payload["proposals"][0]
    assert first["scope"]["component_id"] == "retrieve.strategies.KeywordSearch"


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


def test_metrics_proposals_lists_stored(cli_env):
    _seed_failing_outcomes(cli_env["outcome_store"])
    runner.invoke(app, ["metrics", "tune", "--format", "json"])

    result = runner.invoke(app, ["metrics", "proposals", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) >= 1


def test_metrics_proposals_status_filter(cli_env):
    _seed_failing_outcomes(cli_env["outcome_store"])
    runner.invoke(app, ["metrics", "tune", "--format", "json"])

    result = runner.invoke(
        app, ["metrics", "proposals", "--status", "pending", "--format", "json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
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
    payload = json.loads(result.output)
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
    payload = json.loads(result.output)
    assert payload["active_version"] is not None
    assert len(payload["versions"]) == 1


# ---------------------------------------------------------------------------
# promote dry-run + commit
# ---------------------------------------------------------------------------


def test_metrics_promote_dry_run_by_default(cli_env):
    _seed_failing_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.output)["proposals"][0]["proposal_id"]

    result = runner.invoke(app, ["metrics", "promote", proposal_id, "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dry_run"] is True

    # Proposal should still be pending (no mutation).
    assert cli_env["tuner_state"].get_proposal(proposal_id).status == "pending"


def test_metrics_promote_commit_changes_state(cli_env):
    _seed_failing_outcomes(cli_env["outcome_store"])
    tune_result = runner.invoke(app, ["metrics", "tune", "--format", "json"])
    proposal_id = json.loads(tune_result.output)["proposals"][0]["proposal_id"]

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
    payload = json.loads(result.output)
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
    payload = json.loads(result.output)
    assert payload["status"] == "skipped"
    assert payload["reason"] == "proposal_not_found"
