"""What "public" means to an ``external`` key, pinned against a hand-written record.

An ``external`` key reads a document only when its stored label is exactly one
record. The store filter and the per-row gate both take that record from
``public_label_record()``, so they cannot disagree with each other, but both
could drift from what is *on disk* together: bump ``classification_version``,
add a ``DataClassification`` field with a default, change what ``classified_by``
defaults to, and ``public_label_record()`` changes with the model while every
stored document keeps the old record. Every external read then answers an empty
200, silently.

``LITERAL_PUBLIC_RECORD`` is written out by hand, not built from the model, so
that drift fails here instead. A failure of the first class is not a test to
loosen. It means documents already labelled ``public`` are about to stop being
served.

WHAT A PERSON HAS TO DO when a test in this file fails:

1. Decide whether the change to ``DataClassification`` is wanted. If it is,
   existing ``public`` documents carry the old record and must be relabelled
   (``trellis curate classify-sensitivity <doc_id> --sensitivity public``, or
   ``POST /api/v1/documents/<doc_id>/sensitivity``), or the stores migrated.
   Plan that before merging.
2. Only then update ``LITERAL_PUBLIC_RECORD`` to the new record, in the same
   change as the migration. Never edit the literal to make the test pass first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from trellis.ingest_corpus.models import chunk_doc_id
from trellis.retrieve.public_rank import public_label_filter, read_public_documents
from trellis.retrieve.sensitivity import DocumentClearance, permits
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    is_public_label,
    public_label_record,
    sensitivity_of,
)
from trellis.stores.sqlite.document import SQLiteDocumentStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from trellis.schemas.classification import Sensitivity

#: The record a document stored by ``classification.set`` to ``public`` carries,
#: written out literally. Do not build it from ``DataClassification``.
LITERAL_PUBLIC_RECORD: dict[str, Any] = {
    "sensitivity": "public",
    "regulatory_tags": [],
    "jurisdiction": [],
    "classified_by": ["caller"],
    "classification_version": "1",
}

_WHAT_TO_DO = (
    "The record an external key serves no longer equals the one stored on "
    "public documents. Do NOT edit LITERAL_PUBLIC_RECORD to make this pass. "
    "Existing public documents carry the old record and will stop being "
    "served by every external read (an empty 200). Decide whether the schema "
    "change is wanted; if so, relabel or migrate the documents "
    "(`trellis curate classify-sensitivity <doc_id> --sensitivity public`), "
    "and only then update the literal in the same change. See the docstring "
    "of tests/unit/retrieve/test_public_label_pin.py."
)

#: Records that read ``public`` and are not the exact one. None is served.
NEAR_MISSES: dict[str, dict[str, Any]] = {
    "bare": {"sensitivity": "public"},
    "imported": {**LITERAL_PUBLIC_RECORD, "classified_by": ["import"]},
    "two-classifiers": {**LITERAL_PUBLIC_RECORD, "classified_by": ["caller", "x"]},
    "jurisdiction": {**LITERAL_PUBLIC_RECORD, "jurisdiction": ["EU"]},
    "tags": {**LITERAL_PUBLIC_RECORD, "regulatory_tags": ["gdpr"]},
    "other-version": {**LITERAL_PUBLIC_RECORD, "classification_version": "2"},
    "extra-field": {**LITERAL_PUBLIC_RECORD, "note": "x"},
    "not-public": {**LITERAL_PUBLIC_RECORD, "sensitivity": "internal"},
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteDocumentStore]:
    s = SQLiteDocumentStore(tmp_path / "docs.db")
    try:
        yield s
    finally:
        s.close()


class TestTheLiteralRecordIsWhatIsServed:
    def test_the_filter_is_the_literal_record(self) -> None:
        assert public_label_filter() == {
            DATA_CLASSIFICATION_KEY: LITERAL_PUBLIC_RECORD
        }, _WHAT_TO_DO

    def test_the_shared_function_is_the_literal_record(self) -> None:
        assert public_label_record() == LITERAL_PUBLIC_RECORD, _WHAT_TO_DO

    def test_the_gate_permits_the_literal_record(
        self, store: SQLiteDocumentStore
    ) -> None:
        gate = DocumentClearance(store, "public")
        assert gate.permits("d", {DATA_CLASSIFICATION_KEY: LITERAL_PUBLIC_RECORD}), (
            _WHAT_TO_DO
        )
        assert is_public_label({DATA_CLASSIFICATION_KEY: LITERAL_PUBLIC_RECORD}), (
            _WHAT_TO_DO
        )

    def test_a_stored_row_with_the_literal_record_is_selected_by_the_store(
        self, store: SQLiteDocumentStore
    ) -> None:
        """The filter, run by the store itself, against a row written literally."""
        store.put(
            "served", "body", {DATA_CLASSIFICATION_KEY: dict(LITERAL_PUBLIC_RECORD)}
        )
        assert [r["doc_id"] for r in read_public_documents(store)] == ["served"], (
            _WHAT_TO_DO
        )


class TestNearMissesAreServedByNoSurface:
    @pytest.mark.parametrize("name", sorted(NEAR_MISSES))
    def test_the_gate_refuses_it(self, store: SQLiteDocumentStore, name: str) -> None:
        gate = DocumentClearance(store, "public")
        assert not gate.permits("d", {DATA_CLASSIFICATION_KEY: NEAR_MISSES[name]})
        assert not is_public_label({DATA_CLASSIFICATION_KEY: NEAR_MISSES[name]})

    @pytest.mark.parametrize("name", sorted(NEAR_MISSES))
    def test_the_store_filter_does_not_select_it(
        self, store: SQLiteDocumentStore, name: str
    ) -> None:
        store.put("near", "body", {DATA_CLASSIFICATION_KEY: NEAR_MISSES[name]})
        assert read_public_documents(store) == []

    @pytest.mark.parametrize("name", sorted(NEAR_MISSES))
    def test_filter_and_gate_agree(self, store: SQLiteDocumentStore, name: str) -> None:
        """One predicate: whatever the store selects, the gate permits, and back."""
        bag = {DATA_CLASSIFICATION_KEY: NEAR_MISSES[name]}
        store.put("near", "body", bag)
        selected = bool(
            store.list_documents(
                limit=10, include_chunks=False, filters=public_label_filter()
            )
        )
        assert selected == DocumentClearance(store, "public").permits("near", bag)

    def test_an_unlabelled_row_a_missing_row_and_a_chunk_are_not_served(
        self, store: SQLiteDocumentStore
    ) -> None:
        gate = DocumentClearance(store, "public")
        assert not gate.permits("d", {})
        assert not gate.permits("d", None)
        # A chunk is judged as the whole document it belongs to is, and a
        # public-only caller is served no chunk at all, even under the exact
        # record on its own row.
        assert not gate.permits(
            chunk_doc_id("parent", 0), {DATA_CLASSIFICATION_KEY: LITERAL_PUBLIC_RECORD}
        )


class TestAnInternalClearanceIsUnchanged:
    """The exact-record test applies to a public-only clearance and no other."""

    @pytest.mark.parametrize("clearance", ["internal", "confidential", "restricted"])
    @pytest.mark.parametrize("name", sorted(NEAR_MISSES))
    def test_a_clearance_above_public_reads_the_sensitivity_value(
        self, store: SQLiteDocumentStore, clearance: Sensitivity, name: str
    ) -> None:
        bag = {DATA_CLASSIFICATION_KEY: NEAR_MISSES[name]}
        legacy = (
            True
            if clearance == "restricted"
            else permits(clearance, sensitivity_of(bag))
        )
        assert DocumentClearance(store, clearance).permits("d", bag) is legacy

    def test_a_bare_public_record_is_readable_by_an_internal_clearance(
        self, store: SQLiteDocumentStore
    ) -> None:
        """The distinction the exact-record test draws, stated outright."""
        bag = {DATA_CLASSIFICATION_KEY: {"sensitivity": "public"}}
        assert DocumentClearance(store, "internal").permits("d", bag)
        assert not DocumentClearance(store, "public").permits("d", bag)
