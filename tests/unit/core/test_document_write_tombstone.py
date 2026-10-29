"""A write over a redaction tombstone must be explicit (#1034).

The seam (``put_document``) is where the refusal lives, so every document
writer inherits it; the handler, the builders and the command bytes are pinned
here too. Surface tests (CLI, MCP, REST, corpus sync) live beside their
surfaces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.redaction_tombstone import ORIGINAL_TEXT, make_tombstone, tombstone_metadata
from trellis.core.document_write import (
    REINGEST_STAMP_KEY,
    STAMP_RESERVED_CODE,
    TOMBSTONE_OVERWRITE_CODE,
    RedactionTombstoneError,
    is_redaction_tombstone,
    put_document,
)
from trellis.errors import ValidationError
from trellis.mutate import CommandStatus, build_curate_executor
from trellis.mutate import handlers as handlers_module
from trellis.mutate.evidence_ingest import (
    build_evidence_ingest_command,
    build_evidence_ingest_command_from_args,
)
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

DOC_ID = "doc-tombstoned"
NEW_TEXT = "Quartz harbor rewritten text"


@pytest.fixture
def document_store(tmp_path: Path):
    store = SQLiteDocumentStore(tmp_path / "docs.db")
    yield store
    store.close()


@pytest.fixture
def vector_store(tmp_path: Path):
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    yield store
    store.close()


@pytest.fixture
def tombstoned(document_store, vector_store):
    make_tombstone(document_store, vector_store, DOC_ID)
    return document_store


def _write(document_store, vector_store, content: str, metadata=None, **kw: Any):
    return put_document(
        document_store,
        vector_store,
        DOC_ID,
        content,
        metadata if metadata is not None else {},
        preserve_updated_at=False,
        **kw,
    )


class TestSeamRefusal:
    def test_an_unflagged_content_write_is_refused_and_the_row_stays_blank(
        self, tombstoned, vector_store
    ) -> None:
        with pytest.raises(RedactionTombstoneError) as caught:
            _write(tombstoned, vector_store, NEW_TEXT)

        assert caught.value.code == TOMBSTONE_OVERWRITE_CODE
        assert isinstance(caught.value, ValidationError)
        assert is_redaction_tombstone(tombstoned.get(DOC_ID))
        assert tombstoned.get(DOC_ID)["content"] == ""
        assert vector_store.get(DOC_ID) is None

    def test_the_message_names_the_flag_and_not_the_id_or_the_text(
        self, tombstoned, vector_store
    ) -> None:
        with pytest.raises(RedactionTombstoneError) as caught:
            _write(tombstoned, vector_store, NEW_TEXT)

        message = str(caught.value)
        assert "--reingest-redacted" in message
        assert "execute_mutation" in message
        assert DOC_ID not in message
        assert ORIGINAL_TEXT not in message

    def test_a_flagged_write_succeeds_and_stamps_the_row(
        self, tombstoned, vector_store
    ) -> None:
        before = datetime.now(UTC)
        _write(tombstoned, vector_store, NEW_TEXT, reingest_redacted=True)
        after = datetime.now(UTC)

        row = tombstoned.get(DOC_ID)
        assert row["content"] == NEW_TEXT
        assert not is_redaction_tombstone(row)
        stamp = datetime.fromisoformat(row["metadata"][REINGEST_STAMP_KEY])
        assert before <= stamp <= after

    def test_the_stamp_is_not_carried_to_the_next_write(
        self, tombstoned, vector_store
    ) -> None:
        _write(tombstoned, vector_store, NEW_TEXT, reingest_redacted=True)

        _write(tombstoned, vector_store, "Second plain write")

        assert REINGEST_STAMP_KEY not in tombstoned.get(DOC_ID)["metadata"]

    def test_a_flag_on_a_live_row_stamps_nothing(
        self, document_store, vector_store
    ) -> None:
        _write(document_store, vector_store, "live text")
        _write(document_store, vector_store, NEW_TEXT, reingest_redacted=True)

        assert REINGEST_STAMP_KEY not in document_store.get(DOC_ID)["metadata"]

    def test_a_flag_on_a_missing_row_stamps_nothing(
        self, document_store, vector_store
    ) -> None:
        _write(document_store, vector_store, NEW_TEXT, reingest_redacted=True)

        assert REINGEST_STAMP_KEY not in document_store.get(DOC_ID)["metadata"]


class TestTheTombstoneReadFailsClosed:
    def test_a_failed_read_of_the_stored_row_stops_the_write(
        self, vector_store
    ) -> None:
        """A write that cannot tell whether the id is a tombstone is not made."""
        written: list[str] = []

        class _UnreadableDocumentStore:
            def get(self, doc_id: str) -> None:
                msg = "document backend unreachable"
                raise RuntimeError(msg)

            def put(self, doc_id: str, *args: Any, **kwargs: Any) -> str:
                written.append(doc_id)
                return doc_id

        with pytest.raises(RuntimeError, match="unreachable"):
            put_document(
                _UnreadableDocumentStore(),  # type: ignore[arg-type]
                vector_store,
                DOC_ID,
                NEW_TEXT,
                {},
                preserve_updated_at=False,
            )

        assert written == []


class TestBlankWritesStillPass:
    def test_a_second_redaction_over_a_tombstone_passes(
        self, tombstoned, vector_store
    ) -> None:
        _write(tombstoned, vector_store, "", tombstone_metadata())

        assert is_redaction_tombstone(tombstoned.get(DOC_ID))

    def test_a_metadata_only_write_with_blank_content_passes(
        self, tombstoned, vector_store
    ) -> None:
        metadata = tombstone_metadata()
        metadata["note"] = "kept"

        _write(tombstoned, vector_store, "", metadata)

        row = tombstoned.get(DOC_ID)
        assert row["metadata"]["note"] == "kept"
        assert is_redaction_tombstone(row)


class TestStampIsReserved:
    def test_a_forged_stamp_on_a_new_row_is_refused(
        self, document_store, vector_store
    ) -> None:
        forged = {REINGEST_STAMP_KEY: datetime.now(UTC).isoformat()}

        with pytest.raises(ValidationError) as caught:
            _write(document_store, vector_store, NEW_TEXT, forged)

        assert caught.value.code == STAMP_RESERVED_CODE
        assert document_store.get(DOC_ID) is None

    def test_a_forged_stamp_over_a_tombstone_is_refused_even_when_flagged(
        self, tombstoned, vector_store
    ) -> None:
        forged = {REINGEST_STAMP_KEY: "2999-01-01T00:00:00+00:00"}

        with pytest.raises(ValidationError) as caught:
            _write(tombstoned, vector_store, NEW_TEXT, forged, reingest_redacted=True)

        assert caught.value.code == STAMP_RESERVED_CODE
        assert is_redaction_tombstone(tombstoned.get(DOC_ID))

    def test_a_bag_that_repeats_the_rows_own_stamp_passes(
        self, tombstoned, vector_store
    ) -> None:
        _write(tombstoned, vector_store, NEW_TEXT, reingest_redacted=True)
        stamp = tombstoned.get(DOC_ID)["metadata"][REINGEST_STAMP_KEY]

        _write(tombstoned, vector_store, "edited", {REINGEST_STAMP_KEY: stamp})

        assert tombstoned.get(DOC_ID)["metadata"][REINGEST_STAMP_KEY] == stamp

    def test_a_bag_with_a_different_stamp_than_the_rows_is_refused(
        self, tombstoned, vector_store
    ) -> None:
        _write(tombstoned, vector_store, NEW_TEXT, reingest_redacted=True)

        with pytest.raises(ValidationError) as caught:
            _write(
                tombstoned,
                vector_store,
                "edited",
                {REINGEST_STAMP_KEY: "2999-01-01T00:00:00+00:00"},
            )

        assert caught.value.code == STAMP_RESERVED_CODE


def test_handlers_still_re_exports_the_tombstone_predicate() -> None:
    assert handlers_module.is_redaction_tombstone is is_redaction_tombstone


def test_a_tombstone_needs_both_halves() -> None:
    blank_live = {"content": "", "metadata": {}}
    archived_other = {
        "content": "",
        "metadata": {
            "lifecycle": {"state": "archived", "deprecation_reason": "superseded"}
        },
    }
    text_with_stamp = {"content": "text", "metadata": tombstone_metadata()}

    assert not is_redaction_tombstone(blank_live)
    assert not is_redaction_tombstone(archived_other)
    assert not is_redaction_tombstone(text_with_stamp)


class TestHandler:
    @pytest.fixture
    def registry(self, tmp_path: Path) -> StoreRegistry:
        return StoreRegistry(stores_dir=tmp_path / "stores")

    @pytest.fixture
    def executor(self, registry):
        return build_curate_executor(registry, clearance="restricted")

    @pytest.fixture
    def tombstone(self, registry) -> None:
        make_tombstone(
            registry.knowledge.document_store,
            registry.knowledge.vector_store,
            DOC_ID,
        )

    def _command(self, **kw: Any):
        return build_evidence_ingest_command(
            content=NEW_TEXT, requested_by="tester", doc_id=DOC_ID, **kw
        )

    def test_an_unflagged_command_is_rejected_and_the_row_stays_a_tombstone(
        self, registry, executor, tombstone
    ) -> None:
        result = executor.execute(self._command())

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == TOMBSTONE_OVERWRITE_CODE
        assert "--reingest-redacted" in result.message
        assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC_ID))

    def test_a_flagged_command_succeeds_and_stamps(
        self, registry, executor, tombstone
    ) -> None:
        result = executor.execute(self._command(reingest_redacted=True))

        assert result.status == CommandStatus.SUCCESS, result.message
        row = registry.knowledge.document_store.get(DOC_ID)
        assert row["content"] == NEW_TEXT
        assert REINGEST_STAMP_KEY in row["metadata"]

    def test_a_forged_stamp_in_the_bag_is_rejected(self, registry, executor) -> None:
        result = executor.execute(
            self._command(metadata={REINGEST_STAMP_KEY: "2999-01-01T00:00:00+00:00"})
        )

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == STAMP_RESERVED_CODE
        assert registry.knowledge.document_store.get(DOC_ID) is None

    def test_a_non_boolean_flag_is_rejected(
        self, registry, executor, tombstone
    ) -> None:
        command = self._command(reingest_redacted="yes")  # type: ignore[arg-type]

        result = executor.execute(command)

        assert result.status == CommandStatus.REJECTED
        assert result.metadata["rejection_reason"] == "evidence_ingest_shape"
        assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC_ID))

    def test_a_live_row_takes_an_ordinary_write(self, registry, executor) -> None:
        assert executor.execute(self._command()).status == CommandStatus.SUCCESS
        assert executor.execute(self._command()).status in {
            CommandStatus.SUCCESS,
            CommandStatus.DUPLICATE,
        }


# Computed from origin/main's builder before #1034 existed.
GOLDEN_KEY = (
    "evidence.ingest:c258347daacc603864209e739e127ccf4848adce02769a00071f5e4d628ca0b0"
)
GOLDEN_EVIDENCE = {
    "content": "Plain body text",
    "created_at": "2026-01-01T00:00:00+00:00",
    "doc_id": "doc-fixed",
    "embed_mode": "soft",
    "metadata": {"k": "v"},
    "preserve_updated_at": False,
    "uri": "file:///x",
}


class TestCommandBytesAreUnchangedWithoutTheFlag:
    def _build(self, **kw: Any):
        return build_evidence_ingest_command(
            content="Plain body text",
            requested_by="tester",
            doc_id="doc-fixed",
            metadata={"k": "v"},
            uri="file:///x",
            created_at="2026-01-01T00:00:00+00:00",
            **kw,
        )

    def test_the_default_command_matches_the_pre_flag_bytes_and_key(self) -> None:
        command = self._build()

        assert command.idempotency_key == GOLDEN_KEY
        assert command.args == {"evidence": GOLDEN_EVIDENCE}

    def test_an_explicit_false_is_the_default(self) -> None:
        command = self._build(reingest_redacted=False)

        assert command.idempotency_key == GOLDEN_KEY
        assert "reingest_redacted" not in command.args["evidence"]

    def test_the_flag_changes_both_the_bytes_and_the_key(self) -> None:
        command = self._build(reingest_redacted=True)

        assert command.args["evidence"]["reingest_redacted"] is True
        assert command.idempotency_key != GOLDEN_KEY

    def test_the_from_args_builder_carries_the_flag_and_defaults_off(self) -> None:
        plain = build_evidence_ingest_command_from_args(
            {"evidence": dict(GOLDEN_EVIDENCE)}, requested_by="tester"
        )
        flagged = build_evidence_ingest_command_from_args(
            {"evidence": {**GOLDEN_EVIDENCE, "reingest_redacted": True}},
            requested_by="tester",
        )

        assert plain.idempotency_key == GOLDEN_KEY
        assert "reingest_redacted" not in plain.args["evidence"]
        assert flagged.args["evidence"]["reingest_redacted"] is True
        assert flagged.idempotency_key != GOLDEN_KEY
