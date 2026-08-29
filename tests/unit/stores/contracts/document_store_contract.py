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

from trellis.schemas.classification import LIST_FACETS

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

    def test_put_with_no_metadata_yields_empty_dict(self, store: DocumentStore) -> None:
        store.put("d1", "content")
        doc = store.get("d1")
        assert doc is not None
        assert doc["metadata"] == {}

    # ------------------------------------------------------------------
    # updated_at semantics — the recency-decay input
    # ------------------------------------------------------------------

    def test_put_bumps_updated_at_by_default(self, store: DocumentStore) -> None:
        """A plain re-put marks the row modified. The default must not change.

        ``updated_at`` is what ``retrieve.strategies.KeywordSearch`` feeds to
        its recency decay, so this is a retrieval-visible property, not
        bookkeeping.
        """
        store.put("d1", "v1", {"a": 1})
        first = store.get("d1")
        assert first is not None
        store.put("d1", "v2", {"a": 2})
        second = store.get("d1")
        assert second is not None
        assert str(second["updated_at"]) > str(first["updated_at"])

    def test_put_preserve_updated_at_keeps_prior_stamp(
        self, store: DocumentStore
    ) -> None:
        """``preserve_updated_at`` lets a writer attach derived metadata
        without re-ranking the document.

        Without it, a whole-corpus pass that only adds derived metadata (the
        shadow-tagging pass in ``trellis.classify.shadow``) stamps every row
        with the same fresh timestamp and flattens recency ordering across the
        entire store.
        """
        store.put("d1", "v1", {"a": 1})
        original = store.get("d1")
        assert original is not None

        store.put("d1", "v1", {"a": 1, "derived": "x"}, preserve_updated_at=True)
        after = store.get("d1")
        assert after is not None
        assert str(after["updated_at"]) == str(original["updated_at"])
        # The write still landed — this is a preserved stamp, not a no-op.
        assert after["metadata"] == {"a": 1, "derived": "x"}
        assert str(after["created_at"]) == str(original["created_at"])

    def test_preserve_updated_at_still_stamps_a_new_row(
        self, store: DocumentStore
    ) -> None:
        """On insert there is no prior stamp to preserve, so one is minted."""
        store.put("fresh", "content", {}, preserve_updated_at=True)
        doc = store.get("fresh")
        assert doc is not None
        assert doc["updated_at"]
        assert doc["created_at"]

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
    # include_chunks — chunk documents are excluded on request (#385)
    # ------------------------------------------------------------------

    @staticmethod
    def _seed_chunked_corpus(store: DocumentStore, parents: int, per_parent: int):
        """``parents`` parent docs, each followed by ``per_parent`` chunks."""
        parent_ids = []
        for p in range(parents):
            parent_id = f"corpus:notes:doc{p}"
            store.put(parent_id, f"parent {p} searchable body")
            parent_ids.append(parent_id)
            for c in range(per_parent):
                store.put(
                    f"{parent_id}#chunk-{c}",
                    f"parent {p} searchable body slice {c}",
                    {"parent_doc_id": parent_id, "chunk_index": c},
                )
        return parent_ids

    def test_list_documents_includes_chunks_by_default(
        self, store: DocumentStore
    ) -> None:
        self._seed_chunked_corpus(store, parents=2, per_parent=2)
        ids = {d["doc_id"] for d in store.list_documents(limit=50)}
        assert any("#chunk-" in i for i in ids)
        assert len(ids) == 6

    def test_list_documents_can_exclude_chunks(self, store: DocumentStore) -> None:
        parent_ids = self._seed_chunked_corpus(store, parents=2, per_parent=2)
        ids = {
            d["doc_id"] for d in store.list_documents(limit=50, include_chunks=False)
        }
        assert ids == set(parent_ids)

    def test_excluding_chunks_still_fills_the_page(self, store: DocumentStore) -> None:
        """A ``limit=N`` read returns N *non-chunk* rows, not N-minus-chunks.

        The regression this pins is filtering after the read instead of
        during it: with 3 chunks per parent, a post-hoc filter over a
        20-row page yields 5 rows, and the caller cannot distinguish that
        from the end of the data.
        """
        self._seed_chunked_corpus(store, parents=20, per_parent=3)
        page = store.list_documents(limit=20, offset=0, include_chunks=False)
        assert len(page) == 20
        assert not [d for d in page if "#chunk-" in d["doc_id"]]

    def test_excluding_chunks_keeps_offset_pages_disjoint(
        self, store: DocumentStore
    ) -> None:
        self._seed_chunked_corpus(store, parents=10, per_parent=3)
        first = store.list_documents(limit=5, offset=0, include_chunks=False)
        second = store.list_documents(limit=5, offset=5, include_chunks=False)
        assert len(first) == len(second) == 5
        assert {d["doc_id"] for d in first}.isdisjoint({d["doc_id"] for d in second})

    def test_count_can_exclude_chunks(self, store: DocumentStore) -> None:
        self._seed_chunked_corpus(store, parents=3, per_parent=4)
        assert store.count() == 15
        assert store.count(include_chunks=False) == 3

    def test_search_can_exclude_chunks(self, store: DocumentStore) -> None:
        """Chunks drop out; the parent they were sliced from still matches."""
        parent_ids = self._seed_chunked_corpus(store, parents=2, per_parent=3)
        unfiltered = store.search("searchable", limit=50)
        assert any("#chunk-" in d["doc_id"] for d in unfiltered)

        filtered = store.search("searchable", limit=50, include_chunks=False)
        assert {d["doc_id"] for d in filtered} == set(parent_ids)

    @staticmethod
    def _seed_chunk_favouring_corpus(store: DocumentStore, parents: int) -> None:
        """Seed a corpus whose *chunks* outrank their parents on the query.

        The ranking is load-bearing and is the reason this does not reuse
        :meth:`_seed_chunked_corpus`. ``search`` applies ``LIMIT`` after
        ordering by relevance, so a post-hoc filter is only distinguishable
        from a pushdown when the top-N *contains chunks*. With the shared
        fixture's content the chunks are strictly longer than their parents
        and carry the query term once, so BM25 ranks every parent above
        every chunk and a 20-row page over 25 parents is all parents — a
        page filter passes and the test proves nothing.

        Here the term appears three times in a short chunk and once in a
        long parent, which puts chunks first on term frequency (Postgres
        ``ts_rank``) and on frequency *and* brevity (SQLite ``bm25``).
        """
        for p in range(parents):
            store.put(
                f"corpus:notes:doc{p}",
                f"searchable parent {p} "
                + " ".join(f"filler{p}x{i}" for i in range(40)),
            )
            for c in range(3):
                store.put(
                    f"corpus:notes:doc{p}#chunk-{c}",
                    "searchable searchable searchable",
                    {"parent_doc_id": f"corpus:notes:doc{p}", "chunk_index": c},
                )

    def test_excluding_chunks_still_fills_the_search_limit(
        self, store: DocumentStore
    ) -> None:
        """A ``limit=N`` search returns N *non-chunk* rows, not N-minus-chunks.

        The ``list_documents`` sibling of this test pins the same property
        for the listing; this one pins it for search, which is where
        ``GET /api/v1/search`` reads (#396). The regression it catches is
        filtering the result set instead of the query: with chunks ranking
        above parents, a post-hoc filter over a 20-row page yields *zero*
        rows, and a caller who asked for 20 cannot tell that from "nothing
        matched". The predicate has to run before ``LIMIT`` for the count
        the caller gets back to mean anything.
        """
        self._seed_chunk_favouring_corpus(store, parents=25)

        # Precondition: chunks really do outrank parents here, so the
        # assertion below is testing the pushdown and not the fixture.
        unfiltered = store.search("searchable", limit=20)
        assert all("#chunk-" in d["doc_id"] for d in unfiltered)

        page = store.search("searchable", limit=20, include_chunks=False)
        assert len(page) == 20
        assert not [d for d in page if "#chunk-" in d["doc_id"]]

    def test_chunk_documents_remain_addressable_when_excluded(
        self, store: DocumentStore
    ) -> None:
        """Exclusion is a listing choice, never a deletion."""
        self._seed_chunked_corpus(store, parents=1, per_parent=1)
        assert store.get("corpus:notes:doc0#chunk-0") is not None

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

    def test_get_by_hash_returns_none_for_missing(self, store: DocumentStore) -> None:
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

    def test_search_empty_query_returns_empty_list(self, store: DocumentStore) -> None:
        store.put("d1", "indexed content")
        assert store.search("") == []

    def test_search_empty_store_returns_empty_list(self, store: DocumentStore) -> None:
        assert store.search("anything") == []

    def test_search_results_carry_rank(self, store: DocumentStore) -> None:
        store.put("d1", "python programming language")
        results = store.search("python")
        assert len(results) >= 1
        # Per ABC: search results "with a rank key".
        assert "rank" in results[0]

    def test_search_matches_on_some_terms_not_all(self, store: DocumentStore) -> None:
        """A multi-word query matches documents carrying *some* of its terms.

        Callers pass natural-language intents — ``get_context(intent=...)``
        hands the whole sentence to this method — so requiring every term to
        co-occur makes recall fall toward zero as the intent gets more
        specific, which is backwards.

        Postgres did exactly that: ``plainto_tsquery`` ANDs its terms, and on
        the production corpus ``"implement the classify layer tagging
        pipeline"`` matched **0** documents under AND and 267 under OR.
        SQLite had always OR-ed, so the two backends disagreed and every test
        was written against the permissive one. Ranking, not exclusion, is
        what sorts a loose match down.
        """
        store.put("d1", "the classify layer handles tagging for the pipeline")
        store.put("d2", "an unrelated note about kitchen renovation")

        results = store.search("implement the classify layer tagging pipeline")

        assert [r["doc_id"] for r in results][:1] == ["d1"], (
            "a document matching most query terms must be returned and ranked first"
        )

    def test_search_ranks_a_fuller_match_higher(self, store: DocumentStore) -> None:
        """OR semantics without ranking would be useless; pin the ordering."""
        store.put("weak", "a document mentioning only pipeline")
        store.put("strong", "classify layer tagging pipeline all together here")

        results = store.search("classify layer tagging pipeline")
        ids = [r["doc_id"] for r in results]

        assert "strong" in ids
        assert ids.index("strong") < ids.index("weak")

    def test_search_tag_filter_default_passes_valueless_facets(
        self, store: DocumentStore
    ) -> None:
        """A facet with no value never excludes — missing *or* empty.

        Every backend default-passes a missing facet so untagged items stay
        retrievable. An empty list facet (``domain: []``) carries no value
        either and must behave identically: the classify-on-write path
        persists exactly that shape (``domain`` is the one hard-excluding
        facet, so it is never auto-assigned — see
        :mod:`trellis.classify.ingest`), so a backend that reads ``[]`` as a
        value hides every tagged document from every domain-scoped query.
        """
        store.put("untagged", "python programming language")
        store.put(
            "empty_facet",
            "python programming language",
            {"content_tags": {"domain": [], "signal_quality": "standard"}},
        )
        results = store.search(
            "python",
            filters={"content_tags": {"domain": {"in": ["engineering"]}}},
        )
        assert {r["doc_id"] for r in results} == {"untagged", "empty_facet"}

    def test_search_list_facet_matches_a_tagged_document(
        self, store: DocumentStore
    ) -> None:
        """A list facet must MATCH, not merely default-pass.

        This suite pinned only the default-pass half — missing and empty
        facets stay visible — and never asserted that a document carrying a
        value is *returned* for it. Postgres consequently read
        ``metadata -> 'content_tags' ->> 'domain'`` as the text
        ``'["finance"]'``, which is not NULL, is not ``'[]'``, and is not in
        ``('finance')``: all three branches false, so a correctly tagged
        document was hard-excluded from its own domain query on the deployed
        backend, with CI green throughout.

        Every facet in :data:`LIST_FACETS` is covered rather than ``domain``
        alone, because the divergence that hid this was one backend keeping
        its own narrower idea of which facets are lists.
        """
        for facet in sorted(LIST_FACETS):
            store.put(
                f"match-{facet}",
                "python programming language",
                {"content_tags": {facet: ["alpha", "beta"]}},
            )
            store.put(
                f"other-{facet}",
                "python programming language",
                {"content_tags": {facet: ["gamma"]}},
            )

        for facet in sorted(LIST_FACETS):
            results = store.search(
                "python", filters={"content_tags": {facet: {"in": ["alpha"]}}}
            )
            found = {r["doc_id"] for r in results}
            assert f"match-{facet}" in found, (
                f"{facet}: a document tagged with the queried value was excluded"
            )
            assert f"other-{facet}" not in found, (
                f"{facet}: a document tagged with a different value was returned"
            )

    def test_search_list_facet_not_in_excludes_a_match(
        self, store: DocumentStore
    ) -> None:
        """``not_in`` on a list facet is the inverse, and still default-passes."""
        store.put("untagged", "python programming language")
        store.put(
            "noisy",
            "python programming language",
            {"content_tags": {"domain": ["spam"]}},
        )
        store.put(
            "wanted",
            "python programming language",
            {"content_tags": {"domain": ["engineering"]}},
        )
        results = store.search(
            "python", filters={"content_tags": {"domain": {"not_in": ["spam"]}}}
        )
        assert {r["doc_id"] for r in results} == {"untagged", "wanted"}

    def test_search_list_facet_tolerates_a_scalar_stored_shape(
        self, store: DocumentStore
    ) -> None:
        """``domain`` is legally ``list[str] | str | None`` on the metadata model.

        A backend that assumes the list shape can do worse than mis-filter:
        Postgres' ``jsonb_array_elements_text`` *raises* on a scalar, which
        takes down the whole query rather than returning the wrong rows. The
        scalar must match by equality — the shape #282's shredding failure
        was about.
        """
        store.put(
            "scalar",
            "python programming language",
            {"content_tags": {"domain": "engineering"}},
        )
        store.put(
            "scalar_other",
            "python programming language",
            {"content_tags": {"domain": "finance"}},
        )
        results = store.search(
            "python", filters={"content_tags": {"domain": {"in": ["engineering"]}}}
        )
        found = {r["doc_id"] for r in results}
        assert "scalar" in found
        assert "scalar_other" not in found
