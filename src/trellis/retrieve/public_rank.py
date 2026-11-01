"""The public-only read: which rows it ranks, and the scores it ranks them by.

A caller that reads only ``public`` documents must receive nothing that
depends on a document it may not read. A strategy that ranks the whole corpus
cannot promise that, however the page is cut afterwards: a hidden row can
outrank a public one, and a hidden row moves the corpus statistics (document
frequency, vector width) every score is computed from. So the public path
never ranks, matches or counts a hidden row, and does not search the store. The
label filter still scans hidden rows, so the time a read takes grows with them, but
status and body do not: a row whose metadata the store cannot read does not match
the filter (:class:`~trellis.stores.base.document.ReadableMatch`), is withheld and
warned about, and does not fail the read, whatever the query holds (a public row
whose own text columns are not UTF-8 still does; see the operations guide):

* **Which rows.** One read, :func:`read_public_documents`: the newest
  :data:`PUBLIC_ROW_CEILING` documents the label predicate selects
  (:func:`public_label_filter` handed to ``DocumentStore.list_documents``). A
  hidden row is not in the result, whatever its text holds, and the ceiling
  counts public rows only. :class:`~trellis.retrieve.sensitivity.
  DocumentClearance` then judges every row a second time, with the same
  predicate, so the filter can only narrow what is served.
* **The keyword axis matches and scores locally**, over those rows and no
  others (:func:`public_keyword_rows`). It does not call the store's
  ``search``: a full-text index covers every row, so a query word would be
  looked up among hidden rows (a corrupt hidden row that holds the word would
  answer differently from one that does not), and past the ceiling the store's
  own rank would choose which public rows are captured from document frequencies
  that include hidden ones. A document matches when it holds any query word,
  as the stores' full-text query reads (any term); the score is BM25 with ``N``
  and document frequency taken over the public rows read.
* **The semantic axis** scores each public row's own stored vector against the
  query vector (:func:`cosine`). The vector store's ``query`` is never called,
  because it sees the whole corpus (and filters after an over-fetch on two
  backends). Ties break on ``doc_id``.
* **A process-wide limit** (:func:`public_read_slot`) admits at most
  :data:`MAX_CONCURRENT_PUBLIC_READS` public reads at once and refuses the rest
  at once, because a public read costs the same however narrow the question.

This module is a leaf (no import of ``strategies``), so the strategies' public
methods can use it. Nothing here runs for a caller above ``public``.
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import numpy as np

from trellis.errors import PublicReadBusyError
from trellis.retrieve.sensitivity import DocumentClearance
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    public_label_record,
)
from trellis.stores.base.document import ReadableMatch

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from trellis.stores.base.document import DocumentStore

#: The most public rows one read holds: the newest ones. It counts public rows
#: only (the label is a query predicate, so no hidden row can reach it), so
#: whether it bites, and which rows it keeps, are functions of what the key may
#: read. Above it, older public documents are out of reach of the keyword and
#: semantic reads and a count is a clip (``total_exact`` is ``false``).
PUBLIC_ROW_CEILING = 2000

#: The most public reads one process runs at once. A public read lists and
#: tokenises up to :data:`PUBLIC_ROW_CEILING` whole documents, which is the same
#: work for any question, so an unbounded number of them starves every other
#: caller of the process. Over the limit a read is refused at once
#: (:class:`~trellis.errors.PublicReadBusyError`), never queued.
MAX_CONCURRENT_PUBLIC_READS = 2

#: ``Retry-After`` seconds a refused public read is told.
PUBLIC_READ_RETRY_AFTER_SECONDS = 1

#: BM25 constants (the usual defaults).
_BM25_K1 = 1.2
_BM25_B = 0.75

#: The query words, as the stores read them: ASCII alphanumerics, at most 20.
_WORD = re.compile(r"[a-z0-9]+")
_MAX_QUERY_WORDS = 20

_in_flight = 0
_in_flight_lock = threading.Lock()
_held = threading.local()


@contextmanager
def public_read_slot() -> Iterator[None]:
    """Hold one of the process's :data:`MAX_CONCURRENT_PUBLIC_READS` slots.

    Refuses at once with :class:`~trellis.errors.PublicReadBusyError` when every
    slot is held. Re-entrant per thread: a public read that calls another (a
    pack build that reads the same rows twice) holds one slot, so a nested call
    never refuses itself. Only public reads take a slot, so the refusal depends
    on public traffic alone.
    """
    global _in_flight  # noqa: PLW0603 - the process-wide counter is the point
    depth = getattr(_held, "depth", 0)
    if depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    with _in_flight_lock:
        if _in_flight >= MAX_CONCURRENT_PUBLIC_READS:
            msg = (
                "this key's public read was refused: the server is already "
                f"running {MAX_CONCURRENT_PUBLIC_READS} reads for keys that read "
                "only public documents; retry shortly"
            )
            raise PublicReadBusyError(msg, retry_after=PUBLIC_READ_RETRY_AFTER_SECONDS)
        _in_flight += 1
    _held.depth = 1
    try:
        yield
    finally:
        _held.depth = 0
        with _in_flight_lock:
            _in_flight -= 1


def public_label_filter() -> dict[str, Any]:
    """The metadata filter that selects documents labelled ``public``.

    The label is one top-level key holding a whole record, and a store filter is
    equality over a key's value, so the filter is
    :func:`~trellis.schemas.classification.public_label_record` under its key:
    the same record, from the same function, that
    :meth:`~trellis.retrieve.sensitivity.DocumentClearance.permits` tests with
    :func:`~trellis.schemas.classification.is_public_label`, so the store and
    the gate cannot disagree about what "public" is. A document whose record
    reads ``public`` and carries anything more (regulatory tags, a jurisdiction,
    another classifier) or less (bare frontmatter) does not match and is not
    served: the predicate fails closed. A row whose metadata cannot be read at all
    does not match either (the value is a ``ReadableMatch``), instead of failing the
    read. ``tests/unit/retrieve/
    test_public_label_pin.py`` pins a literal record against both.
    """
    return {DATA_CLASSIFICATION_KEY: ReadableMatch(public_label_record())}


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
    holding none of the query words scores zero and sorts last.
    """
    words = query_words(query)
    counts = [
        Counter(_WORD.findall((row.get("content") or "").lower())) for row in rows
    ]
    total = len(rows)
    if not total:
        return []
    lengths = [sum(c.values()) for c in counts]
    average = sum(lengths) / total or 1.0
    frequency = {w: sum(1 for c in counts if w in c) for w in words}
    scored: list[tuple[dict[str, Any], float]] = []
    for row, count, length in zip(rows, counts, lengths, strict=True):
        score = 0.0
        for word in words:
            tf = count.get(word, 0)
            if not tf:
                continue
            df = frequency[word]
            idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            norm = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * length / average)
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


