"""Unit tests for Vector Store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

pytest.importorskip("numpy")

from trellis.stores.base.vector import VectorStore
from trellis.stores.sqlite.vector import SQLiteVectorStore


@pytest.fixture
def vector_store(tmp_path: Path) -> SQLiteVectorStore:
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    yield store  # type: ignore[misc]
    store.close()


def test_implements_abc() -> None:
    assert issubclass(SQLiteVectorStore, VectorStore)


def test_upsert_and_get(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0, 0.0], {"label": "x"})
    result = vector_store.get("v1")
    assert result is not None
    assert result["item_id"] == "v1"
    assert result["dimensions"] == 3
    assert result["metadata"]["label"] == "x"


def test_upsert_updates(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0], {"v": 1})
    vector_store.upsert("v1", [0.0, 1.0], {"v": 2})
    result = vector_store.get("v1")
    assert result is not None
    assert result["metadata"]["v"] == 2
    assert result["dimensions"] == 2


def test_query_similar(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("a", [1.0, 0.0, 0.0])
    vector_store.upsert("b", [0.9, 0.1, 0.0])
    vector_store.upsert("c", [0.0, 0.0, 1.0])
    results = vector_store.query([1.0, 0.0, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0]["item_id"] == "a"  # exact match
    assert results[0]["score"] > results[1]["score"]


def test_query_with_filters(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("a", [1.0, 0.0], {"cat": "x"})
    vector_store.upsert("b", [0.9, 0.1], {"cat": "y"})
    results = vector_store.query([1.0, 0.0], filters={"cat": "x"})
    assert len(results) == 1
    assert results[0]["item_id"] == "a"


def test_delete(vector_store: SQLiteVectorStore) -> None:
    vector_store.upsert("v1", [1.0, 0.0])
    assert vector_store.delete("v1") is True
    assert vector_store.get("v1") is None
    assert vector_store.delete("v1") is False


def test_count(vector_store: SQLiteVectorStore) -> None:
    assert vector_store.count() == 0
    vector_store.upsert("a", [1.0])
    vector_store.upsert("b", [0.5])
    assert vector_store.count() == 2


def test_get_nonexistent(vector_store: SQLiteVectorStore) -> None:
    assert vector_store.get("nope") is None


def test_query_empty(vector_store: SQLiteVectorStore) -> None:
    results = vector_store.query([1.0, 0.0])
    assert results == []


def test_upsert_refuses_a_non_finite_metadata_value(
    vector_store: SQLiteVectorStore,
) -> None:
    """json.dumps(..., allow_nan=False) refuses NaN/Infinity metadata before
    the INSERT runs. ``upsert_bulk`` loops over ``upsert`` (SQLite is an
    in-process backend; the bulk method exists for API symmetry only), so
    this one fix covers both call paths.
    """
    with pytest.raises(ValueError, match="not JSON compliant"):
        vector_store.upsert("v1", [1.0, 0.0], {"score": float("inf")})
    assert vector_store.get("v1") is None
    assert vector_store.count() == 0


# ---------------------------------------------------------------------------
# Rows of more than one width in one store
# ---------------------------------------------------------------------------

_WIDE = 8


def _wide(*lead: float) -> list[float]:
    """An 8-wide vector whose first components are ``lead``."""
    return [*lead, *([0.0] * (_WIDE - len(lead)))]


@pytest.fixture
def mixed_store(vector_store: SQLiteVectorStore) -> SQLiteVectorStore:
    """Three 8-wide rows and two 3-wide rows, interleaved on insert."""
    vector_store.upsert("w1", _wide(1.0), {"cat": "x"})
    vector_store.upsert("n1", [1.0, 0.0, 0.0], {"cat": "x"})
    vector_store.upsert("w2", _wide(0.9, 0.1), {"cat": "y"})
    vector_store.upsert("n2", [0.0, 1.0, 0.0], {"cat": "y"})
    vector_store.upsert("w3", _wide(0.0, 0.0, 1.0), {"cat": "x"})
    return vector_store


def test_query_scores_only_rows_of_the_query_width(
    mixed_store: SQLiteVectorStore,
) -> None:
    results = mixed_store.query(_wide(1.0), top_k=10)

    assert [r["item_id"] for r in results] == ["w1", "w2", "w3"]
    assert results[0]["score"] > results[1]["score"] > results[2]["score"]


def test_a_narrow_query_returns_the_narrow_rows(
    mixed_store: SQLiteVectorStore,
) -> None:
    results = mixed_store.query([1.0, 0.0, 0.0], top_k=10)

    assert [r["item_id"] for r in results] == ["n1", "n2"]


def test_a_mixed_store_ranks_one_width_as_a_store_of_only_that_width_does(
    mixed_store: SQLiteVectorStore, tmp_path: Path
) -> None:
    alone = SQLiteVectorStore(tmp_path / "alone.db")
    try:
        alone.upsert("w1", _wide(1.0), {"cat": "x"})
        alone.upsert("w2", _wide(0.9, 0.1), {"cat": "y"})
        alone.upsert("w3", _wide(0.0, 0.0, 1.0), {"cat": "x"})

        assert mixed_store.query(_wide(1.0, 0.2), top_k=2) == alone.query(
            _wide(1.0, 0.2), top_k=2
        )
    finally:
        alone.close()


def test_a_query_of_a_width_no_row_has_returns_nothing(
    mixed_store: SQLiteVectorStore,
) -> None:
    assert mixed_store.query([1.0, 0.0, 0.0, 0.0, 0.0], top_k=10) == []


def test_a_store_holding_only_another_width_returns_nothing(
    vector_store: SQLiteVectorStore,
) -> None:
    vector_store.upsert("n1", [1.0, 0.0, 0.0])
    vector_store.upsert("n2", [0.0, 1.0, 0.0])

    assert vector_store.query(_wide(1.0), top_k=10) == []


def test_the_width_test_composes_with_scalar_and_complex_filters(
    mixed_store: SQLiteVectorStore,
) -> None:
    # A scalar filter is pushed to SQL beside the width test; a list value is
    # not, so it runs Python-side over the rows the width test let through.
    scalar = mixed_store.query(_wide(1.0), top_k=10, filters={"cat": "x"})
    assert [r["item_id"] for r in scalar] == ["w1", "w3"]

    mixed_store.upsert("w4", _wide(0.5, 0.5), {"tags": ["a", "b"]})
    mixed_store.upsert("n3", [0.5, 0.5, 0.0], {"tags": ["a", "b"]})
    complex_ = mixed_store.query(_wide(1.0), top_k=10, filters={"tags": ["a", "b"]})
    assert [r["item_id"] for r in complex_] == ["w4"]


def test_replacing_a_row_at_another_width_moves_it_between_queries(
    mixed_store: SQLiteVectorStore,
) -> None:
    mixed_store.upsert("w2", [0.9, 0.1, 0.0])

    assert [r["item_id"] for r in mixed_store.query(_wide(1.0), top_k=10)] == [
        "w1",
        "w3",
    ]
    assert {r["item_id"] for r in mixed_store.query([1.0, 0.0, 0.0], top_k=10)} == {
        "n1",
        "n2",
        "w2",
    }


# ---------------------------------------------------------------------------
# The degenerate case is loud: a width no stored row has
# ---------------------------------------------------------------------------

_MISMATCH = "vector_query.width_mismatch"


def _mismatch_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == _MISMATCH]


def test_a_store_holding_only_another_width_warns_once_with_the_count(
    vector_store: SQLiteVectorStore,
) -> None:
    vector_store.upsert("stale-a", [1.0, 0.0, 0.0])
    vector_store.upsert("stale-b", [0.0, 1.0, 0.0])
    vector_store.upsert("stale-c", [0.0, 0.0, 1.0])

    with capture_logs() as logs:
        assert vector_store.query(_wide(1.0), top_k=10) == []

    events = _mismatch_events(logs)
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["query_width"] == _WIDE
    assert events[0]["other_width_rows"] == 3
    # Counts only: no row id and no row content reaches the event.
    assert "stale-" not in repr(events[0])


def test_a_width_no_row_of_a_mixed_store_has_warns_with_every_row_counted(
    mixed_store: SQLiteVectorStore,
) -> None:
    with capture_logs() as logs:
        assert mixed_store.query([1.0, 0.0, 0.0, 0.0, 0.0], top_k=10) == []

    events = _mismatch_events(logs)
    assert len(events) == 1
    assert events[0]["query_width"] == 5
    assert events[0]["other_width_rows"] == 5


def test_an_empty_store_does_not_warn(vector_store: SQLiteVectorStore) -> None:
    with capture_logs() as logs:
        assert vector_store.query(_wide(1.0), top_k=10) == []

    assert not _mismatch_events(logs)


def test_a_store_of_the_query_width_does_not_warn(
    vector_store: SQLiteVectorStore,
) -> None:
    vector_store.upsert("w1", _wide(1.0))
    vector_store.upsert("w2", _wide(0.9, 0.1))

    with capture_logs() as logs:
        assert [r["item_id"] for r in vector_store.query(_wide(1.0))] == ["w1", "w2"]

    assert not _mismatch_events(logs)


def test_a_mixed_store_that_answers_does_not_warn(
    mixed_store: SQLiteVectorStore,
) -> None:
    # Rows of another width are present, but the query found rows of its own:
    # the skip stays silent, and the count is never paid for.
    with capture_logs() as logs:
        assert mixed_store.query(_wide(1.0), top_k=10)

    assert not _mismatch_events(logs)


def test_a_filter_that_matches_nothing_is_not_misreported_as_a_width_mismatch(
    mixed_store: SQLiteVectorStore,
) -> None:
    # The query's width is stored, the filter simply matches no row, and no
    # row of another width carries the filter value either.
    with capture_logs() as logs:
        assert mixed_store.query(_wide(1.0), filters={"cat": "absent"}) == []

    assert not _mismatch_events(logs)


def test_the_count_takes_the_same_filters_as_the_query(
    vector_store: SQLiteVectorStore,
) -> None:
    vector_store.upsert("n1", [1.0, 0.0, 0.0], {"cat": "x"})
    vector_store.upsert("n2", [0.0, 1.0, 0.0], {"cat": "y"})
    vector_store.upsert("n3", [0.0, 0.0, 1.0], {"cat": "x"})

    with capture_logs() as logs:
        assert vector_store.query(_wide(1.0), filters={"cat": "x"}) == []

    events = _mismatch_events(logs)
    assert len(events) == 1
    assert events[0]["other_width_rows"] == 2
