"""Tests for ``trellis extract traces`` backfill CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def _ingest(trace: dict) -> str:
    """Ingest a trace via the CLI (flag off, so no graph yet) and return its id."""
    payload = runner.invoke(
        app, ["ingest", "trace", "-", "--format", "json"], input=json.dumps(trace)
    )
    assert payload.exit_code == 0, payload.stdout
    return json.loads(payload.stdout.strip())["trace_id"]


_TRACE_A: dict = {
    "source": "agent",
    "intent": "fix the import",
    "steps": [{"step_type": "tool_call", "name": "grep"}],
    "context": {"agent_id": "a1", "domain": "backend"},
}

_TRACE_B: dict = {
    "source": "agent",
    "intent": "deploy the service",
    "steps": [{"step_type": "tool_call", "name": "deploy"}],
    "context": {"agent_id": "a2", "domain": "platform"},
}


class TestBackfill:
    def test_dry_run_reports_drafts_without_executing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Ingest with the flag OFF so the graph starts empty.
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        _ingest(_TRACE_A)
        _ingest(_TRACE_B)

        result = runner.invoke(
            app, ["extract", "traces", "--dry-run", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "backfilled"
        assert data["dry_run"] is True
        assert data["traces_scanned"] == 2
        assert data["total_entities"] > 0
        assert len(data["per_trace"]) == 2

        # Dry-run executed nothing — graph still empty.
        from trellis_cli.stores import get_graph_store

        assert get_graph_store().count_nodes() == 0

    def test_executes_and_populates_graph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        trace_id = _ingest(_TRACE_A)

        result = runner.invoke(app, ["extract", "traces", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "backfilled"
        assert data["dry_run"] is False
        assert data["total_entities"] > 0

        from trellis_cli.stores import get_graph_store

        graph = get_graph_store()
        assert graph.count_nodes() > 0
        assert graph.get_node(f"trace:{trace_id}") is not None

    def test_domain_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        _ingest(_TRACE_A)  # backend
        _ingest(_TRACE_B)  # platform

        result = runner.invoke(
            app,
            ["extract", "traces", "--domain", "platform", "--format", "json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["traces_scanned"] == 1
        assert data["per_trace"][0]["domain"] == "platform"

    def test_text_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        _ingest(_TRACE_A)
        result = runner.invoke(app, ["extract", "traces"])
        assert result.exit_code == 0
        assert "backfill" in result.stdout.lower()

    def test_empty_store_scans_zero(self) -> None:
        result = runner.invoke(app, ["extract", "traces", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["traces_scanned"] == 0
        assert data["total_entities"] == 0

    def test_since_filter_excludes_old(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_TRACE_EXTRACTION", raising=False)
        _ingest(_TRACE_A)
        # since=0 days -> window start is "now"; the just-ingested trace
        # falls outside (created_at < now), so nothing is scanned.
        result = runner.invoke(
            app, ["extract", "traces", "--since", "0", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["traces_scanned"] == 0
