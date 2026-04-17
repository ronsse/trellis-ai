"""Tests for ingest CLI commands."""

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


def _trace_json() -> str:
    return json.dumps(
        {
            "source": "agent",
            "intent": "deploy service",
            "steps": [],
            "context": {"agent_id": "agent-1", "domain": "platform"},
        }
    )


def _evidence_json() -> str:
    return json.dumps(
        {
            "evidence_type": "snippet",
            "content": "SELECT * FROM users",
            "source_origin": "trace",
        }
    )


class TestIngestTrace:
    def test_ingest_trace_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "trace.json"
        f.write_text(_trace_json())
        result = runner.invoke(app, ["ingest", "trace", str(f)])
        assert result.exit_code == 0
        assert "ingested" in result.stdout.lower()

    def test_ingest_trace_json_format(self, tmp_path: Path) -> None:
        f = tmp_path / "trace.json"
        f.write_text(_trace_json())
        result = runner.invoke(app, ["ingest", "trace", str(f), "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert "trace_id" in data

    def test_ingest_trace_from_stdin(self) -> None:
        result = runner.invoke(app, ["ingest", "trace", "-"], input=_trace_json())
        assert result.exit_code == 0

    def test_ingest_trace_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("not json")
        result = runner.invoke(app, ["ingest", "trace", str(f)])
        assert result.exit_code == 1

    def test_ingest_trace_invalid_schema(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"bogus": "data"}))
        result = runner.invoke(app, ["ingest", "trace", str(f)])
        assert result.exit_code == 1

    def test_ingest_trace_file_not_found(self) -> None:
        result = runner.invoke(app, ["ingest", "trace", "/nonexistent/file.json"])
        assert result.exit_code == 1


class TestIngestEvidence:
    def test_ingest_evidence_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "evidence.json"
        f.write_text(_evidence_json())
        result = runner.invoke(app, ["ingest", "evidence", str(f)])
        assert result.exit_code == 0
        assert "ingested" in result.stdout.lower()

    def test_ingest_evidence_json_format(self, tmp_path: Path) -> None:
        f = tmp_path / "evidence.json"
        f.write_text(_evidence_json())
        result = runner.invoke(app, ["ingest", "evidence", str(f), "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"

    def test_ingest_evidence_invalid(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text(json.dumps({"bad": "data"}))
        result = runner.invoke(app, ["ingest", "evidence", str(f)])
        assert result.exit_code == 1

    def test_ingest_evidence_file_not_found(self) -> None:
        result = runner.invoke(app, ["ingest", "evidence", "/nonexistent.json"])
        assert result.exit_code == 1


class TestIngestHelp:
    def test_ingest_help(self) -> None:
        result = runner.invoke(app, ["ingest", "--help"])
        assert result.exit_code == 0
        assert "trace" in result.stdout
        assert "evidence" in result.stdout


_SAMPLE_DBT_MANIFEST: dict = {
    "nodes": {
        "model.p.stg_orders": {
            "unique_id": "model.p.stg_orders",
            "resource_type": "model",
            "name": "stg_orders",
            "schema": "staging",
            "description": "Staged orders",
            "depends_on": {"nodes": ["source.p.raw.orders"]},
            "config": {"materialized": "view"},
        },
    },
    "sources": {
        "source.p.raw.orders": {
            "unique_id": "source.p.raw.orders",
            "resource_type": "source",
            "name": "orders",
            "source_name": "raw",
            "schema": "public",
            "description": "Raw orders table",
        },
    },
}

_SAMPLE_OL_EVENTS: list[dict] = [
    {
        "eventType": "COMPLETE",
        "job": {"namespace": "spark", "name": "etl_job"},
        "inputs": [{"namespace": "warehouse", "name": "raw.events"}],
        "outputs": [{"namespace": "warehouse", "name": "analytics.daily_events"}],
    },
]


class TestIngestDbtManifest:
    def test_ingest_manifest_file(self, tmp_path: Path) -> None:
        # admin init is needed for the stores dir layout the CLI expects.
        runner.invoke(app, ["admin", "init"])
        f = tmp_path / "manifest.json"
        f.write_text(json.dumps(_SAMPLE_DBT_MANIFEST))
        result = runner.invoke(
            app, ["ingest", "dbt-manifest", str(f), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert data["nodes"] == 2
        assert data["edges"] == 1
        assert data["documents"] == 2  # both have descriptions

    def test_ingest_from_project_dir(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        target = tmp_path / "target"
        target.mkdir()
        (target / "manifest.json").write_text(json.dumps(_SAMPLE_DBT_MANIFEST))
        result = runner.invoke(
            app, ["ingest", "dbt-manifest", str(tmp_path), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["nodes"] == 2

    def test_missing_path(self) -> None:
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(
            app, ["ingest", "dbt-manifest", "/nonexistent/manifest.json"]
        )
        assert result.exit_code == 1


class TestIngestOpenLineage:
    def test_ingest_events_json_array(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        f = tmp_path / "events.json"
        f.write_text(json.dumps(_SAMPLE_OL_EVENTS))
        result = runner.invoke(
            app, ["ingest", "openlineage", str(f), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ingested"
        assert data["nodes"] == 3  # 1 job + 2 datasets
        assert data["edges"] == 2  # reads + writes

    def test_ingest_events_ndjson(self, tmp_path: Path) -> None:
        runner.invoke(app, ["admin", "init"])
        f = tmp_path / "events.ndjson"
        f.write_text("\n".join(json.dumps(e) for e in _SAMPLE_OL_EVENTS))
        result = runner.invoke(
            app, ["ingest", "openlineage", str(f), "--format", "json"]
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(result.stdout.strip())
        assert data["nodes"] == 3

    def test_missing_path(self) -> None:
        runner.invoke(app, ["admin", "init"])
        result = runner.invoke(
            app, ["ingest", "openlineage", "/nonexistent/events.json"]
        )
        assert result.exit_code == 1
