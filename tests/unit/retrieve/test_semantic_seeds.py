"""Unit tests for :class:`trellis.retrieve.semantic_seeds.SemanticSeedExtractor`.

Covers the SEM-1 contract:

* Greenfield ``embedding_fn=None`` raises (no silent fallback).
* Top-K hits are filtered to entity-summary docs only.
* ``entity_id`` resolution prefers the metadata stamp over the
  ``doc:`` prefix strip; both are accepted.
* Caching avoids re-embedding repeat intents within an instance.
* Vector-store exceptions are swallowed (return ``[]``) — pack
  assembly never blocks on a degraded vector backend.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from trellis.retrieve.semantic_seeds import (
    DEFAULT_TOP_K,
    ENTITY_SUMMARY_CONTENT_TYPE,
    SemanticSeedExtractor,
)


def _hit(
    *,
    item_id: str,
    score: float,
    content_type: str = ENTITY_SUMMARY_CONTENT_TYPE,
    entity_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a vector-store hit dict in the shape SemanticSearch consumes."""
    metadata: dict[str, Any] = {"content_type": content_type}
    if entity_id is not None:
        metadata["entity_id"] = entity_id
    if extra_metadata:
        metadata.update(extra_metadata)
    return {"item_id": item_id, "score": score, "metadata": metadata}


class TestConstruction:
    def test_missing_embedding_fn_raises(self) -> None:
        with pytest.raises(ValueError, match="embedding_fn"):
            SemanticSeedExtractor(MagicMock(), None)  # type: ignore[arg-type]

    def test_zero_top_k_raises(self) -> None:
        with pytest.raises(ValueError, match="top_k"):
            SemanticSeedExtractor(
                MagicMock(), MagicMock(return_value=[0.1]), top_k=0
            )

    def test_negative_cache_size_raises(self) -> None:
        with pytest.raises(ValueError, match="cache_size"):
            SemanticSeedExtractor(
                MagicMock(),
                MagicMock(return_value=[0.1]),
                cache_size=-1,
            )

    def test_default_top_k_exposed(self) -> None:
        extractor = SemanticSeedExtractor(
            MagicMock(), MagicMock(return_value=[0.1])
        )
        assert extractor.top_k == DEFAULT_TOP_K


