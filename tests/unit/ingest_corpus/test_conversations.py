"""Claude conversation-export reader + sync tests."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.ingest_corpus.conversations import (
    conversation_doc_id,
    read_claude_export,
    sync_conversations,
)
from trellis.retrieve.embed_ingest_hook import EMBED_ON_INGEST_FLAG
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
        assert rec.handler_metadata["content_type"] == "conversation"
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
        assert doc["metadata"]["content_type"] == "conversation"

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
