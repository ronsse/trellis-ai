"""``chunk_parent_id`` and ``is_chunk_doc_id`` read the minted shape (#1001).

A substring test took any caller-named id that contained ``#chunk-`` for a
chunk of whatever preceded it. The class prior's chunk scope acts on that
reading (it withholds a chunk by its parent's form), so the classifier must
match the minted shape, ``<parent>#chunk-<digits>``, and nothing wider.
"""

from __future__ import annotations

import pytest

from trellis.ingest_corpus.models import chunk_doc_id, chunk_parent_id, is_chunk_doc_id


@pytest.mark.parametrize(
    "parent",
    [
        "notes",
        "corpus:notes:0f1e2d3c4b5a69788796a5b4c3d2e1f001122334",
        "conversation:claude-ai:abc",
        # A caller-named parent that itself holds the marker rolls up whole.
        "notes#chunk-intro",
        "a#chunk-1",
    ],
)
@pytest.mark.parametrize("index", [0, 7, 120])
def test_a_minted_chunk_id_round_trips_to_its_parent(parent: str, index: int) -> None:
    minted = chunk_doc_id(parent, index)

    assert is_chunk_doc_id(minted)
    assert chunk_parent_id(minted) == parent


@pytest.mark.parametrize(
    "doc_id",
    [
        "notes",
        "notes#chunk-intro",
        "notes#chunk-intro-2",
        "notes#chunk-",
        "notes#chunk-1a",
        "notes#chunk--1",
        "notes#chunk-3 ",
        "notes#chunk-²",
        "notes#chunkish",
        "notes#chunk-3#fragment",
        "#chunk-3",
        "",
    ],
)
def test_an_id_that_is_not_the_minted_shape_is_no_chunk(doc_id: str) -> None:
    assert not is_chunk_doc_id(doc_id)
    assert chunk_parent_id(doc_id) is None


def test_zero_padded_indices_are_still_chunks() -> None:
    """Fixtures mint ``<parent>#chunk-0003``: digits, not canonical ints."""
    assert chunk_parent_id("conv#chunk-0003") == "conv"