class TestExtraction:
    def _build(
        self,
        hits: list[dict[str, Any]],
        *,
        cache_size: int = 64,
        top_k: int = DEFAULT_TOP_K,
    ) -> tuple[SemanticSeedExtractor, MagicMock, MagicMock]:
        store = MagicMock()
        store.query.return_value = hits
        embed = MagicMock(return_value=[0.1, 0.2, 0.3])
        return (
            SemanticSeedExtractor(
                store, embed, top_k=top_k, cache_size=cache_size
            ),
            store,
            embed,
        )

    def test_returns_entity_ids_in_similarity_order(self) -> None:
        hits = [
            _hit(item_id="doc:e1", score=0.95, entity_id="e1"),
            _hit(item_id="doc:e2", score=0.80, entity_id="e2"),
            _hit(item_id="doc:e3", score=0.70, entity_id="e3"),
        ]
        extractor, _store, _embed = self._build(hits)
        seeds = extractor.extract("paraphrased intent text")
        assert seeds == ["e1", "e2", "e3"]

    def test_filters_non_entity_summary_hits(self) -> None:
        hits = [
            _hit(item_id="doc:e1", score=0.95, entity_id="e1"),
            _hit(
                item_id="doc:n1",
                score=0.90,
                content_type="feedback_note",
                entity_id="n1",
            ),
            _hit(item_id="doc:e2", score=0.80, entity_id="e2"),
        ]
        extractor, _store, _embed = self._build(hits)
        assert extractor.extract("intent") == ["e1", "e2"]

    def test_filters_when_content_type_missing(self) -> None:
        hits = [
            _hit(item_id="doc:e1", score=0.95, entity_id="e1"),
            {
                "item_id": "doc:no_meta",
                "score": 0.80,
                "metadata": {"entity_id": "no_meta"},  # no content_type
            },
        ]
        extractor, _store, _embed = self._build(hits)
        # Hit without content_type=entity_summary must not be elevated
        # to a graph seed; only the explicitly-tagged one survives.
        assert extractor.extract("intent") == ["e1"]

    def test_strips_doc_prefix_when_entity_id_metadata_missing(self) -> None:
        hits = [
            # Defensive fallback: caller forgot to stamp entity_id.
            _hit(item_id="doc:fallback_e", score=0.9, entity_id=None),
        ]
        extractor, _store, _embed = self._build(hits)
        assert extractor.extract("intent") == ["fallback_e"]

    def test_prefers_metadata_entity_id_over_item_id(self) -> None:
        hits = [
            # Metadata stamp wins even when the item_id would resolve to
            # a different (broken) string.
            _hit(item_id="doc:wrong", score=0.9, entity_id="canonical_e"),
        ]
        extractor, _store, _embed = self._build(hits)
        assert extractor.extract("intent") == ["canonical_e"]

    def test_deduplicates_repeat_entity_ids(self) -> None:
        # Two vector-store entries pointing at the same entity (e.g.,
        # mirrored docs across systems). Order preserved by first hit.
        hits = [
            _hit(item_id="doc:e1.a", score=0.95, entity_id="e1"),
            _hit(item_id="doc:e2", score=0.90, entity_id="e2"),
            _hit(item_id="doc:e1.b", score=0.85, entity_id="e1"),
        ]
        extractor, _store, _embed = self._build(hits)
        assert extractor.extract("intent") == ["e1", "e2"]

    def test_drops_hits_with_empty_entity_id_and_no_doc_prefix(self) -> None:
        hits = [
            {
                "item_id": "raw_id_no_prefix",
                "score": 0.9,
                "metadata": {
                    "content_type": ENTITY_SUMMARY_CONTENT_TYPE,
                    "entity_id": "",  # empty stamp
                },
            },
            _hit(item_id="doc:good", score=0.8, entity_id="good"),
        ]
        extractor, _store, _embed = self._build(hits)
        # Empty entity_id stamp falls back to item_id strip; that yields
        # ``raw_id_no_prefix`` (no doc: prefix → returned as-is). The
        # other hit returns "good".
        seeds = extractor.extract("intent")
        assert "good" in seeds
        # The fallback path is permissive: a non-empty raw item_id is
        # surfaced. Documenting this so a future tightening (e.g.,
        # require the prefix) doesn't silently break callers.
        assert "raw_id_no_prefix" in seeds

    def test_drops_hits_with_none_item_id_and_no_metadata_stamp(self) -> None:
        hits = [
            {
                "item_id": "",
                "score": 0.9,
                "metadata": {"content_type": ENTITY_SUMMARY_CONTENT_TYPE},
            },
            _hit(item_id="doc:good", score=0.8, entity_id="good"),
        ]
        extractor, _store, _embed = self._build(hits)
        # Empty item_id + missing entity_id → skipped.
        assert extractor.extract("intent") == ["good"]

    def test_empty_hits_returns_empty_list(self) -> None:
        extractor, _store, _embed = self._build([])
        assert extractor.extract("intent") == []

    def test_top_k_passed_to_store(self) -> None:
        extractor, store, _embed = self._build([], top_k=7)
        extractor.extract("intent")
        store.query.assert_called_once()
        _, kwargs = store.query.call_args
        assert kwargs.get("top_k") == 7

    def test_embedding_fn_receives_intent_verbatim(self) -> None:
        extractor, _store, embed = self._build([])
        extractor.extract("paraphrased intent text")
        embed.assert_called_once_with("paraphrased intent text")

    def test_vector_store_exception_returns_empty_seeds(self) -> None:
        store = MagicMock()
        store.query.side_effect = RuntimeError("vector backend down")
        extractor = SemanticSeedExtractor(
            store, MagicMock(return_value=[0.1, 0.2])
        )
        # Failure must not propagate — pack assembly continues with
        # other seed sources.
        assert extractor.extract("intent") == []


