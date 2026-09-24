"""Tests for ``trellis ingest corpus``."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.ingest_corpus.handlers import supported_extensions
from trellis_cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("---\ntitle: T\n---\n\nBody [[Link]].\n")
    (root / "other.txt").write_text("plain\n")
    return root


class TestIngestCorpus:
    def test_sync_json_output(self, vault: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ingest",
                "corpus",
                str(vault),
                "--source-system",
                "obsidian",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "synced"
        assert data["counts"]["ingested"] == 1
        assert data["counts"]["skipped_unsupported"] == 1
        assert data["files"][0]["doc_id"].startswith("corpus:obsidian:")

    def test_second_run_skips_unchanged(self, vault: Path) -> None:
        args = ["ingest", "corpus", str(vault), "--format", "json"]
        assert runner.invoke(app, args).exit_code == 0
        result = runner.invoke(app, args)
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["counts"]["skipped_unchanged"] == 1
        assert data["counts"]["ingested"] == 0

    def test_dry_run_reports_plan(self, vault: Path) -> None:
        result = runner.invoke(
            app, ["ingest", "corpus", str(vault), "--dry-run", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "planned"
        assert data["dry_run"] is True
        follow_up = runner.invoke(
            app, ["ingest", "corpus", str(vault), "--dry-run", "--format", "json"]
        )
        # Dry runs never write: the plan is identical the second time.
        assert json.loads(follow_up.stdout.strip())["counts"]["ingested"] == 1

    def test_tags_and_domain_land_in_metadata(self, vault: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ingest",
                "corpus",
                str(vault),
                "--domain",
                "ops",
                "--tag",
                "team=core",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        from trellis_cli.stores import _get_registry, _reset_registry

        _reset_registry()
        doc_id = json.loads(result.stdout.strip())["files"][0]["doc_id"]
        stored = _get_registry().knowledge.document_store.get(doc_id)
        assert stored["metadata"]["domain"] == "ops"
        assert stored["metadata"]["team"] == "core"

    def test_invalid_tag_exits_with_validation_code(self, vault: Path) -> None:
        result = runner.invoke(
            app, ["ingest", "corpus", str(vault), "--tag", "notkv", "--format", "json"]
        )
        assert result.exit_code == 2
        data = json.loads(result.stdout.strip())
        assert data["status"] == "error"

    def test_missing_path_errors(self) -> None:
        result = runner.invoke(
            app, ["ingest", "corpus", "/nope/missing", "--format", "json"]
        )
        assert result.exit_code == 1
        data = json.loads(result.stdout.strip())
        assert data["status"] == "error"

    def test_text_output_mentions_counts(self, vault: Path) -> None:
        result = runner.invoke(app, ["ingest", "corpus", str(vault)])
        assert result.exit_code == 0
        assert "new=1" in plain(result.stdout)


class TestIngestCorpusHelp:
    """``--help`` states the input contract (ADR §8, #257)."""

    def test_help_states_input_contract(self) -> None:
        result = runner.invoke(app, ["ingest", "corpus", "--help"])
        assert result.exit_code == 0
        # Whitespace-normalised so a phrase Rich wraps still matches.
        text = " ".join(plain(result.stdout).split())
        # Read from the registry, not typed here: registering a handler
        # without stating its extension in the help fails this test.
        extensions = supported_extensions()
        assert extensions
        for ext in extensions:
            # Whole token: ".mdx" in the help must not satisfy ".md".
            token = rf"(?<!\w){re.escape(ext)}(?!\w)"
            assert re.search(token, text), f"{ext!r} missing from corpus --help"
        assert "unsupported" in text
        assert "trellis ingest conversations" in text
