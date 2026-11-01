"""The public-only read ranks public documents and nothing else.

A caller that reads only ``public`` documents must receive nothing that depends
on a document it may not read, so the public path never ranks a hidden row
(``trellis.retrieve.public_rank``). These tests pin each part of that: which
rows are read (the label is a query predicate), how they are scored (locally,
over those rows alone), which axes run (an allow-list), and the gates that keep
running over the public candidates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock

import pytest

from tests.unit.retrieve.test_sensitivity import label
from trellis.retrieve import public_rank
from trellis.retrieve.builder_factory import describe_axes
from trellis.retrieve.class_prior import ClassPriorConfig
from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.public_rank import (
    PUBLIC_ROW_CEILING,
    bm25_rank,
    cosine,
    public_keyword_rows,
    public_label_filter,
    read_public_documents,
)
from trellis.retrieve.sensitivity import stamp_sensitivity
from trellis.retrieve.strategies import (
    GraphSearch,
    KeywordSearch,
    SearchStrategy,
    SemanticSearch,
)
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY
from trellis.stores.sqlite.document import SQLiteDocumentStore
from trellis.stores.sqlite.vector import SQLiteVectorStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteDocumentStore]:
    s = SQLiteDocumentStore(tmp_path / "documents.db")
    yield s
    s.close()


@pytest.fixture
def vectors(tmp_path: Path) -> Iterator[SQLiteVectorStore]:
    v = SQLiteVectorStore(tmp_path / "vectors.db")
    yield v
    v.close()


def _ids(rows: list[tuple[dict[str, Any], float]]) -> list[str]:
    return [row["doc_id"] for row, _ in rows]


class TestLabelFilter:
    def test_it_is_the_record_the_labelling_path_writes_for_public(self) -> None:
        stamped = stamp_sensitivity({}, "public", "public")
        assert public_label_filter() == {
            DATA_CLASSIFICATION_KEY: stamped[DATA_CLASSIFICATION_KEY]
        }

    def test_it_selects_public_documents_and_no_other(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("pub", "alpha", stamp_sensitivity({}, "public", "public"))
        store.put("internal", "alpha", stamp_sensitivity({}, "internal", "internal"))
        store.put("plain", "alpha", {})
        rows = store.list_documents(filters=public_label_filter())
        assert [r["doc_id"] for r in rows] == ["pub"]

    def test_a_public_record_with_other_fields_is_not_selected(
        self, store: SQLiteDocumentStore
    ) -> None:
        """The filter fails closed: equality over the whole record."""
        record = dict(public_label_filter()[DATA_CLASSIFICATION_KEY])
        record["regulatory_tags"] = ["hipaa"]
        store.put("tagged-public", "alpha", {DATA_CLASSIFICATION_KEY: record})
        assert read_public_documents(store) == []


class TestReadPublicDocuments:
    def test_hidden_rows_are_not_in_the_result(
        self, store: SQLiteDocumentStore
    ) -> None:
        for i in range(2500):
            store.put(f"hidden-{i:04d}", "alpha", {})
        for i in range(3):
            store.put(f"pub-{i}", "alpha", label("public"))
        assert sorted(r["doc_id"] for r in read_public_documents(store)) == [
            "pub-0",
            "pub-1",
            "pub-2",
        ]

    def test_the_ceiling_counts_public_rows_only(
        self, store: SQLiteDocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(public_rank, "PUBLIC_ROW_CEILING", 4)
        for i in range(30):
            store.put(f"hidden-{i:02d}", "alpha", {})
        for i in range(6):
            store.put(f"pub-{i}", "alpha", label("public"))
        rows = read_public_documents(store)
        assert len(rows) == 4
        assert {r["doc_id"].split("-")[0] for r in rows} == {"pub"}

    def test_a_chunk_is_never_read(self, store: SQLiteDocumentStore) -> None:
        store.put("p", "alpha", label("public"))
        store.put("p#chunk-0", "alpha", label("public"))
        assert [r["doc_id"] for r in read_public_documents(store)] == ["p"]

    def test_the_ceiling_is_the_documented_one(self) -> None:
        assert PUBLIC_ROW_CEILING == 2000


class TestBm25:
    ROWS: ClassVar[list[dict[str, Any]]] = [
        {"doc_id": "b", "content": "alpha beta"},
        {"doc_id": "a", "content": "alpha beta"},
        {"doc_id": "c", "content": "alpha alpha alpha gamma"},
        {"doc_id": "d", "content": "unrelated words only"},
    ]

    def test_a_score_is_a_function_of_the_rows_given_and_no_others(self) -> None:
        def scores(rows: list[dict[str, Any]]) -> dict[str, float]:
            return {r["doc_id"]: s for r, s in bm25_rank("alpha", rows)}

        given = self.ROWS[:3]
        assert scores(given) == scores(list(reversed(given)))
        # Rows given that do not hold the word still count in N and the average
        # length, so a row added to the input moves a score: which is why the
        # public path must never be given a hidden one.
        padded = given + [
            {"doc_id": f"x{i}", "content": "nothing here"} for i in range(40)
        ]
        assert scores(padded)["a"] != scores(given)["a"]

    def test_ties_break_on_doc_id_and_a_non_match_sorts_last(self) -> None:
        ranked = [r["doc_id"] for r, _ in bm25_rank("beta", self.ROWS)]
        assert ranked[:2] == ["a", "b"]
        assert ranked[-2:] == ["c", "d"]

    def test_it_is_deterministic(self) -> None:
        assert bm25_rank("alpha beta", self.ROWS) == bm25_rank(
            "alpha beta", list(reversed(self.ROWS))
        )

    def test_no_rows_is_no_ranking(self) -> None:
        assert bm25_rank("alpha", []) == []


class TestCosine:
    def test_identical_vectors(self) -> None:
        assert cosine([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            "text",
            [1.0],
            [1.0, 2.0, 3.0],
            [0.0, 0.0],
            [float("nan"), 1.0],
            [[1.0]],
        ],
    )
    def test_an_unusable_stored_vector_is_none(self, stored: Any) -> None:
        assert cosine([1.0, 0.0], stored) is None


class TestPublicKeywordRows:
    def test_a_hidden_word_does_not_move_the_order_or_the_scores(
        self, tmp_path: Path
    ) -> None:
        plain = SQLiteDocumentStore(tmp_path / "a.db")
        mixed = SQLiteDocumentStore(tmp_path / "b.db")
        try:
            for s in (plain, mixed):
                s.put("p1", "failover plan for the queue", label("public"))
                s.put("p2", "failover failover steps", label("public"))
            mixed.put("h", "failover " * 50, {})

            # The rows carry the store's own ``rank`` (computed over the whole
            # corpus) and clocks, so compare the order and the scores served.
            def served(rows: list[tuple[dict[str, Any], float]]) -> list[Any]:
                return [(row["doc_id"], score) for row, score in rows]

            assert served(public_keyword_rows(plain, "failover")) == served(
                public_keyword_rows(mixed, "failover")
            )
        finally:
            plain.close()
            mixed.close()

    def test_the_stores_own_rank_is_discarded(self) -> None:
        store = MagicMock()
        store.search.return_value = [
            {"doc_id": "weak", "content": "alpha", "rank": -99.0, "metadata": {}},
            {
                "doc_id": "strong",
                "content": "alpha alpha alpha",
                "rank": -0.1,
                "metadata": {},
            },
        ]
        gate_rows = store.search.return_value
        for row in gate_rows:
            row["metadata"] = label("public")
        ranked = public_keyword_rows(store, "alpha")
        assert _ids(ranked) == ["strong", "weak"]

    def test_the_label_is_asked_of_the_store(self) -> None:
        store = MagicMock()
        store.search.return_value = []
        public_keyword_rows(store, "alpha", filters={"scope": "x"})
        kwargs = store.search.call_args.kwargs
        assert kwargs["filters"] == {"scope": "x", **public_label_filter()}
        assert kwargs["include_chunks"] is False
        assert kwargs["limit"] == PUBLIC_ROW_CEILING

    def test_a_row_the_gate_refuses_is_dropped_even_if_the_store_returned_it(
        self,
    ) -> None:
        store = MagicMock()
        store.search.return_value = [
            {"doc_id": "x", "content": "alpha", "metadata": label("internal")},
        ]
        assert public_keyword_rows(store, "alpha") == []


class TestSemanticRead:
    @staticmethod
    def _put(
        store: SQLiteDocumentStore,
        vectors: SQLiteVectorStore,
        doc_id: str,
        vector: list[float],
        meta: dict[str, Any],
    ) -> None:
        store.put(doc_id, f"text of {doc_id}", meta)
        vectors.upsert(doc_id, vector, {})

    def test_it_never_calls_the_vector_stores_query(
        self, store: SQLiteDocumentStore, vectors: SQLiteVectorStore
    ) -> None:
        self._put(store, vectors, "pub", [1.0, 0.0], label("public"))
        vectors.query = MagicMock(side_effect=AssertionError("query called"))  # type: ignore[method-assign]
        strategy = SemanticSearch(vectors, lambda _q: [1.0, 0.0])
        result = strategy.public_result("q", store, limit=5)
        assert [i.item_id for i in result.items] == ["pub"]

    def test_a_hidden_vector_that_matches_exactly_is_not_read(
        self, store: SQLiteDocumentStore, vectors: SQLiteVectorStore
    ) -> None:
        self._put(store, vectors, "hidden", [1.0, 0.0], {})
        self._put(store, vectors, "pub-near", [0.9, 0.1], label("public"))
        self._put(store, vectors, "pub-far", [0.0, 1.0], label("public"))
        strategy = SemanticSearch(vectors, lambda _q: [1.0, 0.0])
        items = strategy.public_result("q", store, limit=5).items
        assert [i.item_id for i in items] == ["pub-near", "pub-far"]

    def test_a_row_with_no_usable_vector_is_skipped(
        self, store: SQLiteDocumentStore, vectors: SQLiteVectorStore
    ) -> None:
        self._put(store, vectors, "ok", [1.0, 0.0], label("public"))
        self._put(store, vectors, "wide", [1.0, 0.0, 0.0], label("public"))
        self._put(store, vectors, "zero", [0.0, 0.0], label("public"))
        store.put("no-vector", "text", label("public"))
        strategy = SemanticSearch(vectors, lambda _q: [1.0, 0.0])
        items = strategy.public_result("q", store, limit=10).items
        assert [i.item_id for i in items] == ["ok"]

    def test_without_an_embedder_it_serves_nothing(
        self, store: SQLiteDocumentStore, vectors: SQLiteVectorStore
    ) -> None:
        self._put(store, vectors, "pub", [1.0, 0.0], label("public"))
        assert SemanticSearch(vectors).public_result("q", store).items == []


class TestMalformedPublicRow:
    def test_a_public_row_with_a_malformed_importance_costs_that_row_only(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("good", "alpha beta", label("public"))
        store.put("bad", "alpha beta", {**label("public"), "auto_importance": 0.9})
        result = KeywordSearch(store).public_result("alpha", limit=5)
        assert [i.item_id for i in result.items] == ["good"]
        assert [i.item_id for i in result.skipped] == ["bad"]


def _builder(
    store: SQLiteDocumentStore,
    vectors: SQLiteVectorStore | None = None,
    *,
    extra: list[SearchStrategy] | None = None,
    **kwargs: Any,
) -> PackBuilder:
    strategies: list[SearchStrategy] = [KeywordSearch(store)]
    if vectors is not None:
        strategies.append(SemanticSearch(vectors, lambda _q: [1.0, 0.0]))
    strategies.extend(extra or [])
    return PackBuilder(strategies=strategies, document_store=store, **kwargs)


class TestAllowList:
    """The public path runs the keyword and semantic axes and nothing else."""

    @staticmethod
    def _spy(spec: type, name: str) -> MagicMock:
        spy = MagicMock(spec=spec)
        spy.name = name
        return spy

    def test_a_graph_or_plugin_strategy_is_never_asked(
        self, store: SQLiteDocumentStore, vectors: SQLiteVectorStore
    ) -> None:
        store.put("pub", "alpha", label("public"))
        graph = self._spy(GraphSearch, "graph")
        plugin = self._spy(SearchStrategy, "plugin")
        builder = _builder(store, vectors, extra=[graph, plugin])
        assert [s.name for s in builder._public_strategies()] == [
            "keyword",
            "semantic",
        ]
        pack = builder.build("alpha", clearance="public")
        assert graph.method_calls == []
        assert plugin.method_calls == []
        assert [i.item_id for i in pack.items] == ["pub"]

    def test_a_strategy_that_did_not_run_is_reported_as_not_run(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("pub", "alpha", label("public"))
        graph = self._spy(GraphSearch, "graph")
        plugin = self._spy(SearchStrategy, "plugin")
        builder = _builder(store, extra=[graph, plugin])
        pack = builder.build("alpha", clearance="public")
        axes = describe_axes(
            builder,
            pack.retrieval_report.strategies_used,
            embedder_configured=False,
            graph_axis=pack.retrieval_report.graph_axis,
            public_only=True,
        )
        assert pack.retrieval_report.strategies_used == ["keyword"]
        assert axes["failed"] == ["plugin"]
        assert axes["graph"] == "absent"
        assert "graph" not in axes["available"]

    def test_a_strategy_that_would_serve_a_hidden_row_is_not_asked(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put("pub", "alpha", label("public"))
        plugin = self._spy(SearchStrategy, "plugin")
        _builder(store, extra=[plugin]).build("alpha", clearance="public")
        assert not plugin.search.called


class TestGatesStillRun:
    def test_class_prior_withholds_a_public_conversation_parent(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put(
            "talk",
            "alpha beta",
            {**label("public"), "document_form": "conversation"},
        )
        store.put("note", "alpha beta", label("public"))
        on = _builder(store, class_prior_config=ClassPriorConfig(mode="withhold"))
        off = _builder(store, class_prior_config=ClassPriorConfig(mode="off"))
        assert [i.item_id for i in on.build("alpha", clearance="public").items] == [
            "note"
        ]
        assert sorted(
            i.item_id for i in off.build("alpha", clearance="public").items
        ) == [
            "note",
            "talk",
        ]

    def test_an_archived_public_document_is_withheld(
        self, store: SQLiteDocumentStore
    ) -> None:
        store.put(
            "old",
            "alpha",
            {**label("public"), "lifecycle": {"state": "archived"}},
        )
        store.put("new", "alpha", label("public"))
        pack = _builder(store).build("alpha", clearance="public")
        assert [i.item_id for i in pack.items] == ["new"]

    def test_a_document_found_by_both_axes_is_counted_once_per_axis(
        self, store: SQLiteDocumentStore, vectors: SQLiteVectorStore
    ) -> None:
        """``candidates_found`` is read before dedup, a function of public rows."""
        for i in range(3):
            store.put(f"p{i}", "alpha beta", label("public"))
            vectors.upsert(f"p{i}", [1.0, 0.0], {})
        pack = _builder(store, vectors).build("alpha", clearance="public")
        assert len(pack.items) == 3
        assert pack.retrieval_report.candidates_found == 6

    def test_the_dedup_winner_does_not_move_with_hidden_documents(
        self, tmp_path: Path
    ) -> None:
        winners = []
        for hidden in (0, 1, 5, 40):
            s = SQLiteDocumentStore(tmp_path / f"d{hidden}.db")
            v = SQLiteVectorStore(tmp_path / f"v{hidden}.db")
            try:
                s.put("p", "alpha beta", label("public"))
                v.upsert("p", [1.0, 0.0], {})
                for i in range(hidden):
                    s.put(f"h{i}", f"garden {i}", {})
                    v.upsert(f"h{i}", [0.0, 1.0], {})
                pack = _builder(s, v).build("alpha", clearance="public")
                winners.append(
                    [(i.item_id, i.item_type, i.strategy_source) for i in pack.items]
                )
            finally:
                s.close()
                v.close()
        assert winners[0]
        assert all(w == winners[0] for w in winners)