class PublicRead:
    """One call's read of the public documents, taken once and shared.

    The keyword and semantic axes of a pack ask for the same rows, so a pack
    reads them once. The read happens on first use (a failure surfaces in the
    axis that asked, not at construction) and a failed read is not remembered.
    """

    def __init__(
        self,
        document_store: DocumentStore | None,
        *,
        filters: dict[str, Any] | None = None,
    ) -> None:
        self._store = document_store
        self._filters = filters
        self._rows: list[dict[str, Any]] | None = None
        self._truncated = False

    @property
    def rows(self) -> list[dict[str, Any]]:
        """The public documents, newest first, at most the ceiling.

        A builder with no document store has nothing it can label, read or
        rebuild, so it reads no row (and a pack from it serves none).
        """
        if self._rows is None:
            store = self._store
            if store is None:
                self._rows = []
                return self._rows
            with public_read_slot():
                found = store.list_documents(
                    limit=PUBLIC_ROW_CEILING,
                    include_chunks=False,
                    filters={**(self._filters or {}), **public_label_filter()},
                )
                self._rows = _permitted(store, found)
            self._truncated = len(found) >= PUBLIC_ROW_CEILING
        return self._rows

    @property
    def truncated(self) -> bool:
        """Whether the read reached the ceiling, so older public rows may exist."""
        _ = self.rows
        return self._truncated


def read_public_documents(
    document_store: DocumentStore, *, filters: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The whole documents labelled ``public``, newest first, never a chunk.

    At most :data:`PUBLIC_ROW_CEILING`. ``filters`` is the caller's own store
    filter (it narrows; the label always wins a key clash).
    """
    return PublicRead(document_store, filters=filters).rows


def public_keyword_rows(
    document_store: DocumentStore,
    query: str,
    *,
    filters: dict[str, Any] | None = None,
    read: PublicRead | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """The public documents matching ``query``, scored by local BM25, best first.

    The one keyword read of the public path: packs, ``GET /search`` and
    ``GET /documents?q`` all call it. It reads the public rows
    (:class:`PublicRead`, or the caller's own so two axes share one read) and
    matches them here: a document is a match when it holds any of the query's
    words, and the order is :func:`bm25_rank` over every public row read. The
    store's ``search`` is never called, so nothing a hidden row holds is looked
    up and the answer is a function of the public rows alone.
    """
    with public_read_slot():
        rows = (read or PublicRead(document_store, filters=filters)).rows
        return [(row, score) for row, score in bm25_rank(query, rows) if score > 0.0]


__all__ = [
    "MAX_CONCURRENT_PUBLIC_READS",
    "PUBLIC_READ_RETRY_AFTER_SECONDS",
    "PUBLIC_ROW_CEILING",
    "PublicRead",
    "bm25_rank",
    "cosine",
    "public_keyword_rows",
    "public_label_filter",
    "public_read_slot",
    "query_words",
    "read_public_documents",
]
