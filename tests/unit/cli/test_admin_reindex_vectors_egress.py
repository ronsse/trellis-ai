"""``reindex-vectors`` keeps labelled documents off an undeclared embedder.

A ``trace-summary:`` document is judged by the documents its trace cites, as
``worker embed-traces`` judges it: ``--force`` is the one way this command
re-embeds one, and a summary carries no label of its own.

Same harness as ``test_admin_reindex_vectors.py``: real SQLite stores in a tmp
config/data dir and an embedder injected through ``TRELLIS_EMBEDDING_FN``. The
embedder records what it is handed, so "kept off the embedder" is asserted
against the embedder and the vector store, not the command's own summary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unit.retrieve.test_sensitivity import label
from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import EvidenceRef, Outcome, Trace, TraceContext
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


# --- a trace summary is judged by the documents its trace cites ---------------

SUMMARY = "trace-summary:trace-0001"
STALE = {"content": "stale"}
#: ``--force`` walks every document, so the restricted document the trace cites
#: is itself withheld in the same run; a count for the summary is this plus one.
SECRET_ITSELF = 1


def _trace(*cited: str) -> Trace:
    when = datetime(2026, 8, 1, tzinfo=UTC)
    return Trace(
        trace_id="trace-0001",
        source=TraceSource.AGENT,
        intent="Investigate widget-0001",
        outcome=Outcome(status=OutcomeStatus.SUCCESS, summary="Finished"),
        evidence_used=[EvidenceRef(evidence_id=c) for c in cited],
        context=TraceContext(domain="platform", started_at=when),
        created_at=when,
        updated_at=when,
    )


def _seed_summary(*cited: str, trace: bool = True) -> None:
    """A trace summary that already has a (stale) vector, so ``--force`` reaches it."""
    registry = _get_registry()
    docs = registry.knowledge.document_store
    docs.put("open-doc", "an open document", metadata=label("public"))
    docs.put("secret-doc", "a restricted document", metadata=label("restricted"))
    docs.put(SUMMARY, "the summary of a trace", metadata={})
    registry.knowledge.vector_store.upsert(SUMMARY, [9.0, 9.0, 9.0], STALE)
    if trace:
        registry.operational.trace_store.append(_trace(*cited))


def _summary_vector() -> dict:
    row = _vector(SUMMARY)
    assert row is not None
    return row


def test_a_summary_of_a_trace_citing_a_restricted_document_is_not_re_embedded(
    cli_env,
) -> None:
    _seed_summary("open-doc", "secret-doc")

    summary = _run("--force")

    assert summary["skipped_egress_withheld"] == SECRET_ITSELF + 1
    assert summary["skipped_egress_unreadable"] == 0
    assert summary["errors"] == 0
    assert "the summary of a trace" not in SEEN
    # The existing row is neither refreshed nor dropped.
    assert _summary_vector()["metadata"]["content"] == "stale"


def test_a_summary_of_a_trace_citing_only_open_documents_is_re_embedded(
    cli_env,
) -> None:
    _seed_summary("open-doc")

    summary = _run("--force")

    assert summary["skipped_egress_withheld"] == SECRET_ITSELF
    assert summary["skipped_egress_unreadable"] == 0
    assert "the summary of a trace" in SEEN
    assert _summary_vector()["metadata"]["content"] == "the summary of a trace"


def test_a_summary_is_re_embedded_when_the_embedder_is_declared_local(
    cli_env,
) -> None:
    _seed_summary("secret-doc")
    _get_registry()._embedding_config = {"local": True}

    summary = _run("--force")

    assert summary["skipped_egress_withheld"] == 0
    assert _summary_vector()["metadata"]["content"] == "the summary of a trace"


def test_a_summary_whose_trace_cannot_be_loaded_is_unreadable(cli_env) -> None:
    _seed_summary("open-doc", trace=False)

    summary = _run("--force")

    assert summary["skipped_egress_unreadable"] == 1
    assert summary["skipped_egress_withheld"] == SECRET_ITSELF
    assert "the summary of a trace" not in SEEN
    assert _summary_vector()["metadata"]["content"] == "stale"


def test_a_summary_whose_trace_store_fails_is_unreadable(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_summary("open-doc")
    trace_store = _get_registry().operational.trace_store

    def broken(trace_id):
        msg = "trace store unavailable (staged)"
        raise RuntimeError(msg)

    monkeypatch.setattr(trace_store, "get", broken)

    summary = _run("--force")

    assert summary["skipped_egress_unreadable"] == 1
    assert summary["errors"] == 0
    assert _summary_vector()["metadata"]["content"] == "stale"


def test_a_summary_whose_cited_document_cannot_be_read_is_unreadable(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_summary("open-doc")
    docs = _get_registry().knowledge.document_store
    real_get = docs.get

    def get(doc_id, *args, **kwargs):
        if doc_id == "open-doc":
            msg = "document store unavailable (staged)"
            raise RuntimeError(msg)
        return real_get(doc_id, *args, **kwargs)

    monkeypatch.setattr(docs, "get", get)

    summary = _run("--force")

    assert summary["skipped_egress_unreadable"] == 1
    assert _summary_vector()["metadata"]["content"] == "stale"


def test_a_chunk_whose_parent_cannot_be_read_is_unreadable(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document's own verdict still counts beside a summary's cited one."""
    docs = _get_registry().knowledge.document_store
    docs.put("parent-doc", "an unlabelled parent", metadata={})
    docs.put("parent-doc#chunk-0", "an unlabelled chunk", metadata={})
    real_get = docs.get

    def get(doc_id, *args, **kwargs):
        if doc_id == "parent-doc":
            msg = "document store unavailable (staged)"
            raise RuntimeError(msg)
        return real_get(doc_id, *args, **kwargs)

    monkeypatch.setattr(docs, "get", get)

    dry = _run("--dry-run")
    assert dry["skipped_egress_unreadable"] == 1
    assert dry["embedded"] == 1

    summary = _run()

    assert summary["skipped_egress_unreadable"] == 1
    assert summary["embedded"] == 1
    assert _vector("parent-doc#chunk-0") is None


def test_a_dry_run_counts_the_summary_as_a_live_run_would(cli_env) -> None:
    _seed_summary("secret-doc")

    summary = _run("--force", "--dry-run")

    assert summary["skipped_egress_withheld"] == SECRET_ITSELF + 1
    assert SEEN == []


def test_a_local_embedder_does_not_open_the_trace_store(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared-local embedder reads nothing, so it must not need the store."""
    _seed_summary("secret-doc", trace=False)
    registry = _get_registry()
    registry._embedding_config = {"local": True}
    opened: list[str] = []
    real = type(registry.operational).trace_store.fget

    def spy(self):
        opened.append("trace_store")
        return real(self)

    monkeypatch.setattr(type(registry.operational), "trace_store", property(spy))

    summary = _run("--force")

    assert summary["skipped_egress_unreadable"] == 0
    assert opened == []


def test_the_text_output_words_each_count_by_its_verdict(
    cli_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_summary("open-doc", trace=False)
    result = runner.invoke(admin_app, ["reindex-vectors", "--force"])
    out = " ".join(plain(result.stdout).split())

    assert result.exit_code == 0, result.output
    assert "1 withheld (egress_withheld)" in out
    assert "1 unreadable (egress_unreadable)" in out
    # An unreadable skip is not a statement about the embedder's declaration.
    unreadable_line = out.split("1 unreadable (egress_unreadable)", 1)[1]
    assert "not declared true" not in unreadable_line
