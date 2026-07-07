"""Tests for ``trellis admin reindex-vectors``.

Runs the command through CliRunner against real SQLite stores in a
tmp config/data dir (the same harness as the other admin-command
tests). The embedder is injected via ``TRELLIS_EMBEDDING_FN`` — the
env override the registry checks first — pointing at
:func:`fake_embed` below, so no network or provider extra is needed.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from trellis_cli.admin import admin_app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()

#: Dotted path handed to TRELLIS_EMBEDDING_FN in the tests below.
EMBED_FN_PATH = "tests.unit.cli.test_admin_reindex_vectors.fake_embed"


def fake_embed(text: str) -> list[float]:
    """Deterministic 3-dim embedding for CLI tests."""
    return [1.0, 0.0, float(len(text) % 7)]


@pytest.fixture
def cli_env(tmp_path, monkeypatch) -> None:
    """Isolated config/data dirs with initialised SQLite stores."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()


def _seed_documents() -> None:
    registry = _get_registry()
    registry.knowledge.document_store.put("doc-1", "first document", metadata={})
    registry.knowledge.document_store.put(
        "doc-2", "second document", metadata={"domain": "backend"}
    )
    registry.knowledge.document_store.put("doc-empty", "", metadata={})


def _run_json(*args: str) -> dict:
    result = runner.invoke(admin_app, ["reindex-vectors", "--format", "json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output.strip().splitlines()[-1])


class TestReindexVectorsCLI:
    def test_command_registered_on_admin_app(self) -> None:
        names = [cmd.name for cmd in admin_app.registered_commands]
        assert "reindex-vectors" in names

    def test_missing_embedder_exits_loudly(self, cli_env, monkeypatch) -> None:
        monkeypatch.delenv("TRELLIS_EMBEDDING_FN", raising=False)
        _reset_registry()
        result = runner.invoke(admin_app, ["reindex-vectors", "--format", "json"])
        assert result.exit_code == 1
        assert "error" in result.output

    def test_backfills_and_skips_on_rerun(self, cli_env) -> None:
        _seed_documents()

        summary = _run_json()
        assert summary["scanned"] == 3
        assert summary["embedded"] == 2
        assert summary["skipped_empty"] == 1
        assert summary["errors"] == 0

        # Vectors landed, keyed by doc_id, carrying the excerpt + metadata.
        vector_store = _get_registry().knowledge.vector_store
        row = vector_store.get("doc-2")
        assert row is not None
        assert row["metadata"]["content"] == "second document"
        assert row["metadata"]["domain"] == "backend"

        # Rerun: everything already indexed.
        rerun = _run_json()
        assert rerun["embedded"] == 0
        assert rerun["skipped_existing"] == 2

        # --force re-embeds.
        forced = _run_json("--force")
        assert forced["embedded"] == 2

    def test_dry_run_counts_without_writing(self, cli_env) -> None:
        _seed_documents()
        summary = _run_json("--dry-run")
        assert summary["dry_run"] is True
        assert summary["embedded"] == 2
        assert _get_registry().knowledge.vector_store.count() == 0

    def test_limit_bounds_scan(self, cli_env) -> None:
        _seed_documents()
        summary = _run_json("--limit", "1")
        assert summary["scanned"] == 1