class TestCaching:
    def test_repeat_intent_uses_cache(self) -> None:
        store = MagicMock()
        store.query.return_value = [
            _hit(item_id="doc:e1", score=0.9, entity_id="e1"),
        ]
        embed = MagicMock(return_value=[0.1, 0.2])
        extractor = SemanticSeedExtractor(store, embed, cache_size=8)
        first = extractor.extract("repeat intent")
        second = extractor.extract("repeat intent")
        assert first == second == ["e1"]
        # Embedder called once for two extractions — cache is doing
        # its job.
        embed.assert_called_once()
        store.query.assert_called_once()

    def test_distinct_intents_re_embed(self) -> None:
        store = MagicMock()
        store.query.return_value = [
            _hit(item_id="doc:e1", score=0.9, entity_id="e1"),
        ]
        embed = MagicMock(return_value=[0.1])
        extractor = SemanticSeedExtractor(store, embed, cache_size=8)
        extractor.extract("intent A")
        extractor.extract("intent B")
        assert embed.call_count == 2

    def test_cache_size_zero_disables_cache(self) -> None:
        store = MagicMock()
        store.query.return_value = [
            _hit(item_id="doc:e1", score=0.9, entity_id="e1"),
        ]
        embed = MagicMock(return_value=[0.1])
        extractor = SemanticSeedExtractor(store, embed, cache_size=0)
        extractor.extract("intent")
        extractor.extract("intent")
        # Two embeds because caching is disabled.
        assert embed.call_count == 2

    def test_cache_eviction_at_capacity(self) -> None:
        store = MagicMock()
        store.query.return_value = [
            _hit(item_id="doc:e1", score=0.9, entity_id="e1"),
        ]
        embed = MagicMock(return_value=[0.1])
        extractor = SemanticSeedExtractor(store, embed, cache_size=2)
        extractor.extract("a")
        extractor.extract("b")
        # 'a' still cached at this point.
        extractor.extract("a")
        embed_calls_after_a_revisit = embed.call_count
        # Adding 'c' should evict the oldest (which is now 'b' since
        # 'a' got refreshed by the revisit).
        extractor.extract("c")
        # 'b' is evicted; revisiting 'b' triggers a re-embed.
        extractor.extract("b")
        assert embed.call_count == embed_calls_after_a_revisit + 2

    def test_cache_clear_drops_entries(self) -> None:
        store = MagicMock()
        store.query.return_value = [
            _hit(item_id="doc:e1", score=0.9, entity_id="e1"),
        ]
        embed = MagicMock(return_value=[0.1])
        extractor = SemanticSeedExtractor(store, embed, cache_size=8)
        extractor.extract("intent")
        extractor.cache_clear()
        extractor.extract("intent")
        assert embed.call_count == 2


class TestPipelineCompositionShape:
    """Sanity that the extractor's output is shaped to feed
    ``filters['seed_ids']`` directly (the consumer contract)."""

    def test_seeds_are_plain_strings(self) -> None:
        store = MagicMock()
        store.query.return_value = [
            _hit(item_id="doc:e1", score=0.9, entity_id="e1"),
            _hit(item_id="doc:e2", score=0.85, entity_id="e2"),
        ]
        embed = MagicMock(return_value=[0.1, 0.2])
        extractor = SemanticSeedExtractor(store, embed)
        seeds = extractor.extract("intent")
        assert all(isinstance(s, str) for s in seeds)
        # And ready to be unioned with literal seeds: type-stable list
        # that supports ordinary set operations downstream.
        literal_seeds = ["e3"]
        union = list(dict.fromkeys(literal_seeds + seeds))
        assert union == ["e3", "e1", "e2"]
