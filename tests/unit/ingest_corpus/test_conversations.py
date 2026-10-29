"""Claude conversation-export reader + sync tests."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.redaction_tombstone import tombstone_metadata
from trellis.core.document_write import is_redaction_tombstone, put_document
from trellis.ingest_corpus.conversations import (
    conversation_doc_id,
    read_claude_export,
    sync_conversations,
)
from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.embed_ingest_hook import EMBED_ON_INGEST_FLAG
from trellis.schemas.document_metadata import document_form_of
from trellis.stores.base.event_log import EventType
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.event_log import SQLiteEventLog
from trellis.stores.sqlite.vector import SQLiteVectorStore

_DIMS = 64


def _embed(text: str) -> list[float]:
    vector = [0.0] * _DIMS
    for word in text.lower().split():
        digest = hashlib.md5(word.encode(), usedforsecurity=False).digest()
        vector[digest[0] % _DIMS] += 1.0
    norm = sum(v * v for v in vector) ** 0.5 or 1.0
    return [v / norm for v in vector]


@pytest.fixture
def registry(tmp_path: Path) -> MagicMock:
    reg = MagicMock()
    reg.knowledge.document_store = SQLiteDocumentStore(tmp_path / "docs.db")
    reg.knowledge.vector_store = SQLiteVectorStore(tmp_path / "vectors.db")
    reg.operational.event_log = SQLiteEventLog(tmp_path / "events.db")
    reg.embedding_fn = _embed
    return reg


def _export(conversations: list[dict]) -> list[dict]:
    return conversations


def _write_export(path: Path, conversations: list[dict]) -> Path:
    target = path / "conversations.json"
    target.write_text(json.dumps(conversations))
    return target


_CONV_OLD = {
    "uuid": "conv-1",
    "name": "Retirement planning",
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-01T10:20:00Z",
    "chat_messages": [
        {"sender": "human", "text": "Set up custodial Roths for the kids?"},
        {
            "sender": "assistant",
            "text": "You'll need earned income for a custodial Roth.",
        },
    ],
}
_CONV_NEW_SHAPE = {
    "uuid": "conv-2",
    "name": "",
    "chat_messages": [
        {
            "sender": "human",
            "text": "",
            "content": [{"type": "text", "text": "Best espresso grind?"}],
        },
        {
            "sender": "assistant",
            "text": "",
            "content": [
                {"type": "thinking", "text": "internal reasoning"},
                {"type": "text", "text": "A fine, table-salt grind."},
                {"type": "tool_use", "name": "search"},
            ],
        },
    ],
}


class TestReader:
    def test_parses_old_text_shape(self, tmp_path: Path):
        src = _write_export(tmp_path, [_CONV_OLD])
        records, warnings = read_claude_export(src)
        assert warnings == []
        assert len(records) == 1
        rec = records[0]
        assert rec.doc_id == conversation_doc_id("claude-ai", "conv-1")
        assert rec.handler_metadata["title"] == "Retirement planning"
        assert rec.handler_metadata["message_count"] == 2
        assert rec.handler_metadata["document_form"] == "conversation"
        assert "content_type" not in rec.handler_metadata
        assert rec.handler_metadata["created_at"] == "2026-06-01T10:00:00Z"
        assert "**You:** Set up custodial Roths" in rec.content
        assert "**Claude:** You'll need earned income" in rec.content

    def test_parses_new_content_block_shape_and_skips_nontext_blocks(
        self, tmp_path: Path
    ):
        src = _write_export(tmp_path, [_CONV_NEW_SHAPE])
        records, _ = read_claude_export(src)
        rec = records[0]
        # thinking + tool_use blocks excluded → 2 rendered turns.
        assert rec.handler_metadata["message_count"] == 2
        assert "A fine, table-salt grind." in rec.content
        assert "internal reasoning" not in rec.content

    def test_untitled_fallback(self, tmp_path: Path):
        src = _write_export(tmp_path, [_CONV_NEW_SHAPE])
        records, _ = read_claude_export(src)
        assert records[0].handler_metadata["title"] == "Untitled conversation"

    def test_empty_conversation_skipped_with_warning(self, tmp_path: Path):
        src = _write_export(tmp_path, [{"uuid": "c", "name": "x", "chat_messages": []}])
        records, warnings = read_claude_export(src)
        assert records == []
        assert warnings[0]["kind"] == "empty_conversation"

    def test_missing_id_skipped_with_warning(self, tmp_path: Path):
        src = _write_export(
            tmp_path,
            [{"name": "x", "chat_messages": [{"sender": "human", "text": "hi"}]}],
        )
        records, warnings = read_claude_export(src)
        assert records == []
        assert warnings[0]["kind"] == "conversation_missing_id"

    def test_reads_from_zip(self, tmp_path: Path):
        zip_path = tmp_path / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("conversations.json", json.dumps([_CONV_OLD]))
            archive.writestr("users.json", "{}")
        records, warnings = read_claude_export(zip_path)
        assert warnings == []
        assert len(records) == 1

    def test_reads_from_directory(self, tmp_path: Path):
        _write_export(tmp_path, [_CONV_OLD])
        records, _ = read_claude_export(tmp_path)
        assert len(records) == 1

    def test_malformed_json_warns_not_raises(self, tmp_path: Path):
        bad = tmp_path / "conversations.json"
        bad.write_text("{not json")
        records, warnings = read_claude_export(bad)
        assert records == []
        assert warnings[0]["kind"] == "malformed_export"

    def test_dict_with_conversations_key(self, tmp_path: Path):
        src = tmp_path / "conversations.json"
        src.write_text(json.dumps({"conversations": [_CONV_OLD]}))
        records, _ = read_claude_export(src)
        assert len(records) == 1

    def test_single_conversation_object(self, tmp_path: Path):
        src = tmp_path / "conversations.json"
        src.write_text(json.dumps(_CONV_OLD))
        records, _ = read_claude_export(src)
        assert len(records) == 1

    def test_role_key_fallback(self, tmp_path: Path):
        conv = {
            "uuid": "api-1",
            "name": "API chat",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        }
        records, _ = read_claude_export(_write_export(tmp_path, [conv]))
        assert records[0].handler_metadata["message_count"] == 2
        assert "**You:** hello" in records[0].content


class TestSyncConversations:
    def test_ingests_and_emits_events(self, registry, tmp_path: Path):
        src = _write_export(tmp_path, [_CONV_OLD, _CONV_NEW_SHAPE])
        report = sync_conversations(registry, src)
        assert report.counts()["ingested"] == 2

        doc = registry.knowledge.document_store.get(
            conversation_doc_id("claude-ai", "conv-1")
        )
        assert doc["metadata"]["document_form"] == "conversation"
        assert document_form_of(doc["metadata"]) == "conversation"

        stored = registry.operational.event_log.get_events(
            event_type=EventType.MEMORY_STORED
        )
        assert {e.entity_id for e in stored} == {
            conversation_doc_id("claude-ai", "conv-1"),
            conversation_doc_id("claude-ai", "conv-2"),
        }
        summary = registry.operational.event_log.get_events(
            event_type=EventType.CORPUS_SYNCED
        )
        assert summary[0].payload["source_system"] == "claude-ai"

    def test_second_run_is_idempotent(self, registry, tmp_path: Path):
        src = _write_export(tmp_path, [_CONV_OLD])
        sync_conversations(registry, src)
        report = sync_conversations(registry, src)
        assert report.counts()["skipped_unchanged"] == 1
        assert report.counts()["ingested"] == 0

    def test_new_turns_reput_the_conversation(self, registry, tmp_path: Path):
        src = _write_export(tmp_path, [_CONV_OLD])
        sync_conversations(registry, src)
        grown = json.loads(json.dumps(_CONV_OLD))
        grown["chat_messages"].append(
            {"sender": "human", "text": "One more follow-up question."}
        )
        _write_export(tmp_path, [grown])
        report = sync_conversations(registry, src)
        assert report.counts()["updated"] == 1
        doc = registry.knowledge.document_store.get(
            conversation_doc_id("claude-ai", "conv-1")
        )
        assert "One more follow-up question." in doc["content"]

    def test_identical_content_two_convs_not_treated_as_move(
        self, registry, tmp_path: Path
    ):
        # detect_moves is disabled for conversations: same text, two uuids,
        # both stored — never re-keyed.
        twin_a = {
            "uuid": "twin-a",
            "name": "T",
            "chat_messages": _CONV_OLD["chat_messages"],
        }
        twin_b = {
            "uuid": "twin-b",
            "name": "T",
            "chat_messages": _CONV_OLD["chat_messages"],
        }
        report = sync_conversations(registry, _write_export(tmp_path, [twin_a, twin_b]))
        assert report.counts()["moved"] == 0
        assert report.counts()["ingested"] == 2
        store = registry.knowledge.document_store
        assert store.get(conversation_doc_id("claude-ai", "twin-a")) is not None
        assert store.get(conversation_doc_id("claude-ai", "twin-b")) is not None

    def test_prune_removes_dropped_conversations(self, registry, tmp_path: Path):
        src = _write_export(tmp_path, [_CONV_OLD, _CONV_NEW_SHAPE])
        sync_conversations(registry, src)
        _write_export(tmp_path, [_CONV_OLD])
        report = sync_conversations(registry, src, prune=True)
        assert [p["doc_id"] for p in report.pruned] == [
            conversation_doc_id("claude-ai", "conv-2")
        ]
        store = registry.knowledge.document_store
        assert store.get(conversation_doc_id("claude-ai", "conv-2")) is None
        assert store.get(conversation_doc_id("claude-ai", "conv-1")) is not None

    def test_dropped_conversation_is_kept_without_prune(self, registry, tmp_path: Path):
        src = _write_export(tmp_path, [_CONV_OLD, _CONV_NEW_SHAPE])
        sync_conversations(registry, src)
        _write_export(tmp_path, [_CONV_OLD])
        report = sync_conversations(registry, src)
        assert report.pruned == []
        assert report.prune_withheld == []
        store = registry.knowledge.document_store
        assert store.get(conversation_doc_id("claude-ai", "conv-2")) is not None

    def test_long_conversation_is_chunked_and_retrievable(
        self, registry, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv(EMBED_ON_INGEST_FLAG, "1")
        long_turn = "We discussed violins and bow rosin at length. " * 300
        conv = {
            "uuid": "long-1",
            "name": "Music gear",
            "chat_messages": [
                {"sender": "human", "text": "Tell me about violins."},
                {"sender": "assistant", "text": long_turn.strip()},
            ],
        }
        sync_conversations(registry, _write_export(tmp_path, [conv]))
        parent_id = conversation_doc_id("claude-ai", "long-1")
        parent = registry.knowledge.document_store.get(parent_id)
        assert parent["metadata"]["chunk_count"] >= 2
        hits = registry.knowledge.vector_store.query(_embed("violins rosin"), top_k=1)
        assert hits[0]["item_id"].startswith(f"{parent_id}#chunk-")

    def test_dry_run_writes_nothing(self, registry, tmp_path: Path):
        src = _write_export(tmp_path, [_CONV_OLD])
        report = sync_conversations(registry, src, dry_run=True)
        assert report.counts()["ingested"] == 1
        assert registry.knowledge.document_store.count() == 0

    def test_conversation_stored_under_the_old_shape_still_reads(
        self, registry, tmp_path: Path
    ):
        # A conversation ingested before the content_type → document_form
        # reconciliation, exactly as it sits in the store today. It must stay
        # readable, and the next re-sync must migrate it without touching any
        # other key.
        doc_id = conversation_doc_id("claude-ai", "conv-1")
        legacy_metadata = {
            "conversation_id": "conv-1",
            "title": "Retirement planning",
            "content_type": "conversation",
            "message_count": 2,
            "source_system": "claude-ai",
            "source_path": "Retirement planning",
        }
        registry.knowledge.document_store.put(
            doc_id, "stale body", metadata=legacy_metadata
        )
        stored = registry.knowledge.document_store.get(doc_id)["metadata"]
        assert document_form_of(stored) == "conversation"

        report = sync_conversations(registry, _write_export(tmp_path, [_CONV_OLD]))
        assert report.counts()["updated"] == 1

        metadata = registry.knowledge.document_store.get(doc_id)["metadata"]
        assert metadata["document_form"] == "conversation"
        assert "content_type" not in metadata
        assert metadata["conversation_id"] == "conv-1"
        assert metadata["message_count"] == 2
        assert document_form_of(metadata) == "conversation"

    def test_extract_off_by_default(self, registry, tmp_path: Path):
        # Without the env flag / --extract, no extraction runs and the
        # report shows zero extracted entities.
        report = sync_conversations(registry, _write_export(tmp_path, [_CONV_OLD]))
        assert report.counts()["entities_extracted"] == 0
        assert report.counts()["edges_extracted"] == 0

    def test_speaker_labels_threaded_to_extraction_policy(
        self, registry, tmp_path: Path, monkeypatch
    ):
        # #299: the reader's turn labels reach sync_records so the
        # extraction draft policy can drop person-typed speaker drafts.
        import trellis.ingest_corpus.conversations as conv_mod

        captured: dict = {}

        def _capture(reg, records, **kwargs):
            captured.update(kwargs)
            from trellis.ingest_corpus.models import CorpusSyncReport

            return CorpusSyncReport(
                root="x", source_system="claude-ai", dry_run=False, prune=False
            )

        monkeypatch.setattr(conv_mod, "sync_records", _capture)
        sync_conversations(registry, _write_export(tmp_path, [_CONV_OLD]))
        labels = set(captured["extraction_participant_names"])
        assert labels == {"You", "Claude", "Unknown"}


class TestPruneKeepsARedactedConversation:
    """A conversation prune decides by id alone, and a redacted parent row is
    not one to delete (#1067).

    The blank parent row is what refuses the redacted text when the
    conversation comes back in a later export, so deleting it let the next sync
    write the parent back live.
    """

    @staticmethod
    def _long_conversation(uuid: str) -> dict:
        turn = "We discussed violins and bow rosin at length. " * 300
        return {
            "uuid": uuid,
            "name": "Music gear",
            "chat_messages": [
                {"sender": "human", "text": "Tell me about violins."},
                {"sender": "assistant", "text": turn.strip()},
            ],
        }

    @staticmethod
    def _redact_parent(registry, parent_id: str) -> int:
        """Blank a parent the way a redaction does: no text, the redacted
        lifecycle stamp, and only ``chunk_count`` kept. Returns that count."""
        docs = registry.knowledge.document_store
        count = docs.get(parent_id)["metadata"]["chunk_count"]
        put_document(
            docs,
            registry.knowledge.vector_store,
            parent_id,
            "",
            {**tombstone_metadata(), "chunk_count": count},
            preserve_updated_at=False,
        )
        assert is_redaction_tombstone(docs.get(parent_id))
        return count

    @staticmethod
    def _tombstone_warnings(report) -> list[dict]:
        return [w for w in report.warnings if w["kind"] == "redaction_tombstone"]

    @pytest.fixture
    def redacted(self, registry, tmp_path: Path) -> tuple[Path, str, int, str]:
        """Sync three conversations, redact one parent, then drop it and
        another from the export. Returns the export path, the redacted parent
        id, its chunk count and the plain dropped conversation's id."""
        src = _write_export(
            tmp_path,
            [self._long_conversation("redacted-1"), _CONV_OLD, _CONV_NEW_SHAPE],
        )
        sync_conversations(registry, src)
        parent_id = conversation_doc_id("claude-ai", "redacted-1")
        count = self._redact_parent(registry, parent_id)
        assert count >= 2
        _write_export(tmp_path, [_CONV_OLD])
        return src, parent_id, count, conversation_doc_id("claude-ai", "conv-2")

    def test_prune_keeps_the_tombstoned_parent_and_its_chunks(self, registry, redacted):
        """Defective subject: drop the tombstone guard in ``_prune_vanished`` and
        the parent row is deleted with the rest of the tree."""
        src, parent_id, count, plain_id = redacted
        docs = registry.knowledge.document_store

        report = sync_conversations(registry, src, prune=True)

        assert [p["doc_id"] for p in report.pruned] == [plain_id]
        assert is_redaction_tombstone(docs.get(parent_id))
        for index in range(count):
            assert docs.get(chunk_doc_id(parent_id, index))["content"] != ""
        assert docs.get(plain_id) is None
        assert self._tombstone_warnings(report) == [
            {"kind": "redaction_tombstone", "path": parent_id, "doc_id": parent_id}
        ]

    def test_a_dry_run_predicts_the_same_report_and_deletes_nothing(
        self, registry, redacted
    ):
        """Defective subject: apply the guard only outside the dry-run branch
        and the plan lists the tombstoned parent as pruned and drops the
        warning."""
        src, parent_id, _count, plain_id = redacted
        docs = registry.knowledge.document_store
        rows_before = docs.count()

        report = sync_conversations(registry, src, prune=True, dry_run=True)

        assert [p["doc_id"] for p in report.pruned] == [plain_id]
        assert docs.count() == rows_before
        assert is_redaction_tombstone(docs.get(parent_id))
        assert self._tombstone_warnings(report) == [
            {"kind": "redaction_tombstone", "path": parent_id, "doc_id": parent_id}
        ]
        # The prediction is the real run's report.
        real = sync_conversations(registry, src, prune=True)
        assert real.pruned == report.pruned
        assert self._tombstone_warnings(real) == self._tombstone_warnings(report)

    def test_the_conversation_coming_back_is_not_written_live(
        self, registry, redacted, tmp_path: Path
    ):
        src, parent_id, _count, _plain_id = redacted
        docs = registry.knowledge.document_store
        sync_conversations(registry, src, prune=True)

        _write_export(tmp_path, [self._long_conversation("redacted-1"), _CONV_OLD])
        back = sync_conversations(registry, src, prune=True)

        assert is_redaction_tombstone(docs.get(parent_id))
        assert docs.get(parent_id)["content"] == ""
        # The put guard refuses and warns; it reports the record's own label
        # as the path, so only the id is pinned.
        assert [w["doc_id"] for w in self._tombstone_warnings(back)] == [parent_id]

    def test_a_prune_with_no_tombstone_warns_of_nothing(self, registry, tmp_path):
        src = _write_export(tmp_path, [_CONV_OLD, _CONV_NEW_SHAPE])
        sync_conversations(registry, src)
        _write_export(tmp_path, [_CONV_OLD])

        report = sync_conversations(registry, src, prune=True)

        assert [p["doc_id"] for p in report.pruned] == [
            conversation_doc_id("claude-ai", "conv-2")
        ]
        assert self._tombstone_warnings(report) == []
