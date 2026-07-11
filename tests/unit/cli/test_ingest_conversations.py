"""Tests for ``trellis ingest conversations``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


@pytest.fixture
def export(tmp_path: Path) -> Path:
    conversations = [
        {
            "uuid": "c1",
            "name": "Kids and savings",
            "chat_messages": [
                {"sender": "human", "text": "My kids are 7 and 4."},
                {
                    "sender": "assistant",
                    "text": "Noted — planning for two young children.",
                },
            ],
        }
    ]
    src = tmp_path / "conversations.json"
    src.write_text(json.dumps(conversations))
    return src


class TestIngestConversations:
    def test_sync_json_output(self, export: Path) -> None:
        result = runner.invoke(
            app, ["ingest", "conversations", str(export), "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "synced"
        assert data["counts"]["ingested"] == 1
        assert data["files"][0]["doc_id"] == "conversation:claude-ai:c1"

    def test_second_run_skips_unchanged(self, export: Path) -> None:
        args = ["ingest", "conversations", str(export), "--format", "json"]
        assert runner.invoke(app, args).exit_code == 0
        data = json.loads(runner.invoke(app, args).stdout.strip())
        assert data["counts"]["skipped_unchanged"] == 1
        assert data["counts"]["ingested"] == 0

    def test_dry_run_reports_plan(self, export: Path) -> None:
        result = runner.invoke(
            app,
            ["ingest", "conversations", str(export), "--dry-run", "--format", "json"],
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout.strip())["status"] == "planned"

    def test_tags_land_in_metadata(self, export: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ingest",
                "conversations",
                str(export),
                "--domain",
                "personal",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        from trellis_cli.stores import _get_registry, _reset_registry

        _reset_registry()
        doc = _get_registry().knowledge.document_store.get("conversation:claude-ai:c1")
        assert doc["metadata"]["domain"] == "personal"

    def test_missing_path_errors(self) -> None:
        result = runner.invoke(
            app, ["ingest", "conversations", "/nope/missing.json", "--format", "json"]
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout.strip())["status"] == "error"

    def test_text_output_mentions_counts(self, export: Path) -> None:
        result = runner.invoke(app, ["ingest", "conversations", str(export)])
        assert result.exit_code == 0
        assert "new=1" in result.stdout
