"""A SQLite vector store holding rows of two widths still serves a semantic pack.

``upsert`` stores a vector of any width, so an embedder swap (or a write at a
different width) leaves rows of two widths in one store. ``query`` used to
score every row, and one row of another width raised ``ValueError`` out of
``np.dot``: a semantic-only build then raised ``PackAssemblyError``, and a
multi-strategy build lost the semantic axis (recorded under
``strategy_failures``). The query now scores only rows of its own width.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy")

from trellis.retrieve.pack_builder import PackBuilder
from trellis.retrieve.strategies import SemanticSearch
from trellis.schemas.pack import PackBudget
from trellis.stores.sqlite.vector import SQLiteVectorStore

WIDE = 8
NARROW = 3
INTENT = "rotate the signing key"


def _embed(_text: str) -> list[float]:
    """The query embedding: the current embedder's width, ``WIDE``."""
    return [1.0] + [0.0] * (WIDE - 1)


def _meta(excerpt: str) -> dict[str, Any]:
    # Stamped, so the importance guard has nothing to say about these rows.
    return {
        "content": excerpt,
        "auto_importance": 0.5,
        "content_tags": {"importance_scored_at": datetime.now(UTC).isoformat()},
        "created_at": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def mixed_store(tmp_path: Path) -> SQLiteVectorStore:
    store = SQLiteVectorStore(tmp_path / "vectors.db")
    store.upsert("current-1", [1.0] + [0.0] * (WIDE - 1), _meta("current one"))
    store.upsert("stale-1", [1.0, 0.0, 0.0], _meta("stale one"))
    store.upsert("current-2", [0.8, 0.2] + [0.0] * (WIDE - 2), _meta("current two"))
    store.upsert("stale-2", [0.0, 1.0, 0.0], _meta("stale two"))
    yield store  # type: ignore[misc]
    store.close()


def _build(store: SQLiteVectorStore) -> list[str]:
    builder = PackBuilder(strategies=[SemanticSearch(store, embedding_fn=_embed)])
    pack = builder.build(
        intent=INTENT,
        clearance="restricted",
        budget=PackBudget(max_items=25, max_tokens=100_000),
    )
    return [item.item_id for item in pack.items]


def test_a_semantic_only_build_over_a_mixed_store_serves_the_matching_rows(
    mixed_store: SQLiteVectorStore,
) -> None:
    # The only configured strategy is required: before the fix this raised
    # PackAssemblyError ("Required strategy 'semantic' failed: ValueError").
    assert sorted(_build(mixed_store)) == ["current-1", "current-2"]


def test_a_store_holding_only_another_width_builds_an_empty_pack(
    tmp_path: Path,
) -> None:
    store = SQLiteVectorStore(tmp_path / "stale.db")
    try:
        store.upsert("stale-1", [1.0, 0.0, 0.0], _meta("stale one"))

        assert _build(store) == []
    finally:
        store.close()
