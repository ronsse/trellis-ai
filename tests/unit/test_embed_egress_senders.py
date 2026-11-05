"""Labelled content reaches an embedder only when ``embeddings.local`` says so.

The behaviour half of ``test_embed_egress_rule.py``: that rule is a tripwire over
the shapes of an embed site, and this is the guard. Each sender below is run
against real SQLite stores and a *recording* embedder, so "the embedder saw
nothing" is an assertion about the embedder and not about a return value.

For a document labelled above ``internal`` under an embedder not declared local:
no vector row, the embedder is never called, and the skip is counted. Under
``embeddings: {local: true}`` the same document is embedded exactly as before.
An unlabelled or ``internal`` document is embedded either way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.unit.retrieve.test_sensitivity import label
from trellis.errors import ConfigError
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.ingest_corpus.sync import sync_corpus
from trellis.llm.egress import (
    EGRESS_UNREADABLE_REASON,
    EGRESS_WITHHELD_REASON,
    EgressGate,
    EgressVerdict,
)
from trellis.llm.routing import resolve_embedding_local
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandStatus
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis.mutate.handlers.evidence_handler import EvidenceIngestHandler
from trellis.ops.embed_egress_health import (
    embed_egress_reasons,
    emit_embed_withheld,
    summarize_embed_egress,
)
from trellis.ops.write_health import summarize_backend_health
from trellis.retrieve.embed_ingest_hook import (
    EMBED_ON_INGEST_FLAG,
    build_vector_row,
    run_embed_on_ingest,
)
from trellis.retrieve.strategies import SemanticSearch
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.vector import SQLiteVectorStore

#: Dotted path handed to ``TRELLIS_EMBEDDING_FN``; it cannot carry a closure.
EMBED_FN_PATH = "tests.unit.test_embed_egress_senders.embed"

#: Labels above ``internal``: the ones an undeclared embedder must not see.
ABOVE_INTERNAL = ("confidential", "restricted")
#: Everything else: embedded whether or not the embedder is declared local.
AT_OR_BELOW_INTERNAL = (None, "public", "internal")

TEXTS: list[str] = []


def embed(text: str) -> list[float]:
    """Recording embedder: every text it is handed lands in ``TEXTS``."""
    TEXTS.append(text)
    return [1.0, float(len(text) % 7), 0.5]


def _metadata(level: str | None) -> dict[str, Any]:
    return {} if level is None else label(level)


@pytest.fixture(autouse=True)
def _recording(monkeypatch: pytest.MonkeyPatch) -> None:
    TEXTS.clear()
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")


def make_registry(tmp_path: Path, *, local: bool | None) -> StoreRegistry:
    """Real SQLite stores; ``embeddings.local`` declared, or silent for ``None``."""
    stores = tmp_path / "stores"
    stores.mkdir(exist_ok=True)
    config = {} if local is None else {"local": local}
    return StoreRegistry(stores_dir=stores, embedding_config=config)


def _events(registry: StoreRegistry) -> list[Any]:
    return registry.operational.event_log.get_events(
        event_type=EventType.EMBED_WITHHELD, limit=100
    )


def _vector(registry: StoreRegistry, doc_id: str) -> dict[str, Any] | None:
    return registry.knowledge.vector_store.get(doc_id)


class TestHook:
    """``run_embed_on_ingest``: the inline path every ingest surface shares."""

    @pytest.mark.parametrize("level", ABOVE_INTERNAL)
    def test_labelled_under_an_undeclared_embedder_gets_no_vector(
        self, tmp_path: Path, level: str
    ) -> None:
        registry = make_registry(tmp_path, local=None)
        summary = run_embed_on_ingest(
            registry, "doc-1", "body", _metadata(level), source="test"
        )
        assert summary == {"embedded": False, "reason": EGRESS_WITHHELD_REASON}
        assert _vector(registry, "doc-1") is None
        assert TEXTS == [], "the embedder was handed labelled content"

    @pytest.mark.parametrize("level", ABOVE_INTERNAL)
    def test_the_skip_is_counted_as_an_event_without_content(
        self, tmp_path: Path, level: str
    ) -> None:
        registry = make_registry(tmp_path, local=False)
        run_embed_on_ingest(
            registry, "doc-1", "secret body", _metadata(level), source="test"
        )
        (event,) = _events(registry)
        assert event.entity_id == "doc-1"
        assert event.payload == {
            "verdict": "withheld",
            "reason": EGRESS_WITHHELD_REASON,
        }
        assert "secret body" not in repr(event.payload)

    @pytest.mark.parametrize("level", ABOVE_INTERNAL)
    def test_labelled_under_a_declared_local_embedder_is_embedded(
        self, tmp_path: Path, level: str
    ) -> None:
        registry = make_registry(tmp_path, local=True)
        summary = run_embed_on_ingest(
            registry, "doc-1", "body", _metadata(level), source="test"
        )
        assert summary is not None
        assert summary["embedded"] is True
        assert _vector(registry, "doc-1") is not None
        assert TEXTS == ["body"]
        assert _events(registry) == []

    @pytest.mark.parametrize("local", [None, False, True])
    @pytest.mark.parametrize("level", AT_OR_BELOW_INTERNAL)
    def test_unlabelled_and_internal_are_embedded_either_way(
        self, tmp_path: Path, level: str | None, local: bool | None
    ) -> None:
        registry = make_registry(tmp_path, local=local)
        summary = run_embed_on_ingest(
            registry, "doc-1", "body", _metadata(level), source="test"
        )
        assert summary is not None
        assert summary["embedded"] is True
        assert _vector(registry, "doc-1") is not None
        assert _events(registry) == []

    def test_a_declared_local_embedder_writes_the_row_it_always_wrote(
        self, tmp_path: Path
    ) -> None:
        """Byte for byte: the same row an open gate builds, key order included."""
        registry = make_registry(tmp_path, local=True)
        metadata = {**label("restricted"), "created_at": "2026-01-01T00:00:00+00:00"}
        run_embed_on_ingest(registry, "doc-1", "body", metadata, source="test")
        expected = build_vector_row(
            "doc-1", "body", metadata, embed, egress=EgressGate(None, local=True)
        )
        stored = _vector(registry, "doc-1")
        assert stored is not None
        assert stored["metadata"] == expected["metadata"]
        assert list(stored["metadata"]) == list(expected["metadata"])

    def test_the_declaration_is_not_inferred_from_a_localhost_base_url(
        self, tmp_path: Path
    ) -> None:
        stores = tmp_path / "stores"
        stores.mkdir()
        registry = StoreRegistry(
            stores_dir=stores,
            embedding_config={
                "provider": "openai",
                "base_url": "http://localhost:11434/v1",
            },
        )
        assert registry.embedding_local is False
        run_embed_on_ingest(
            registry, "doc-1", "body", _metadata("restricted"), source="test"
        )
        assert _vector(registry, "doc-1") is None


class TestChunks:
    """A chunk is judged by its parent's label, as in retrieval."""

    def _chunk_meta(self, parent: str) -> dict[str, Any]:
        return {"parent_doc_id": parent}

    @pytest.mark.parametrize("level", ABOVE_INTERNAL)
    def test_a_chunk_of_a_labelled_parent_is_withheld(
        self, tmp_path: Path, level: str
    ) -> None:
        registry = make_registry(tmp_path, local=None)
        registry.knowledge.document_store.put("parent", "whole", _metadata(level))
        chunk = chunk_doc_id("parent", 0)
        summary = run_embed_on_ingest(
            registry, chunk, "part", self._chunk_meta("parent"), source="test"
        )
        assert summary == {"embedded": False, "reason": EGRESS_WITHHELD_REASON}
        assert _vector(registry, chunk) is None
        assert TEXTS == []

    def test_a_chunk_of_an_internal_parent_is_embedded(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path, local=None)
        registry.knowledge.document_store.put("parent", "whole", _metadata("internal"))
        chunk = chunk_doc_id("parent", 0)
        run_embed_on_ingest(
            registry, chunk, "part", self._chunk_meta("parent"), source="test"
        )
        assert _vector(registry, chunk) is not None

    def test_a_chunk_of_a_labelled_parent_is_embedded_when_local(
        self, tmp_path: Path
    ) -> None:
        registry = make_registry(tmp_path, local=True)
        registry.knowledge.document_store.put(
            "parent", "whole", _metadata("restricted")
        )
        chunk = chunk_doc_id("parent", 0)
        run_embed_on_ingest(
            registry, chunk, "part", self._chunk_meta("parent"), source="test"
        )
        assert _vector(registry, chunk) is not None

    def test_a_chunk_whose_parent_is_gone_is_withheld(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path, local=None)
        chunk = chunk_doc_id("missing", 0)
        summary = run_embed_on_ingest(
            registry, chunk, "part", self._chunk_meta("missing"), source="test"
        )
        assert summary == {"embedded": False, "reason": EGRESS_WITHHELD_REASON}
        assert TEXTS == []


