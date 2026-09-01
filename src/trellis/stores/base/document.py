"""DocumentStore — abstract interface for document storage."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


def chunk_id_like_pattern() -> str:
    """SQL ``LIKE`` pattern matching the doc id of a chunk document.

    Chunk ids are ``f"{parent_doc_id}{CHUNK_ID_SEPARATOR}{index}"``, and the
    separator holds no ``LIKE`` metacharacter, so the pattern needs no
    escaping. Imported lazily because :mod:`trellis.ingest_corpus` imports
    this module — the constant's home stays with the writer that mints the
    ids rather than being copied into the store layer, where a second copy
    could drift from the first.

    Verified against the reference deployment: over 1,317 documents the id
    marker and the ``metadata.chunk_index`` bookkeeping disagree on **zero**
    rows, so the cheap portable predicate and the metadata one select the
    same set. The id is preferred because it is immutable — a metadata
    rewrite cannot make a chunk stop looking like one.

    The converse is not guarded, and the guarantee should be read narrowly.
    ``%#chunk-%`` matches the substring anywhere in the id, and no writer
    validates that a caller-supplied ``doc_id`` avoids it, so a memory
    stored with ``#chunk-`` in its id is invisible to every surface that
    excludes chunks by default (#396 widened that from one REST route to
    four). Every ``#chunk-`` id written by production code is minted by
    :func:`trellis.ingest_corpus.sync.sync_records` (tests mint their own),
    so this is reachable only by a caller naming its own id — but it is
    reachable, silently.
    """
    from trellis.ingest_corpus.models import CHUNK_ID_SEPARATOR  # noqa: PLC0415

    return f"%{CHUNK_ID_SEPARATOR}%"


def chunk_exclusion_clause(
    placeholder: str, *, include_chunks: bool
) -> tuple[str, list[Any]]:
    """``(sql, params)`` for a standalone ``WHERE`` excluding chunk rows.

    Empty on both halves when ``include_chunks`` is true, so a caller
    interpolates the fragment and splats the params unconditionally. The
    fragment carries its own leading space and no trailing one — the two
    backends differ only in ``placeholder`` (``?`` vs ``%s``), and the
    predicate itself must not.

    For a query that already has a ``WHERE`` (full-text search), append
    ``doc_id NOT LIKE <placeholder>`` to the existing condition list with
    :func:`chunk_id_like_pattern` instead; there is no fragment to reuse
    and pretending otherwise would need this helper to know about the
    caller's clause structure.
    """
    if include_chunks:
        return "", []
    return f" WHERE doc_id NOT LIKE {placeholder}", [chunk_id_like_pattern()]


def encode_filter_value(key: str, value: Any) -> str:
    """JSON-encode one metadata filter value, naming the key on failure.

    Both backends compare a filter against stored JSON, so both need the
    filter value encoded the same way, and both would otherwise raise
    ``TypeError: Object of type set is not JSON serializable`` from inside
    a query builder with no indication of *which* filter caused it.

    A non-serialisable filter value used to fail silently and
    differently on each backend — SQLite compared it in Python and
    matched nothing, Postgres skipped the branch and matched everything
    (#409). Raising is the point; naming the key is what makes it
    actionable.
    """
    try:
        return json.dumps(value)
    except TypeError as exc:
        message = (
            f"metadata filter {key!r} has a value that is not JSON-"
            f"serialisable ({type(value).__name__}); document metadata is "
            f"JSON, so a filter over it must be too"
        )
        raise TypeError(message) from exc


class DocumentStore(ABC):
    """Abstract interface for document storage.

    Documents are raw content items (notes, files, transcripts, etc.)
    with metadata and optional full-text search.
    """

    @abstractmethod
    def put(
        self,
        doc_id: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        preserve_updated_at: bool = False,
    ) -> str:
        """Store or update a document.

        Auto-generates an ID if *doc_id* is ``None``.

        Args:
            doc_id: Document id, or ``None`` to auto-generate one.
            content: Document content.
            metadata: Free-form metadata mapping.
            preserve_updated_at: When ``True`` and the row already exists,
                its ``updated_at`` is left untouched instead of being set to
                now. **Pass it whenever the write does not change what the
                document says** — attaching derived metadata, stamping a
                lifecycle state, applying a tag. Ignored on insert (a new
                row's ``updated_at`` is its creation time).

                ``updated_at`` is a **public column with an open set of
                readers**, and that is why the rule is stated as an invariant
                rather than a list. Three successive enumerations of "who
                reads this" have each been wrong, and each time a metadata-only
                write shipped without the flag because its author checked the
                list and concluded the site was unaffected:

                * the keyword axis decays relevance off it
                  (:class:`~trellis.retrieve.strategies.KeywordSearch`), which
                  softens a bump via ``RECENCY_FLOOR``;
                * :mod:`trellis.mutate.retention` gates ``older_than_days`` on
                  it, so a bump shields a row from pruning;
                * :func:`trellis.retrieve.file_context._newest_timestamp`
                  builds ``newest_item_at``, which the #307 read hook compares
                  against a file's mtime — **a gate, not a score**. A bump
                  makes memory look newer than the file, so the hook injects
                  the stale context it exists to suppress, with no floor to
                  soften it. That surface reads ``list_documents`` directly, so
                  neither ``exclude_archived`` nor ``exclude_noise`` masks it.

                Do not extend that list in place of thinking. **The question
                is never "which reader would notice?" — it is "did the content
                change?"** If it did not, pass the flag.

        Returns:
            The document ID.
        """

    @abstractmethod
    def get(self, doc_id: str) -> dict[str, Any] | None:
        """Retrieve a document by ID.

        Returns:
            Document dict ``{doc_id, content, content_hash, metadata,
            created_at, updated_at}`` or ``None``.
        """

    @abstractmethod
    def delete(self, doc_id: str) -> bool:
        """Delete a document.  Returns ``True`` if it existed."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
        include_chunks: bool = True,
    ) -> list[dict[str, Any]]:
        """Full-text search with optional metadata filters.

        Returns a list of matching documents with a ``rank`` key.

        ``include_chunks=False`` excludes chunk documents. See
        :meth:`list_documents` for why the exclusion is pushed down here
        rather than applied to the result.

        **Every filter is a query predicate, never a pass over the
        returned page** — the same rule, for the same reason (#409). A
        ``limit=N`` search whose filter matches at least N documents
        returns N rows; a caller who gets fewer has reached the end of
        the matches, not the end of the ``LIMIT`` window. SQLite used to
        push scalars and ``content_tags`` into ``WHERE`` and apply
        everything else (lists, dicts, ``None``) to the rows ``LIMIT``
        had already returned, so such a search returned a short page —
        possibly empty — with matching documents sitting just past the
        window. Postgres did not filter those shapes *at all*, so the two
        backends silently disagreed about what a non-scalar filter meant.

        Filter semantics, pinned by
        ``DocumentStoreContractTests`` and identical on every backend:

        * ``filters["content_tags"]`` is the facet bag, with its own
          per-facet operator grammar and default-pass rules.
        * Every other key matches when the document's metadata value for
          that key is **equal to** the filter value under Python ``==``
          over the parsed JSON — order-insensitive for objects,
          order-sensitive for arrays, numeric-normalising for numbers.
          One documented exception, because it is measured rather than
          assumed: a **bool filter against a stored number, or a number
          filter against a stored bool**, matches on SQLite (whose callback
          is literally Python ``==``, and ``True == 1``) and does not match
          on Postgres (``'true'::jsonb = '1'::jsonb`` is false). The
          contract suite pins same-typed comparisons only, so nothing
          catches a caller who relies on the cross-type case; do not.
          Found by the #455 review gate and left unresolved deliberately —
          picking a side changes behaviour on one backend, and no in-repo
          caller issues a cross-type filter.
        * A filter value of ``None`` matches a document whose metadata
          omits the key as well as one storing an explicit JSON null.
        * A filter value that is not JSON-serialisable raises. It used to
          match nothing on SQLite and everything on Postgres.
        """

    @abstractmethod
    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        include_chunks: bool = True,
    ) -> list[dict[str, Any]]:
        """Paginated listing of documents.

        ``include_chunks=False`` excludes chunk documents — the
        ``<parent>#chunk-N`` fragments corpus ingestion writes alongside
        the parent they were sliced from, which on the reference
        deployment are 740 of 1,317 rows (56%).

        The exclusion is a store-level predicate rather than a filter over
        the returned page **because it has to be for the page to mean
        anything**: dropping chunks from a ``limit=50`` read of a corpus
        that is 56% chunks yields ~22 rows, and a caller who asked for 50
        and got 22 cannot tell a short page from the end of the data.
        Defaults to ``True`` so every existing caller — the reindex and
        resync walkers, retention, corpus prune — keeps seeing every row
        it needs to act on.
        """

    @abstractmethod
    def count(self, *, include_chunks: bool = True) -> int:
        """Total number of stored documents.

        ``include_chunks`` must match the :meth:`list_documents` call it
        is reported beside; a total that counts rows the listing excludes
        is the same defect one layer up.
        """

    @abstractmethod
    def get_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        """Get a document by its content hash (for deduplication)."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
