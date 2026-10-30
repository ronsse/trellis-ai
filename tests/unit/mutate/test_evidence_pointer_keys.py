"""The pointer-key rule itself (#1057): strict, restore form, shape edges."""

from __future__ import annotations

import pytest

from trellis.mutate.evidence_pointer_keys import (
    DOCUMENT_POINTER_REFUSALS,
    LIFECYCLE_RESTORE_NOTE,
    evidence_ingest_pointer_refusal,
    pointer_key_refusal,
)
from trellis.stores.sqlite.document import SQLiteDocumentStore


@pytest.fixture
def documents(tmp_path):
    store = SQLiteDocumentStore(tmp_path / "documents.db")
    yield store
    store.close()


class TestStrict:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("lifecycle", {"state": "current"}),
            ("lifecycle", {"state": "superseded", "superseded_by": "x"}),
            ("evidence_ref", "doc"),
            ("evidence_ref", None),
        ],
    )
    def test_any_value_is_refused_with_the_unchanged_words(self, key, value):
        refusal = pointer_key_refusal({key: value}, strict=True)
        assert refusal is not None
        assert refusal.key == key
        assert refusal.message == DOCUMENT_POINTER_REFUSALS[key]
        assert LIFECYCLE_RESTORE_NOTE not in refusal.message

    def test_no_pointer_key_is_no_refusal(self):
        assert pointer_key_refusal({"title": "t"}, strict=True) is None


class TestRestoreForm:
    def test_the_lifecycle_note_extends_the_strict_message(self):
        refusal = pointer_key_refusal({"lifecycle": {"state": "archived"}})
        assert refusal is not None
        assert refusal.message == (
            DOCUMENT_POINTER_REFUSALS["lifecycle"] + LIFECYCLE_RESTORE_NOTE
        )

    def test_current_is_accepted(self):
        assert pointer_key_refusal({"lifecycle": {"state": "current"}}) is None

    def test_current_with_a_deprecation_reason_is_accepted(self):
        record = {"state": "current", "deprecation_reason": "restored"}
        assert pointer_key_refusal({"lifecycle": record}) is None

    def test_the_first_offending_key_is_the_one_named(self):
        refusal = pointer_key_refusal(
            {"evidence_ref": "doc", "lifecycle": {"state": "archived"}},
            stored_metadata={"evidence_ref": "doc"},
        )
        assert refusal is not None
        assert refusal.key == "lifecycle"

    def test_an_evidence_ref_needs_a_stored_row_with_the_same_value(self):
        assert pointer_key_refusal({"evidence_ref": "a"}) is not None
        assert (
            pointer_key_refusal({"evidence_ref": "a"}, stored_metadata={}) is not None
        )
        same = {"evidence_ref": "a"}
        assert pointer_key_refusal(same, stored_metadata=same) is None
        other = {"evidence_ref": "b"}
        assert pointer_key_refusal(same, stored_metadata=other) is not None

    def test_a_stored_none_does_not_match_an_absent_key(self):
        """``None`` sent against a row without the key is a new key, not a match."""
        assert (
            pointer_key_refusal({"evidence_ref": None}, stored_metadata={}) is not None
        )


class TestEvidenceIngestShape:
    """A shape the handler refuses is not this rule's to refuse."""

    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"evidence": "text"},
            {"evidence": {"content": "c"}},
            {"evidence": {"content": "c", "metadata": "oops"}},
            {"evidence": {"content": "c", "metadata": {"title": "t"}}},
        ],
    )
    def test_no_refusal(self, documents, args):
        assert (
            evidence_ingest_pointer_refusal(
                args, target_id=None, document_store=documents, clearance="restricted"
            )
            is None
        )

    def test_a_missing_doc_id_falls_back_to_the_target_id(self, documents):
        documents.put("doc", "body", {"evidence_ref": "a"})
        args = {"evidence": {"content": "c", "metadata": {"evidence_ref": "a"}}}
        kept = evidence_ingest_pointer_refusal(
            args, target_id="doc", document_store=documents, clearance="restricted"
        )
        assert kept is None
        unknown = evidence_ingest_pointer_refusal(
            args, target_id="other", document_store=documents, clearance="restricted"
        )
        assert unknown is not None
        assert unknown.key == "evidence_ref"

    def test_no_id_at_all_means_a_new_document(self, documents):
        args = {"evidence": {"content": "c", "metadata": {"evidence_ref": "a"}}}
        refusal = evidence_ingest_pointer_refusal(
            args, target_id=None, document_store=documents, clearance="restricted"
        )
        assert refusal is not None
