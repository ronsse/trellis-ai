"""The pointer-key rule itself (#1057): strict, restore form, shape edges."""

from __future__ import annotations

import pytest

from trellis.mutate.commands import Command, Operation
from trellis.mutate.evidence_ingest import (
    build_evidence_ingest_command_from_args,
)
from trellis.mutate.evidence_pointer_keys import (
    DOCUMENT_POINTER_REFUSALS,
    LIFECYCLE_RESTORE_NOTE,
    evidence_ingest_pointer_refusal,
    pointer_key_refusal,
)
from trellis.schemas.classification import Lifecycle
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

    @pytest.mark.parametrize(
        "record",
        [
            {"state": "current", "valid_until": None},
            {"state": "current", "superseded_by": None},
            {"state": "current", "deprecation_reason": None},
            {"state": "current", "classification_version": "1"},
            Lifecycle().model_dump(mode="json"),
        ],
        ids=[
            "null-valid-until",
            "null-superseded-by",
            "null-deprecation-reason",
            "default-classification-version",
            "schema-default-dump",
        ],
    )
    def test_current_with_every_other_field_at_its_default_is_accepted(self, record):
        """The record ``Lifecycle()`` dumps is the restore form too."""
        assert pointer_key_refusal({"lifecycle": record}) is None

    @pytest.mark.parametrize(
        "record",
        [
            {"state": "current", "deprecation_reason": "restored"},
            {"state": "current", "deprecation_reason": "redacted"},
            {"state": "current", "valid_from": "2020-01-01T00:00:00+00:00"},
            {"state": "current", "classification_version": 2},
            {"state": "current", "valid_until": "2020-01-01T00:00:00+00:00"},
            {"state": "current", "superseded_by": "doc-x"},
            {"state": "current", "unknown_key": None},
        ],
        ids=[
            "deprecation-reason",
            "redaction-reason",
            "valid-from",
            "classification-version",
            "valid-until",
            "superseded-by",
            "unknown-key",
        ],
    )
    def test_only_the_exact_record_is_accepted(self, record):
        """A set value in any other field is refused, an unknown key too."""
        refusal = pointer_key_refusal({"lifecycle": record})
        assert refusal is not None
        assert refusal.key == "lifecycle"

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


def _command(args, *, target_id=None, idempotency_key=None):
    """The command a door builds from ``args``, as the doors build it."""
    return build_evidence_ingest_command_from_args(
        args,
        requested_by="tester",
        target_id=target_id,
        idempotency_key=idempotency_key,
    )


def _refusal(documents, command):
    return evidence_ingest_pointer_refusal(
        command, document_store=documents, clearance="restricted"
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
        assert _refusal(documents, _command(args)) is None

    def test_a_command_whose_evidence_is_not_an_object_is_not_inspected(
        self, documents
    ):
        command = Command(
            operation=Operation.EVIDENCE_INGEST,
            args={"evidence": "text"},
            requested_by="tester",
        )
        assert _refusal(documents, command) is None


REF_ARGS = {"evidence": {"content": "c", "metadata": {"evidence_ref": "a"}}}


class TestTheCommandsOwnDocumentIsJudged:
    """The id read is the one the builder resolved, never a second derivation."""

    def test_a_missing_doc_id_falls_back_to_the_target_id(self, documents):
        documents.put("doc", "body", {"evidence_ref": "a"})
        assert _refusal(documents, _command(REF_ARGS, target_id="doc")) is None
        unknown = _refusal(documents, _command(REF_ARGS, target_id="other"))
        assert unknown is not None
        assert unknown.key == "evidence_ref"

    def test_the_doc_id_wins_over_the_target_id(self, documents):
        """The ref sits on the target id only, and the write lands on the doc id."""
        documents.put("doc-a", "body", {"evidence_ref": "a"})
        args = {
            "evidence": {
                "doc_id": "doc-x",
                "content": "c",
                "metadata": {"evidence_ref": "a"},
            }
        }
        command = _command(args, target_id="doc-a")
        assert command.args["evidence"]["doc_id"] == "doc-x"
        refusal = _refusal(documents, command)
        assert refusal is not None
        assert refusal.key == "evidence_ref"

    def test_an_idempotency_key_alone_is_judged_at_the_derived_id(self, documents):
        command = _command(REF_ARGS, idempotency_key="run-1")
        derived = command.args["evidence"]["doc_id"]
        assert derived.startswith("evidence:")
        assert _refusal(documents, command) is not None
        documents.put(derived, "body", {"evidence_ref": "a"})
        assert _refusal(documents, command) is None

    def test_no_id_at_all_means_a_new_document(self, documents):
        refusal = _refusal(documents, _command(REF_ARGS))
        assert refusal is not None
