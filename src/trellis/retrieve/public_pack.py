"""What a public-only caller (an ``external`` API key) is served, and from where.

A key that reads only ``public`` documents must receive nothing that depends on
a document it may not read. The ordinary pipeline cannot promise that: it
collects first and filters later, so the items it carries, the counts it
derives and the axes it reports are all shaped by the whole corpus. The public
path is therefore its own pipeline (``PackBuilder._build_public`` and
``_build_public_sectioned``), and this module holds the pieces it is made of:

* :func:`readable_stored_copy` -- a collected item, re-read from its stored
  document row. A vector row's text and metadata are an embed-time snapshot
  that can name text the document no longer holds, so a public-only caller is
  served the stored row's text, cut by :func:`truncate_excerpt` exactly as the
  keyword axis cuts it, and never what the strategy carried.
* :func:`project_for_public` -- the item as it is served: metadata cut to the
  allowlist in :data:`~trellis.retrieve.sensitivity.PUBLIC_METADATA_KEYS`, and
  the raw per-strategy score dropped (it is a corpus statistic).

Nothing here runs for a caller above ``public``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.retrieve.authority_rank import AUTHORITY_BOOST_KEY
from trellis.retrieve.excerpts import truncate_excerpt
from trellis.retrieve.feedback_prior import EVIDENCE_KEYS as FEEDBACK_PRIOR_KEYS
from trellis.retrieve.feedback_prior import FEEDBACK_PRIOR_KEY
from trellis.retrieve.parent_verdicts import ParentVerdictCache
from trellis.retrieve.public_rank import (
    PUBLIC_ROW_CEILING,
    public_keyword_rows,
    public_label_filter,
    read_public_documents,
)
from trellis.retrieve.reference import served_text
from trellis.retrieve.sensitivity import item_readable, public_metadata
from trellis.retrieve.strategies import RECENCY_CLOCK_METADATA_KEY
from trellis.schemas.pack import PackItem

if TYPE_CHECKING:
    from trellis.schemas.classification import Sensitivity
    from trellis.stores.base.document import DocumentStore

#: Per-strategy stamps the collect seam wrote onto the item. They describe the
#: strategy, not the document, so they survive the refresh from the stored row
#: and are dropped again by :func:`project_for_public` once they have been
#: promoted onto ``strategy_source``.
_STRATEGY_STAMP_KEYS: tuple[str, ...] = ("source_strategy", RECENCY_CLOCK_METADATA_KEY)


def readable_stored_copy(
    clearance: Sensitivity,
    item: PackItem,
    cache: ParentVerdictCache,
    *,
    excerpt_max_chars: int,
) -> PackItem | None:
    """``item`` rebuilt from its stored document row, or ``None`` if unreadable.

    ``None`` for anything :func:`item_readable` withholds from a public-only
    caller: an item that is not a whole stored document, a chunk, a row with no
    stored document, or a document not labelled ``public``. Otherwise the copy
    carries the stored row's text and metadata in place of whatever the
    strategy found, keeping only the strategy's own stamps.
    """
    if not item_readable(clearance, item, cache):
        return None
    stored = cache.stored_row(item.item_id)
    if stored is None:
        return None
    content, metadata = stored
    stamps = {
        key: item.metadata[key] for key in _STRATEGY_STAMP_KEYS if key in item.metadata
    }
    # A stamp is the strategy's, never the row's: a stored key of the same name
    # is dropped, or ``project_for_public`` would promote it onto
    # ``strategy_source`` for an item no strategy stamped.
    row_metadata = {k: v for k, v in metadata.items() if k not in _STRATEGY_STAMP_KEYS}
    return item.model_copy(
        update={
            "excerpt": truncate_excerpt(
                served_text(content, metadata), limit=excerpt_max_chars
            ),
            "metadata": {**row_metadata, **stamps},
        }
    )


def public_search_page(
    document_store: DocumentStore,
    query: str,
    *,
    limit: int,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Up to ``limit`` stored documents labelled ``public`` that match ``query``.

    The one keyword read of the public path (see
    :func:`~trellis.retrieve.public_rank.public_keyword_rows`): the store is
    asked to list public documents, never to search, and the match and the
    order (BM25 over those rows, ties by ``doc_id``) are computed here. A chunk
    is never a result. Each row's ``rank`` is the
    local score, which :func:`~trellis.retrieve.sensitivity.conceal_document_rows`
    drops before anything is served.
    """
    rows = public_keyword_rows(document_store, query, filters=filters)
    return [{**row, "rank": score} for row, score in rows[:limit]]


#: ``score_breakdown`` keys a public-only caller never receives.
_PRIVATE_BREAKDOWN_KEYS: frozenset[str] = frozenset(
    {"original_score", AUTHORITY_BOOST_KEY} | FEEDBACK_PRIOR_KEYS
)
#: Multipliers that scale ``relevance_score`` after fusion. An item carrying
#: one also loses its ``rrf_*`` keys (see :func:`project_for_public`).
_SCORE_MULTIPLIER_KEYS: frozenset[str] = frozenset(
    {AUTHORITY_BOOST_KEY, FEEDBACK_PRIOR_KEY}
)
#: Prefix of the RRF reranker's per-strategy and total keys
#: (:class:`~trellis.retrieve.rerankers.rrf.RRFReranker`).
_RRF_KEY_PREFIX = "rrf_"


def project_for_public(item: PackItem) -> PackItem:
    """``item`` as a public-only caller receives it.

    Metadata is cut to the allowlist and the strategy stamp is promoted onto
    ``strategy_source`` first, because the reranker and the response read the
    field, not the metadata key. ``score_breakdown["original_score"]`` is the
    raw strategy score (a keyword score is a function of corpus statistics);
    the fused rank-based score stays. ``score_breakdown["authority_boost"]``
    is dropped too: its value names which authority signal an item carries.
    So are the feedback-prior keys: they count how other callers graded it.

    Dropping a multiplier's key is not enough on its own, because
    ``relevance_score`` stays scaled: ``relevance_score / rrf_total`` would
    be the multiplier, and the per-strategy ``rrf_*`` keys sum to
    ``rrf_total``. So an item that carries an applied multiplier (an
    authority boost, or a feedback-prior factor under ``on``; a ``shadow``
    factor scales nothing) also loses every ``rrf_*`` key. An item with no
    multiplier keeps them, so with both off the projection is unchanged.
    """
    source = item.strategy_source or (item.metadata or {}).get("source_strategy")
    scaled = not _SCORE_MULTIPLIER_KEYS.isdisjoint(item.score_breakdown)
    breakdown = {
        k: v
        for k, v in item.score_breakdown.items()
        if k not in _PRIVATE_BREAKDOWN_KEYS
        and not (scaled and k.startswith(_RRF_KEY_PREFIX))
    }
    return item.model_copy(
        update={
            "metadata": public_metadata(item.metadata),
            "strategy_source": source if isinstance(source, str) else None,
            "score_breakdown": breakdown,
        }
    )


__all__ = [
    "PUBLIC_ROW_CEILING",
    "project_for_public",
    "public_label_filter",
    "public_search_page",
    "read_public_documents",
    "readable_stored_copy",
]
