"""Tests for the first-class graph↔document link (ADR planes-and-substrates §2.4).

Covers the validator in ``trellis.stores.base.graph`` and the round-trip
behavior across every GraphStore backend. Each test is parametrized
over SQLite and Kuzu; Postgres is gated behind a live-DSN check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trellis.stores.base.graph import GraphStore, validate_document_ids
from trellis.stores.sqlite.graph import SQLiteGraphStore

if TYPE_CHECKING:
    from pathlib import Path

# Kuzu is an optional extra; skip the Kuzu parametrization if missing.
# Catches both ModuleNotFoundError (package absent) and ImportError
# (package present but KuzuStore symbol missing — e.g. when the backend
# was temporarily deferred post-upstream-archive, see the planes ADR
# 2026-04-19 Amendment).
try:
    from trellis.stores.kuzu import KuzuStore  # noqa: F401

    KUZU_AVAILABLE = True
except ImportError:  # pragma: no cover — runs without [kuzu]
    KUZU_AVAILABLE = False


# -- Validator -------------------------------------------------------------


class TestValidateDocumentIds:
    def test_none_is_valid(self) -> None:
        validate_document_ids(None)  # no raise

    def test_empty_list_is_valid(self) -> None:
        validate_document_ids([])  # no raise

    def test_list_of_strings_is_valid(self) -> None:
        validate_document_ids(["doc-1", "doc-2", "doc-3"])  # no raise

    def test_non_list_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="must be a list"):
            validate_document_ids("doc-1")  # type: ignore[arg-type]

    def test_dict_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="must be a list"):
            validate_document_ids({"doc-1": True})  # type: ignore[arg-type]

    def test_empty_string_element_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            validate_document_ids([""])

    def test_non_string_element_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            validate_document_ids([123])  # type: ignore[list-item]

    def test_duplicate_entries_raise(self) -> None:
        with pytest.raises(ValueError, match="duplicate entry"):
            validate_document_ids(["doc-1", "doc-2", "doc-1"])

    def test_error_names_the_offending_index(self) -> None:
        with pytest.raises(ValueError, match=r"document_ids\[2\]"):
            validate_document_ids(["a", "b", ""])


# -- Backend round-trip ----------------------------------------------------


BACKENDS: list[str] = ["sqlite"]
if KUZU_AVAILABLE:
    BACKENDS.append("kuzu")


@pytest.fixture(params=BACKENDS)
def graph_store(request: pytest.FixtureRequest, tmp_path: Path) -> GraphStore:
    backend = request.param
    store: GraphStore
    if backend == "sqlite":
        store = SQLiteGraphStore(tmp_path / "g.db")
    elif backend == "kuzu":
        from trellis.stores.kuzu import KuzuStore

        store = KuzuStore(tmp_path / "kz")
    else:  # pragma: no cover
        msg = f"Unknown backend {backend!r}"
        raise ValueError(msg)
    yield store
    store.close()


class TestDocumentIdsRoundTrip:
    def test_upsert_with_document_ids(self, graph_store: GraphStore) -> None:
        node_id = graph_store.upsert_node(
            None, "Entity", {"name": "x"}, document_ids=["doc-1", "doc-2"]
        )
        got = graph_store.get_node(node_id)
        assert got is not None
        assert got["document_ids"] == ["doc-1", "doc-2"]

    def test_default_absent_document_ids_returns_empty_list(
        self, graph_store: GraphStore
    ) -> None:
        node_id = graph_store.upsert_node(None, "Entity", {"name": "x"})
        got = graph_store.get_node(node_id)
        assert got is not None
        # Default is an empty list — consumers can iterate unconditionally
        assert got["document_ids"] == []

    def test_explicit_empty_list_returns_empty_list(
        self, graph_store: GraphStore
    ) -> None:
        node_id = graph_store.upsert_node(None, "Entity", {}, document_ids=[])
        got = graph_store.get_node(node_id)
        assert got is not None
        assert got["document_ids"] == []

    def test_document_ids_preserved_across_scd_versions(
        self, graph_store: GraphStore
    ) -> None:
        node_id = graph_store.upsert_node(
            None, "Entity", {"v": 1}, document_ids=["doc-1"]
        )
        graph_store.upsert_node(
            node_id, "Entity", {"v": 2}, document_ids=["doc-2", "doc-3"]
        )
        current = graph_store.get_node(node_id)
        history = graph_store.get_node_history(node_id)
        assert current["document_ids"] == ["doc-2", "doc-3"]
        assert [h["document_ids"] for h in history] == [
            ["doc-2", "doc-3"],
            ["doc-1"],
        ]

    def test_get_nodes_bulk_includes_document_ids(
        self, graph_store: GraphStore
    ) -> None:
        a = graph_store.upsert_node(None, "E", {}, document_ids=["doc-a"])
        b = graph_store.upsert_node(None, "E", {}, document_ids=["doc-b1", "doc-b2"])
        rows = graph_store.get_nodes_bulk([a, b])
        by_id = {r["node_id"]: r for r in rows}
        assert by_id[a]["document_ids"] == ["doc-a"]
        assert by_id[b]["document_ids"] == ["doc-b1", "doc-b2"]

    def test_query_returns_document_ids(self, graph_store: GraphStore) -> None:
        graph_store.upsert_node(None, "E", {}, document_ids=["d1"])
        graph_store.upsert_node(None, "E", {}, document_ids=[])
        rows = graph_store.query(node_type="E")
        assert all("document_ids" in r for r in rows)
        all_doc_ids = sorted(d for r in rows for d in r["document_ids"])
        assert all_doc_ids == ["d1"]

    def test_validation_errors_at_backend_boundary(
        self, graph_store: GraphStore
    ) -> None:
        with pytest.raises(ValueError, match="duplicate entry"):
            graph_store.upsert_node(None, "E", {}, document_ids=["dup", "dup"])
        with pytest.raises(ValueError, match="non-empty string"):
            graph_store.upsert_node(None, "E", {}, document_ids=["valid", ""])


# -- Backfill / migration graceful degradation ----------------------------


class TestSQLiteBackfillGracefulDegrade:
    """Pre-existing rows without document_ids_json must still read as []."""

    def test_pre_existing_null_column_reads_as_empty(self, tmp_path: Path) -> None:
        # Simulate a v3 database created before Phase 4 by manually
        # inserting a row with NULL document_ids_json.
        store = SQLiteGraphStore(tmp_path / "g.db")
        # The migration runs on __init__, so document_ids_json column
        # exists now; manually NULL it for a row to simulate backfill.
        node_id = store.upsert_node(None, "E", {}, document_ids=["d1"])
        store._conn.execute(
            "UPDATE nodes SET document_ids_json = NULL "
            "WHERE node_id = ? AND valid_to IS NULL",
            (node_id,),
        )
        store._conn.commit()

        got = store.get_node(node_id)
        assert got is not None
        assert got["document_ids"] == []  # graceful degrade

        store.close()
