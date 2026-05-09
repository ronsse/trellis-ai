"""DocumentStore contract test suite — runs against every backend.

Mirrors the shape of :mod:`graph_store_contract`. This base class
defines the shared semantics that every ``DocumentStore`` backend must
honour. Backend-specific test files (``test_sqlite_document_contract``,
``test_postgres_document_contract``) subclass
:class:`DocumentStoreContractTests` and provide a ``store`` fixture.

The harness deliberately:

* Does **not** test backend-specific schema / index / FTS-tokenizer
  behaviour — those tests live in the per-backend
  ``tests/unit/stores/test_document_store.py`` (SQLite-only) and stay
  where they are. The contract suite is *additive*.
* Uses only the public ``DocumentStore`` ABC surface — no
  ``_conn`` / ``_pool`` access. If a contract assertion needs
  something the ABC doesn't expose, the ABC needs the missing
  method, not the harness.
* Tests overwrite (last-write-wins) semantics on ``put`` because both
  reference implementations use ``ON CONFLICT … DO UPDATE``. The ABC's
  prose docstring ("Store or update a document") is consistent with
  this. There is no SCD-2 / versioning contract here — by design.

Subclass shape::

    class TestSQLiteDocumentContract(DocumentStoreContractTests):
        @pytest.fixture
        def store(self, tmp_path):
            store = SQLiteDocumentStore(tmp_path / "docs.db")
            yield store
            store.close()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore


class DocumentStoreContractTests:
    """Contract tests every ``DocumentStore`` backend must pass.

    Subclasses must provide a pytest fixture named ``store`` that
    yields a fresh, empty
    :class:`~trellis.stores.base.document.DocumentStore` instance and
    tears it down afterwards.
    """

    # ------------------------------------------------------------------
    # put / get — basic CRUD
    # ------------------------------------------------------------------

    def test_put_returns_id_when_id_omitted(self, store: DocumentStore) -> None:
        did = store.put(None, "hello world")
        assert isinstance(did, str)
        assert did

    def test_put_uses_explicit_id_when_provided(self, store: DocumentStore) -> None:
        did = store.put("explicit_id", "hello")
        assert did == "explicit_id"

    def test_get_round_trip_preserves_all_fields(self, store: DocumentStore) -> None:
        store.put("d1", "the content", {"tag": "test", "domain": "platform"})
        doc = store.get("d1")
        assert doc is not None
        assert doc["doc_id"] == "d1"
        assert doc["content"] == "the content"
        assert doc["metadata"] == {"tag": "test", "domain": "platform"}
        # Hash + timestamps are populated by the backend, not the caller.
        assert doc["content_hash"]
        assert doc["created_at"]
        assert doc["updated_at"]

    def test_get_returns_none_for_missing(self, store: DocumentStore) -> None:
        assert store.get("does_not_exist") is None

    def test_put_with_no_metadata_yields_empty_dict(
        self, store: DocumentStore
    ) -> None:
        store.put("d1", "content")
        doc = store.get("d1")
        assert doc is not None
        assert doc["metadata"] == {}

    # ------------------------------------------------------------------
    # Idempotency / overwrite — last-write-wins on put
    # ------------------------------------------------------------------

    def test_repeated_put_same_id_overwrites_content(
        self, store: DocumentStore
    ) -> None:
        store.put("d1", "v1")
        store.put("d1", "v2")
        doc = store.get("d1")
        assert doc is not None
        assert doc["content"] == "v2"

    def test_repeated_put_same_id_overwrites_metadata(
        self, store: DocumentStore
    ) -> None:
        store.put("d1", "v1", {"a": 1})
        store.put("d1", "v1", {"b": 2})
        doc = store.get("d1")
        assert doc is not None
        # New metadata fully replaces old (no merge).
        assert doc["metadata"] == {"b": 2}

    def test_repeated_put_same_id_keeps_count_at_one(
        self, store: DocumentStore
    ) -> None:
        store.put("d1", "v1")
        store.put("d1", "v2")
        store.put("d1", "v3")
        assert store.count() == 1

    def test_repeated_put_identical_content_is_idempotent(
        self, store: DocumentStore
    ) -> None:
        """Putting the same (id, content, metadata) twice still yields one row
        with identical content_hash — the canonical idempotency case."""
        store.put("d1", "stable content", {"k": "v"})
        first = store.get("d1")
        store.put("d1", "stable content", {"k": "v"})
        second = store.get("d1")
        assert first is not None
        assert second is not None
        assert first["content_hash"] == second["content_hash"]
        assert store.count() == 1

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def test_delete_returns_true_when_existed(self, store: DocumentStore) -> None:
        store.put("d1", "content")
        assert store.delete("d1") is True
        assert store.get("d1") is None

    def test_delete_returns_false_for_missing(self, store: DocumentStore) -> None:
        assert store.delete("ghost") is False

    def test_delete_then_put_recreates_document(self, store: DocumentStore) -> None:
        store.put("d1", "first")
        store.delete("d1")
        store.put("d1", "second")
        doc = store.get("d1")
        assert doc is not None
        assert doc["content"] == "second"

    # ------------------------------------------------------------------
    # Metadata round-trip — mixed types
    # ------------------------------------------------------------------

    def test_metadata_round_trip_mixed_types(self, store: DocumentStore) -> None:
        metadata = {
            "string_field": "platform",
            "int_field": 42,
            "float_field": 3.14,
            "bool_field": True,
            "list_field": ["a", "b", "c"],
            "dict_field": {"nested_key": "nested_value", "n": 7},
        }
        store.put("d1", "content", metadata)
        doc = store.get("d1")
        assert doc is not None
        assert doc["metadata"] == metadata

    def test_metadata_round_trip_nested_content_tags(
        self, store: DocumentStore
    ) -> None:
        """Mirrors the shape ``ClassifierPipeline`` writes — a nested
        ``content_tags`` dict with a list facet (``domain``) and scalar
        facets (``content_type``, ``signal_quality``, ``scope``)."""
        metadata = {
            "content_tags": {
                "domain": ["data-pipeline", "infrastructure"],
                "content_type": "pattern",
                "scope": "team",
                "signal_quality": "high",
            },
        }
        store.put("d1", "content", metadata)
        doc = store.get("d1")
        assert doc is not None
        assert doc["metadata"] == metadata

    # ------------------------------------------------------------------
    # list_documents
    # ------------------------------------------------------------------

    def test_list_documents_empty_store_returns_empty_list(
        self, store: DocumentStore
    ) -> None:
        assert store.list_documents() == []

    def test_list_documents_returns_all_when_under_limit(
        self, store: DocumentStore
    ) -> None:
        store.put("a", "x")
        store.put("b", "y")
        store.put("c", "z")
        docs = store.list_documents(limit=50)
        assert {d["doc_id"] for d in docs} == {"a", "b", "c"}

    def test_list_documents_respects_limit(self, store: DocumentStore) -> None:
        for i in range(5):
            store.put(f"d{i}", f"content {i}")
        docs = store.list_documents(limit=3)
        assert len(docs) == 3

    def test_list_documents_respects_offset(self, store: DocumentStore) -> None:
        for i in range(5):
            store.put(f"d{i}", f"content {i}")
        first_page = store.list_documents(limit=2, offset=0)
        second_page = store.list_documents(limit=2, offset=2)
        assert len(first_page) == 2
        assert len(second_page) == 2
        # Pages must not overlap — offset moves the window.
        first_ids = {d["doc_id"] for d in first_page}
        second_ids = {d["doc_id"] for d in second_page}
        assert first_ids.isdisjoint(second_ids)

    # ------------------------------------------------------------------
    # count
    # ------------------------------------------------------------------

    def test_count_empty_store_is_zero(self, store: DocumentStore) -> None:
        assert store.count() == 0

    def test_count_increments_on_put(self, store: DocumentStore) -> None:
        store.put("a", "x")
        store.put("b", "y")
        assert store.count() == 2

    def test_count_decrements_on_delete(self, store: DocumentStore) -> None:
        store.put("a", "x")
        store.put("b", "y")
        store.delete("a")
        assert store.count() == 1

    # ------------------------------------------------------------------
    # get_by_hash — content-addressed dedup lookup
    # ------------------------------------------------------------------

    def test_get_by_hash_round_trip(self, store: DocumentStore) -> None:
        store.put("d1", "unique content")
        doc = store.get("d1")
        assert doc is not None
        chash = doc["content_hash"]
        assert chash
        found = store.get_by_hash(chash)
        assert found is not None
        assert found["doc_id"] == "d1"
        assert found["content"] == "unique content"

    def test_get_by_hash_returns_none_for_missing(
        self, store: DocumentStore
    ) -> None:
        assert store.get_by_hash("nonexistent_hash") is None

    def test_get_by_hash_after_overwrite_uses_new_content(
        self, store: DocumentStore
    ) -> None:
        """Overwriting changes the content_hash; the old hash no longer
        resolves and the new hash does."""
        store.put("d1", "first content")
        old_doc = store.get("d1")
        assert old_doc is not None
        old_hash = old_doc["content_hash"]

        store.put("d1", "second content")
        new_doc = store.get("d1")
        assert new_doc is not None
        new_hash = new_doc["content_hash"]
        assert new_hash != old_hash

        assert store.get_by_hash(old_hash) is None
        found = store.get_by_hash(new_hash)
        assert found is not None
        assert found["content"] == "second content"

    # ------------------------------------------------------------------
    # search — minimal contract (per-backend tokenizers tested elsewhere)
    # ------------------------------------------------------------------

    def test_search_empty_query_returns_empty_list(
        self, store: DocumentStore
    ) -> None:
        store.put("d1", "indexed content")
        assert store.search("") == []

    def test_search_empty_store_returns_empty_list(
        self, store: DocumentStore
    ) -> None:
        assert store.search("anything") == []

    def test_search_results_carry_rank(self, store: DocumentStore) -> None:
        store.put("d1", "python programming language")
        results = store.search("python")
        assert len(results) >= 1
        # Per ABC: search results "with a rank key".
        assert "rank" in results[0]
