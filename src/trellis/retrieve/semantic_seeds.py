"""Semantic-seed extraction for paraphrased retrieval intents.

The literal seed extractors in the eval corpus loaders
(:func:`eval.corpora.dbt_loader.extract_seed_ids`,
:func:`eval.corpora.github_trellis.loader.build_pr_name_index` →
``extract_seed_ids``) match short-names and unique title phrases by
**word-boundary substring**. Paraphrased intents — e.g.,
``"Phase 1 through Phase 4 PRs that shipped scenarios 5.1, 5.2, 5.3"``
— have no literal anchor on PR titles like ``"Eval Phase 2: scenario
5.2 — multi-backend equivalence"``. The literal extractor returns
``[]`` for such intents and the downstream :class:`GraphSearch` has
no seeds to expand from.

:class:`SemanticSeedExtractor` closes that gap by:

1. Embedding the intent at query time (via the same ``embedding_fn``
   :class:`~trellis.retrieve.strategies.SemanticSearch` uses).
2. Querying the vector store for the top-K most-similar items.
3. Filtering hits to entity-summary documents (so noise / feedback
   / non-entity content cannot contaminate the seed set).
4. Returning the underlying ``entity_id`` for each surviving hit —
   ready to union with literal seeds and pass as ``filters["seed_ids"]``
   to :class:`GraphSearch`.

The extractor is **deliberately additive** — it composes with the
literal seed path rather than replacing it. The literal path stays
the right tool for intents that mention short-names verbatim; the
semantic path catches paraphrases. PackBuilder dedups the resulting
candidates by ``item_id`` so the union pays no double-counting cost.

No fallback paths (greenfield writer contract — see CLAUDE.md):

* Missing ``embedding_fn`` raises at construction.
* Empty top-K returns ``[]``; the caller composes with other seed
  sources rather than the extractor papering over a miss.

Why a class (not a free function): caching. Embedding the same intent
across multiple pack assemblies in a single scenario run is wasteful;
:class:`SemanticSeedExtractor` keeps an in-memory ``intent → entity_ids``
cache scoped to the instance lifetime so the per-round cost is one
embed-then-query the first time and a dict lookup thereafter.

Companion to the literal :func:`extract_seed_ids`. See
``TODO.md`` (2026-05-08 "GitHub corpus seed-extraction follow-ups —
semantic-seed extraction") for the architectural rationale and the
multi_pr_series Q1 case the extractor unblocks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


#: Default for ``top_k`` when callers don't override.
#:
#: Tuned against the github multi_pr_series Q1 ground-truth intent
#: ("Phase 1 through Phase 4 PRs that shipped scenarios 5.1, 5.2,
#: 5.3" → 4 PRs). Paraphrased intents in the trellis-ai eval corpora
#: typically reference 2-4 entities (a small named series, a tightly
#: related cluster). 5 gives one slot of headroom past the largest
#: observed series while keeping the noise floor low: each additional
#: K-slot is one more item that risks anchoring a false-positive
#: graph traversal. Callers with intents that span larger sets (a
#: dozen-plus PR backlog query, a "show me everything tagged X"
#: intent) should override.
DEFAULT_TOP_K = 5


#: Metadata key used to identify entity-summary documents in the
#: vector store. Both eval corpus loaders
#: (:mod:`eval.corpora.github_trellis.loader`,
#: :mod:`eval.corpora.dbt_loader`) stamp ``content_type="entity_summary"``
#: when they index per-entity docs. Filtering on this key prevents
#: feedback-derived documents, advisory notes, or non-entity content
#: from being elevated to graph-traversal seeds.
ENTITY_SUMMARY_CONTENT_TYPE = "entity_summary"


#: Doc-id prefix used by both eval corpus loaders. Vector store
#: ``item_id`` values are stored as ``doc:<entity_id>`` so the doc
#: store and vector store key on the same string. The extractor
#: strips this prefix to surface the underlying ``entity_id`` that
#: :class:`GraphSearch` accepts as a seed.
_DOC_ID_PREFIX = "doc:"


class SemanticSeedExtractor:
    """Embed an intent and surface the top-K entity_ids as graph seeds.

    Args:
        vector_store: Any object exposing
            ``query(vector, top_k, filters) -> list[{item_id, score, metadata}]``.
            Same surface as the :class:`~trellis.retrieve.strategies.SemanticSearch`
            consumer.
        embedding_fn: Synchronous ``callable(str) -> list[float]``. The
            same shape :class:`~trellis.retrieve.strategies.SemanticSearch`
            uses; eval scenarios bridge the async :class:`EmbedderClient`
            via :func:`eval.scenarios._telemetry._make_embedding_fn`.
        top_k: Number of vector-store hits to consider per intent.
            Defaults to :data:`DEFAULT_TOP_K`. Hits that survive the
            entity-summary filter are returned; hits filtered out do
            **not** trigger an over-fetch — they reduce the surviving
            count.
        cache_size: Maximum number of distinct intents to cache. ``0``
            disables caching (every call re-embeds + re-queries). The
            cache is a process-instance LRU; the same extractor instance
            shared across rounds amortises the per-intent cost. Default
            64 is enough for the largest eval scenario (24 rounds x ~12
            distinct intents) plus headroom.

    Raises:
        ValueError: If ``embedding_fn`` is ``None`` (greenfield writer
            contract — silent fallbacks are forbidden, see CLAUDE.md).
    """

    def __init__(
        self,
        vector_store: Any,
        embedding_fn: Callable[[str], list[float]],
        *,
        top_k: int = DEFAULT_TOP_K,
        cache_size: int = 64,
    ) -> None:
        if embedding_fn is None:
            msg = (
                "SemanticSeedExtractor requires an embedding_fn — "
                "silent fallbacks are forbidden under the greenfield "
                "writer contract (CLAUDE.md). Pass the same callable "
                "you would pass to SemanticSearch."
            )
            raise ValueError(msg)
        if top_k < 1:
            msg = f"top_k must be >= 1, got {top_k!r}"
            raise ValueError(msg)
        if cache_size < 0:
            msg = f"cache_size must be >= 0, got {cache_size!r}"
            raise ValueError(msg)
        self._store = vector_store
        self._embedding_fn = embedding_fn
        self._top_k = top_k
        self._cache_size = cache_size
        # Insertion-ordered dict for FIFO eviction (good enough for an
        # eval-loop cache; an LRU would add cost without changing the
        # observed hit pattern — every distinct intent appears
        # round-after-round, so FIFO eviction only kicks in after the
        # working set grows past cache_size).
        self._cache: dict[str, list[str]] = {}

    @property
    def top_k(self) -> int:
        return self._top_k

    def extract(self, intent: str) -> list[str]:
        """Return entity_ids whose summary docs are most similar to *intent*.

        Pipeline:

        1. Cache lookup on the raw intent string. Hits return the
           previously computed list (insertion order preserved).
        2. Cache miss: ``embedding_fn(intent)`` → query vector.
        3. ``vector_store.query(vector, top_k=self._top_k)`` → list of
           ``{item_id, score, metadata}`` ordered by descending similarity.
        4. Filter to hits with ``metadata.content_type ==
           "entity_summary"``. Hits without that tag are dropped; they
           are not entity-anchored content and should not seed graph
           traversal.
        5. Strip the ``doc:`` prefix from each surviving ``item_id``
           (or read ``metadata["entity_id"]`` when the prefix is
           absent — both loaders stamp it as a defensive fallback).
        6. Deduplicate while preserving similarity-rank order.
        7. Cache and return.

        Returns ``[]`` when:

        * The vector store has no entity-summary docs (corpus not
          loaded / no embeddings written).
        * Every top-K hit fails the entity-summary filter.
        * The intent embeds but matches nothing above the store's
          similarity floor.

        Empty results are valid — they mean "the semantic path
        contributed no seeds for this intent". The caller composes
        with literal seeds (which may carry the answer themselves)
        or proceeds with no-seed fallbacks at the strategy level.
        """
        if self._cache_size > 0:
            cached = self._cache.get(intent)
            if cached is not None:
                # Refresh insertion order on hit so a frequently-asked
                # intent doesn't drift toward eviction. Move-to-end is
                # the FIFO-with-LRU-bias behavior the cache eviction
                # test relies on.
                del self._cache[intent]
                self._cache[intent] = cached
                return list(cached)

        vector = self._embedding_fn(intent)
        try:
            hits = self._store.query(vector, top_k=self._top_k)
        except Exception:
            # Vector-store failures are logged but never block pack
            # assembly — the caller falls back to its other seed
            # sources. This mirrors PackBuilder's per-strategy
            # exception handling.
            logger.exception(
                "semantic_seed_extractor_query_failed",
                top_k=self._top_k,
                intent_preview=intent[:80],
            )
            return []

        seeds: list[str] = []
        seen: set[str] = set()
        for hit in hits:
            metadata = hit.get("metadata") or {}
            content_type = metadata.get("content_type")
            if content_type != ENTITY_SUMMARY_CONTENT_TYPE:
                continue
            entity_id = self._resolve_entity_id(hit, metadata)
            if entity_id is None or entity_id in seen:
                continue
            seeds.append(entity_id)
            seen.add(entity_id)

        if self._cache_size > 0:
            self._store_in_cache(intent, seeds)

        logger.debug(
            "semantic_seed_extractor_extracted",
            seeds_count=len(seeds),
            top_k=self._top_k,
            hits_returned=len(hits),
            intent_preview=intent[:80],
        )
        return seeds

    @staticmethod
    def _resolve_entity_id(
        hit: dict[str, Any], metadata: dict[str, Any]
    ) -> str | None:
        """Pull the underlying ``entity_id`` from a vector-store hit.

        Preference order:

        1. ``metadata["entity_id"]`` if present (both eval loaders
           stamp it; it is the canonical source).
        2. Strip the ``doc:`` prefix from ``hit["item_id"]`` (defensive
           fallback for callers that index without the explicit
           metadata stamp).

        Returns ``None`` when neither path yields a non-empty string —
        the hit then gets skipped silently. This preserves the
        "empty-is-valid" contract: a vector store populated with
        non-entity-summary content (or a corpus where the loader
        forgot to stamp the metadata) yields zero seeds rather than
        feeding ill-formed strings into ``filters["seed_ids"]``.
        """
        entity_id = metadata.get("entity_id")
        if isinstance(entity_id, str) and entity_id:
            return entity_id
        raw_id = hit.get("item_id", "")
        if not isinstance(raw_id, str) or not raw_id:
            return None
        if raw_id.startswith(_DOC_ID_PREFIX):
            stripped = raw_id[len(_DOC_ID_PREFIX) :]
            return stripped or None
        return raw_id

    def _store_in_cache(self, intent: str, seeds: list[str]) -> None:
        """Insert *seeds* into the FIFO cache, evicting the oldest if full."""
        if intent in self._cache:
            # Refresh insertion order so a re-asked intent doesn't
            # silently move toward eviction. Cheap on a 64-key dict.
            del self._cache[intent]
        elif len(self._cache) >= self._cache_size:
            # Pop the oldest entry to stay at-cap.
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[intent] = list(seeds)

    def cache_clear(self) -> None:
        """Drop every cached intent. Useful in tests and between corpora."""
        self._cache.clear()


__all__ = [
    "DEFAULT_TOP_K",
    "ENTITY_SUMMARY_CONTENT_TYPE",
    "SemanticSeedExtractor",
]
