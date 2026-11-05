"""``reindex-vectors`` keeps labelled documents off an undeclared embedder.

Same harness as ``test_admin_reindex_vectors.py``: real SQLite stores in a tmp
config/data dir and an embedder injected through ``TRELLIS_EMBEDDING_FN``. The
embedder records what it is handed, so "kept off the embedder" is asserted
against the embedder and the vector store, not the command's own summary.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tests.unit.retrieve.test_sensitivity import label
from trellis_cli.admin import admin_app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()

EMBED_FN_PATH = "tests.unit.cli.test_admin_reindex_vectors_egress.recording_embed"

SEEN: list[str] = []


def recording_embed(text: str) -> list[float]:
    SEEN.append(text)
    return [1.0, 0.0, float(len(text) % 7)]


@pytest.fixture
def cli_env(tmp_path, monkeypatch) -> None:
    SEEN.clear()
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()


def _seed() -> None:
    docs = _get_registry().knowledge.document_store
    docs.put("plain", "an unlabelled document", metadata={})
    docs.put("internal", "an internal document", metadata=label("internal"))
    docs.put("secret", "a restricted document", metadata=label("restricted"))
    docs.put("private", "a confidential document", metadata=label("confidential"))


def _run(*args: str) -> dict:
    result = runner.invoke(admin_app, ["reindex-vectors", "--format", "json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _vector(doc_id: str):
    return _get_registry().knowledge.vector_store.get(doc_id)


def test_labelled_documents_are_counted_and_kept_off_the_embedder(cli_env) -> None:
    _seed()
    summary = _run()

    assert summary["embedded"] == 2
    assert summary["skipped_egress_withheld"] == 2
    assert summary["skipped_egress_unreadable"] == 0
    assert summary["errors"] == 0
    assert _vector("plain") is not None
    assert _vector("internal") is not None
    assert _vector("secret") is None
    assert _vector("private") is None
    assert sorted(SEEN) == ["an internal document", "an unlabelled document"]


def test_a_declared_local_embedder_embeds_everything(cli_env) -> None:
    _seed()
    _get_registry()._embedding_config = {"local": True}
    summary = _run()

    assert summary["embedded"] == 4
    assert summary["skipped_egress_withheld"] == 0
    assert _vector("secret") is not None
    assert _vector("private") is not None


def test_a_dry_run_counts_what_a_live_run_would_decline(cli_env) -> None:
    _seed()
    summary = _run("--dry-run")

    assert summary["embedded"] == 2
    assert summary["skipped_egress_withheld"] == 2
    assert SEEN == [], "a dry run must not call the embedder"
    assert _vector("plain") is None


def test_the_text_output_names_what_was_kept_off_the_embedder(cli_env) -> None:
    _seed()
    result = runner.invoke(admin_app, ["reindex-vectors"])

    assert result.exit_code == 0, result.output
    assert "Kept off the embedder" in result.stdout
