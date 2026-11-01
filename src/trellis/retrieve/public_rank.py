"""The public-only read: which rows it ranks, and the scores it ranks them by.

A caller that reads only ``public`` documents must receive nothing that
depends on a document it may not read. A strategy that ranks the whole corpus
cannot promise that, however the page is cut afterwards: a hidden row can
outrank a public one, and a hidden row moves the corpus statistics (document
frequency, vector width) every score is computed from. So the public path
never ranks a hidden row:

* **Which rows.** The label is a *query predicate* (:func:`public_label_filter`
  handed to ``DocumentStore.search`` and ``DocumentStore.list_documents``), so
  a hidden row is not in any result set, whatever its text holds.
  :class:`~trellis.retrieve.sensitivity.DocumentClearance` then judges every row
  a second time, so the predicate can only narrow what is served.
* **Scores.** Computed here, over the rows that came back and no others:
  BM25 with ``N`` and document frequency taken over those rows
  (:func:`bm25_rank`), and cosine against the query vector of each row's own
  stored vector (:func:`cosine`). The store's ``rank`` and the vector store's
  ``query`` are never read, because both see the whole corpus (the latter
  filters after an over-fetch on two backends). Ties break on ``doc_id``.

This module is a leaf (no import of ``strategies``), so the strategies' public
methods can use it. Nothing here runs for a caller above ``public``.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

import numpy as np

from trellis.retrieve.sensitivity import DocumentClearance
from trellis.schemas.classification import DATA_CLASSIFICATION_KEY, DataClassification

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trellis.stores.base.document import DocumentStore

#: The most public rows one read holds. It counts public rows only: the label
#: is a query predicate, so no hidden row can reach it, and whether it bites is
#: a function of what the key may read. Above it, a keyword read keeps the rows
#: the document store ranks first (that rank sees the whole corpus), and a
#: semantic read keeps the newest.
PUBLIC_ROW_CEILING = 2000

#: BM25 constants (the usual defaults).
_BM25_K1 = 1.2
_BM25_B = 0.75

#: The query words, as the stores read them: ASCII alphanumerics, at most 20.
_WORD = re.compile(r"[a-z0-9]+")
_MAX_QUERY_WORDS = 20


def public_label_filter() -> dict[str, Any]:
    """The metadata filter that selects documents labelled ``public``.

    The label is one top-level key holding a whole record
    (``data_classification``), and a store filter is equality over a key's
    value, so the filter is the record the labelling paths write for ``public``
    (``stamp_sensitivity`` on the save surfaces, ``classification.set`` on a
    relabel), built from the same model so the two cannot drift. A document
    labelled ``public`` whose record carries anything more (regulatory tags, a
    jurisdiction, another classifier) does not match and is not served: the
    predicate fails closed. ``tests/unit/retrieve/test_public_rank.py`` pins it
    against both writers.
    """
    record = DataClassification(sensitivity="public", classified_by=["caller"])
    return {DATA_CLASSIFICATION_KEY: record.model_dump(mode="json")}


def query_words(query: str) -> list[str]:
    """The distinct lower-cased words a keyword query is made of, in order."""
    words = _WORD.findall(query.lower())[:_MAX_QUERY_WORDS]
    return list(dict.fromkeys(words))


def bm25_rank(
    query: str, rows: Sequence[dict[str, Any]]
) -> list[tuple[dict[str, Any], float]]:
    """``rows`` scored by BM25 over ``rows`` alone, best first, ties by ``doc_id``.

    ``N``, document frequency and average length are taken over ``rows``, so
    the score of a row is a function of the rows given and nothing else. A row
    holding none of the query words (a store with a different tokenizer
    matched it) scores zero and sorts last.
    """
    words = query_words(query)
    tokenised = [_WORD.findall((row.get("content") or "").lower()) for row in rows]
    total = len(rows)
    if not total:
        return []
    average = sum(len(t) for t in tokenised) / total or 1.0
    frequency = {w: sum(1 for t in tokenised if w in t) for w in words}
    scored: list[tuple[dict[str, Any], float]] = []
    for row, tokens in zip(rows, tokenised, strict=True):
        score = 0.0
        for word in words:
            df = frequency[word]
            tf = tokens.count(word) if df else 0
            if not tf:
                continue
            idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            norm = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * len(tokens) / average)
            score += idf * tf * (_BM25_K1 + 1.0) / norm
        scored.append((row, score))
    return sorted(scored, key=lambda pair: (-pair[1], str(pair[0].get("doc_id", ""))))


def cosine(query_vector: Sequence[float], stored: Any) -> float | None:
    """Cosine similarity of two vectors, or ``None`` if the stored one is unusable.

    Unusable: not a numeric sequence, a different width from the query, a zero
    or non-finite norm. The caller skips such a row, so one bad vector costs
    that row and not the axis.
    """
    try:
        a = np.asarray(query_vector, dtype=float)
        b = np.asarray(stored, dtype=float)
    except (TypeError, ValueError):
        return None
    if a.ndim != 1 or b.ndim != 1 or a.shape != b.shape or not a.size:
        return None
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if not math.isfinite(norm) or norm == 0.0:
        return None
    value = float(np.dot(a, b)) / norm
    return value if math.isfinite(value) else None


def _permitted(
    document_store: DocumentStore, rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``rows`` the public gate permits: the predicate's second, independent check."""
    gate = DocumentClearance(document_store, "public")
    return [r for r in rows if gate.permits(r.get("doc_id", ""), r.get("metadata"))]


def read_public_documents(
    document_store: DocumentStore, *, filters: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The whole documents labelled ``public``, newest first, never a chunk.

    At most :data:`PUBLIC_ROW_CEILING`. ``filters`` is the caller's own store
    filter (it narrows; the label always wins a key clash).
    """
    rows = document_store.list_documents(
        limit=PUBLIC_ROW_CEILING,
        include_chunks=False,
        filters={**(filters or {}), **public_label_filter()},
    )
    return _permitted(document_store, rows)


def public_keyword_rows(
    document_store: DocumentStore,
    query: str,
    *,
    filters: dict[str, Any] | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """The public documents matching ``query``, scored by local BM25, best first.

    The one keyword read of the public path: packs, ``GET /search`` and
    ``GET /documents?q`` all call it. The store is asked only for public
    rows (the label is a query predicate), the store's own ``rank`` is
    discarded, and the order comes from :func:`bm25_rank` over those rows.
    """
    found = document_store.search(
        query,
        limit=PUBLIC_ROW_CEILING,
        filters={**(filters or {}), **public_label_filter()},
        include_chunks=False,
    )
    return bm25_rank(query, _permitted(document_store, found))


__all__ = [
    "PUBLIC_ROW_CEILING",
    "bm25_rank",
    "cosine",
    "public_keyword_rows",
    "public_label_filter",
    "query_words",
    "read_public_documents",
]
