"""Tests for ``trellis admin resync-vector-metadata`` (trellis-ai#338).

The one-time repair for vector rows that diverged from their documents
before the write-through shipped. Runs through ``CliRunner`` against real
SQLite stores in a tmp config/data dir, the same harness as the other
admin-command tests — and deliberately configures **no embedder**, because
needing none is the point of this command existing beside
``reindex-vectors --force``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from trellis_cli.admin import admin_app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch) -> None:
    """Isolated config/data dirs with initialised SQLite stores, no embedder."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TRELLIS_EMBEDDING_FN", raising=False)
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()


def _seed(
    doc_id: str,
    *,
    doc_tags: dict[str, Any] | None,
    row_tags: dict[str, Any] | None,
    embedded: bool = True,
) -> None:
    """Write a document and (optionally) a vector row that disagrees with it."""
    registry = _get_registry()
    metadata: dict[str, Any] = {"title": doc_id}
    if doc_tags is not None:
        metadata["content_tags"] = doc_tags
    registry.knowledge.document_store.put(doc_id, f"content of {doc_id}", metadata)
    if not embedded:
        return
    row_metadata: dict[str, Any] = {"doc_id": doc_id, "content": f"excerpt {doc_id}"}
    if row_tags is not None:
        row_metadata["content_tags"] = row_tags
    registry.knowledge.vector_store.upsert(doc_id, [0.1, 0.2, 0.3], row_metadata)


def _run_json(*args: str) -> dict:
    result = runner.invoke(
        admin_app, ["resync-vector-metadata", "--format", "json", *args]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output.strip().splitlines()[-1])


class TestRegistration:
    def test_command_registered_on_admin_app(self) -> None:
        names = [cmd.name for cmd in admin_app.registered_commands]
        assert "resync-vector-metadata" in names


class TestRepair:
    """The two divergence shapes production actually had."""

    def test_repairs_a_row_reading_standard(self, cli_env: None) -> None:
        """17 of the 45 production rows still read ``signal_quality="standard"``."""
        _seed(
            "stale",
            doc_tags={"signal_quality": "noise"},
            row_tags={"signal_quality": "standard"},
        )

        summary = _run_json()

        assert summary["divergent"] == 1
        assert summary["repaired"] == 1
        assert summary["errors"] == 0
        row = _get_registry().knowledge.vector_store.get("stale")
        assert row is not None
        assert row["metadata"]["content_tags"]["signal_quality"] == "noise"

    def test_repairs_a_row_with_no_facet_at_all(self, cli_env: None) -> None:
        """The other 28 carried no ``content_tags`` key whatsoever."""
        _seed("bare", doc_tags={"signal_quality": "noise"}, row_tags=None)

        assert _run_json()["repaired"] == 1

        row = _get_registry().knowledge.vector_store.get("bare")
        assert row is not None
        assert row["metadata"]["content_tags"]["signal_quality"] == "noise"

    def test_leaves_agreeing_rows_alone(self, cli_env: None) -> None:
        tags = {"signal_quality": "standard"}
        _seed("agreed", doc_tags=tags, row_tags=tags)

        summary = _run_json()

        assert summary["divergent"] == 0
        assert summary["repaired"] == 0

    def test_second_run_is_a_no_op(self, cli_env: None) -> None:
        """Idempotent, so a steady-state non-zero means a writer is bypassing."""
        _seed(
            "stale",
            doc_tags={"signal_quality": "noise"},
            row_tags={"signal_quality": "standard"},
        )
        assert _run_json()["repaired"] == 1

        assert _run_json()["repaired"] == 0

    def test_preserves_the_embedding_and_the_row_excerpt(self, cli_env: None) -> None:
        """No embedder is configured — a re-embed would be impossible, not slow."""
        _seed(
            "stale",
            doc_tags={"signal_quality": "noise"},
            row_tags={"signal_quality": "standard"},
        )

        _run_json()

        row = _get_registry().knowledge.vector_store.get("stale")
        assert row is not None
        assert row["vector"] == pytest.approx([0.1, 0.2, 0.3])
        assert row["metadata"]["content"] == "excerpt stale"

    def test_un_embedded_documents_are_counted_not_errors(self, cli_env: None) -> None:
        _seed(
            "never", doc_tags={"signal_quality": "noise"}, row_tags=None, embedded=False
        )

        summary = _run_json()

        assert summary["no_vector_row"] == 1
        assert summary["errors"] == 0
        assert summary["repaired"] == 0


class TestDryRun:
    def test_counts_without_writing(self, cli_env: None) -> None:
        _seed(
            "stale",
            doc_tags={"signal_quality": "noise"},
            row_tags={"signal_quality": "standard"},
        )

        summary = _run_json("--dry-run")

        assert summary["dry_run"] is True
        assert summary["divergent"] == 1
        assert summary["repaired"] == 0
        row = _get_registry().knowledge.vector_store.get("stale")
        assert row is not None
        assert row["metadata"]["content_tags"]["signal_quality"] == "standard"


class TestPaging:
    def test_limit_bounds_the_scan(self, cli_env: None) -> None:
        for i in range(5):
            _seed(
                f"doc-{i}",
                doc_tags={"signal_quality": "noise"},
                row_tags={"signal_quality": "standard"},
            )

        summary = _run_json("--limit", "2", "--batch-size", "1")

        assert summary["scanned"] == 2
        assert summary["repaired"] == 2

    def test_pages_past_the_batch_size(self, cli_env: None) -> None:
        for i in range(5):
            _seed(
                f"doc-{i}",
                doc_tags={"signal_quality": "noise"},
                row_tags={"signal_quality": "standard"},
            )

        summary = _run_json("--batch-size", "2")

        assert summary["scanned"] == 5
        assert summary["repaired"] == 5


class TestTextOutput:
    def test_text_summary_names_the_counts(self, cli_env: None) -> None:
        _seed(
            "stale",
            doc_tags={"signal_quality": "noise"},
            row_tags={"signal_quality": "standard"},
        )
        result = runner.invoke(admin_app, ["resync-vector-metadata"])
        assert result.exit_code == 0, result.output
        assert "repaired 1 of 1 scanned" in result.output