class TestUnreadable:
    """A store that could not answer is counted apart from a label that said no."""

    def test_an_unreadable_parent_is_unreadable_not_withheld(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = make_registry(tmp_path, local=None)

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            msg = "store down"
            raise OSError(msg)

        monkeypatch.setattr(registry.knowledge.document_store, "get", boom)
        chunk = chunk_doc_id("parent", 0)
        summary = run_embed_on_ingest(
            registry, chunk, "part", {"parent_doc_id": "parent"}, source="test"
        )
        assert summary == {"embedded": False, "reason": EGRESS_UNREADABLE_REASON}
        assert TEXTS == []
        (event,) = _events(registry)
        assert event.payload["verdict"] == "unreadable"

    def test_the_gate_reports_the_two_verdicts_apart(self) -> None:
        broken = MagicMock()
        broken.get.side_effect = OSError("store down")
        broken.get_bulk.side_effect = OSError("store down")
        chunk = chunk_doc_id("parent", 0)
        meta = {"parent_doc_id": "parent"}
        gate = EgressGate(broken, local=False)
        assert gate.verdict(chunk, meta) is EgressVerdict.UNREADABLE
        assert EgressGate(None, local=False).verdict(chunk, meta) is (
            EgressVerdict.UNREADABLE
        )
        assert EgressGate(None, local=False).verdict("d", label("restricted")) is (
            EgressVerdict.WITHHELD
        )
        assert EgressGate(None, local=True).verdict(chunk, meta) is (
            EgressVerdict.PERMITTED
        )


def _evidence_command(level: str | None, doc_id: str, mode: str) -> Any:
    return build_evidence_ingest_command(
        doc_id=doc_id,
        content="a memory worth keeping",
        metadata=_metadata(level),
        requested_by="test",
        embed_mode=mode,  # type: ignore[arg-type]
    )


class TestEvidenceHandlerSoft:
    """``evidence.ingest`` in soft mode: the fail-soft hook, through the executor."""

    def _ingest(self, registry: StoreRegistry, level: str | None) -> Any:
        executor = build_curate_executor(
            registry, evidence_embed="soft", clearance="restricted"
        )
        return executor.execute(_evidence_command(level, "mem-1", "soft"))

    @pytest.mark.parametrize("level", ABOVE_INTERNAL)
    def test_labelled_is_stored_without_a_vector_and_counted(
        self, tmp_path: Path, level: str
    ) -> None:
        registry = make_registry(tmp_path, local=None)
        result = self._ingest(registry, level)
        assert result.status is CommandStatus.SUCCESS
        assert registry.knowledge.document_store.get("mem-1") is not None
        assert _vector(registry, "mem-1") is None
        assert TEXTS == []
        assert len(_events(registry)) == 1

    @pytest.mark.parametrize("level", ABOVE_INTERNAL)
    def test_labelled_is_embedded_when_the_embedder_is_local(
        self, tmp_path: Path, level: str
    ) -> None:
        registry = make_registry(tmp_path, local=True)
        assert self._ingest(registry, level).status is CommandStatus.SUCCESS
        assert _vector(registry, "mem-1") is not None
        assert _events(registry) == []

    @pytest.mark.parametrize("level", AT_OR_BELOW_INTERNAL)
    def test_unlabelled_and_internal_are_embedded(
        self, tmp_path: Path, level: str | None
    ) -> None:
        registry = make_registry(tmp_path, local=None)
        assert self._ingest(registry, level).status is CommandStatus.SUCCESS
        assert _vector(registry, "mem-1") is not None


class TestEvidenceHandlerStrict:
    """Strict mode is the trace-embed worker's: not fail-soft, so it signals."""

    def _handle(self, registry: StoreRegistry, level: str | None) -> Any:
        handler = EvidenceIngestHandler(registry, embed="strict", embedding_fn=embed)
        result = handler.handle(_evidence_command(level, "mem-1", "strict"))
        return handler, result

    @pytest.mark.parametrize("level", ABOVE_INTERNAL)
    def test_labelled_is_stored_without_a_vector_and_signalled(
        self, tmp_path: Path, level: str
    ) -> None:
        registry = make_registry(tmp_path, local=None)
        handler, (doc_id, message) = self._handle(registry, level)
        assert doc_id == "mem-1"
        assert "without a vector" in message
        assert handler.last_embed_withheld is EgressVerdict.WITHHELD
        assert registry.knowledge.document_store.get("mem-1") is not None
        assert _vector(registry, "mem-1") is None
        assert TEXTS == []
        assert len(_events(registry)) == 1

    @pytest.mark.parametrize("level", ABOVE_INTERNAL)
    def test_labelled_is_embedded_when_the_embedder_is_local(
        self, tmp_path: Path, level: str
    ) -> None:
        registry = make_registry(tmp_path, local=True)
        handler, _ = self._handle(registry, level)
        assert handler.last_embed_withheld is None
        assert _vector(registry, "mem-1") is not None
        assert _events(registry) == []

    @pytest.mark.parametrize("level", AT_OR_BELOW_INTERNAL)
    def test_unlabelled_and_internal_are_embedded(
        self, tmp_path: Path, level: str | None
    ) -> None:
        registry = make_registry(tmp_path, local=None)
        handler, _ = self._handle(registry, level)
        assert handler.last_embed_withheld is None
        assert _vector(registry, "mem-1") is not None

    def test_the_signal_is_reset_for_the_next_command(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path, local=None)
        handler = EvidenceIngestHandler(registry, embed="strict", embedding_fn=embed)
        handler.handle(_evidence_command("restricted", "mem-1", "strict"))
        assert handler.last_embed_withheld is EgressVerdict.WITHHELD
        handler.handle(_evidence_command(None, "mem-2", "strict"))
        assert handler.last_embed_withheld is None


class TestCorpusSync:
    """A batch job counts the skip in its own report."""

    def _vault(self, tmp_path: Path) -> Path:
        root = tmp_path / "vault"
        root.mkdir()
        (root / "a.md").write_text("Alpha content.\n", encoding="utf-8")
        (root / "b.md").write_text("Beta content.\n", encoding="utf-8")
        return root

    def test_labelled_notes_are_counted_and_not_embedded(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path, local=None)
        report = sync_corpus(
            registry,
            self._vault(tmp_path),
            source_system="vault",
            extra_metadata=label("restricted"),
        )
        assert report.embed_skipped_egress_withheld == 2
        assert report.embed_skipped_egress_unreadable == 0
        assert report.counts()["embed_skipped_egress_withheld"] == 2
        assert registry.knowledge.vector_store.count() == 0
        assert TEXTS == []

    def test_the_same_notes_are_embedded_when_the_embedder_is_local(
        self, tmp_path: Path
    ) -> None:
        registry = make_registry(tmp_path, local=True)
        report = sync_corpus(
            registry,
            self._vault(tmp_path),
            source_system="vault",
            extra_metadata=label("restricted"),
        )
        assert report.embed_skipped_egress_withheld == 0
        assert registry.knowledge.vector_store.count() == 2

    def test_unlabelled_notes_are_embedded_and_nothing_is_counted(
        self, tmp_path: Path
    ) -> None:
        registry = make_registry(tmp_path, local=None)
        report = sync_corpus(registry, self._vault(tmp_path), source_system="vault")
        assert report.embed_skipped_egress_withheld == 0
        assert registry.knowledge.vector_store.count() == 2


class TestHealth:
    """The skip surfaces in ``trellis analyze health`` as an event, not a probe."""

    def _log(self, tmp_path: Path) -> SQLiteEventLog:
        return SQLiteEventLog(tmp_path / "events.db")

    def test_withheld_is_counted_and_is_not_a_warning(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        assert emit_embed_withheld(log, "d1", EgressVerdict.WITHHELD, source="t")
        assert emit_embed_withheld(log, "d2", EgressVerdict.WITHHELD, source="t")
        report = summarize_embed_egress(log, days=7)
        assert (report.withheld, report.unreadable) == (2, 0)
        assert embed_egress_reasons(report) == []

    def test_unreadable_is_counted_apart_and_warns(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        emit_embed_withheld(log, "d1", EgressVerdict.WITHHELD, source="t")
        emit_embed_withheld(log, "d2", EgressVerdict.UNREADABLE, source="t")
        report = summarize_embed_egress(log, days=7)
        assert (report.withheld, report.unreadable) == (1, 1)
        assert report.unreadable_docs == ["d2"]
        (reason,) = embed_egress_reasons(report)
        assert "unreadable" in reason.lower()

    def test_a_permitted_verdict_emits_nothing(self, tmp_path: Path) -> None:
        log = self._log(tmp_path)
        assert (
            emit_embed_withheld(log, "d1", EgressVerdict.PERMITTED, source="t") is False
        )
        assert summarize_embed_egress(log, days=7).withheld == 0

    def test_a_failing_event_log_never_raises(self) -> None:
        log = MagicMock()
        log.emit.side_effect = OSError("disk full")
        assert (
            emit_embed_withheld(log, "d1", EgressVerdict.WITHHELD, source="t") is False
        )
        assert (
            emit_embed_withheld(None, "d1", EgressVerdict.WITHHELD, source="t") is False
        )

    def test_the_backend_health_report_carries_the_count(self, tmp_path: Path) -> None:
        registry = make_registry(tmp_path, local=None)
        run_embed_on_ingest(
            registry, "doc-1", "body", _metadata("restricted"), source="test"
        )
        report = summarize_backend_health(registry.operational.event_log, days=7)
        assert report.embed_egress is not None
        assert report.embed_egress.withheld == 1


class TestDeclaration:
    """``embeddings.local`` is validated as an LLM route's ``local`` is."""

    @pytest.mark.parametrize("block", [None, "x", [], {}, {"local": False}])
    def test_silent_or_false_is_cloud(self, block: Any) -> None:
        assert resolve_embedding_local(block) is False

    def test_true_is_local(self) -> None:
        assert resolve_embedding_local({"local": True}) is True

    def test_true_with_an_endpoint_you_run_is_local(self) -> None:
        block = {
            "provider": "openai",
            "base_url": "http://embedder:8080/v1",
            "local": True,
        }
        assert resolve_embedding_local(block) is True

    @pytest.mark.parametrize("bad", ["true", "yes", 1, 0, "", []])
    def test_a_non_boolean_is_refused(self, bad: Any) -> None:
        with pytest.raises(ConfigError):
            resolve_embedding_local({"local": bad})

    def test_local_on_a_hosted_default_endpoint_is_refused(self) -> None:
        with pytest.raises(ConfigError, match=r"embeddings\.local"):
            resolve_embedding_local({"provider": "openai", "local": True})

    def test_a_localhost_base_url_alone_does_not_declare(self) -> None:
        block = {"provider": "openai", "base_url": "http://localhost:11434/v1"}
        assert resolve_embedding_local(block) is False

    def test_a_malformed_declaration_fails_where_the_embedder_resolves(
        self, tmp_path: Path
    ) -> None:
        registry = make_registry(tmp_path, local=None)
        registry._embedding_config = {"local": "yes"}  # type: ignore[attr-defined]
        with pytest.raises(ConfigError):
            registry.embedding_fn  # noqa: B018

    def test_the_env_override_does_not_decide_the_declaration(
        self, tmp_path: Path
    ) -> None:
        """``TRELLIS_EMBEDDING_FN`` names a callable, not where it sends input."""
        registry = make_registry(tmp_path, local=None)
        assert registry.embedding_fn is not None
        assert registry.embedding_local is False


class TestQueryEmbeddingIsNotGated:
    """A caller's intent is not stored content: it is embedded whatever the label."""

    def test_the_query_reaches_a_non_local_embedder(self, tmp_path: Path) -> None:
        store = SQLiteVectorStore(tmp_path / "vectors.db")
        store.upsert(
            "doc-1", [1.0, 0.0, 0.5], {"doc_id": "doc-1", **label("restricted")}
        )
        search = SemanticSearch(store, embedding_fn=embed)
        search.search("what is the restricted plan", limit=3)
        assert TEXTS == ["what is the restricted plan"]
