"""Maximal Marginal Relevance (MMR) reranker.

Promotes diversity by iteratively selecting items that are both relevant
to the query and dissimilar to already-selected items.  Uses excerpt
text overlap (Jaccard similarity on word shingles) as the similarity
measure — no embeddings required, fully deterministic.

Useful against the "sectioned packs all returning similar items" problem:
MMR penalises near-duplicates that keyword and semantic search both
surface with different item_ids but overlapping content.

Reference: Carbonell & Goldstein, "The Use of MMR, Diversity-Based
Reranking for Reordering Documents and Producing Summaries", SIGIR 1998.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.retrieve.rerankers.base import Reranker
from trellis.schemas.pack import PackItem
from trellis.schemas.parameters import ParameterScope

if TYPE_CHECKING:
    from trellis.ops.registry import ParameterRegistry

#: Default trade-off between relevance and diversity. 1.0 = pure
#: relevance (no diversity), 0.0 = pure diversity. 0.7 balances both.
DEFAULT_MMR_LAMBDA = 0.7

#: Default word n-gram size used for shingle-based similarity.
DEFAULT_MMR_SHINGLE_SIZE = 3

_COMPONENT_ID = "retrieve.rerankers.MMRReranker"


def _word_shingles(text: str, n: int = 3) -> set[str]:
    """Extract word-level n-gram shingles from text."""
    words = text.lower().split()
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two shingle sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class MMRReranker(Reranker):
    """Maximal Marginal Relevance using text-overlap similarity.

    Args:
        lambda_param: Trade-off between relevance and diversity.
            1.0 = pure relevance (no diversity), 0.0 = pure diversity.
            Default 0.7 balances both.
        shingle_size: Word n-gram size for similarity computation.
    """

    def __init__(
        self,
        *,
        lambda_param: float = DEFAULT_MMR_LAMBDA,
        shingle_size: int = DEFAULT_MMR_SHINGLE_SIZE,
        registry: ParameterRegistry | None = None,
    ) -> None:
        if registry is not None:
            scope = ParameterScope(component_id=_COMPONENT_ID)
            lambda_param = registry.get(scope, "lambda_param", lambda_param)
            shingle_size = registry.get(scope, "shingle_size", shingle_size)
        self._lambda = lambda_param
        self._shingle_size = shingle_size

    @property
    def name(self) -> str:
        return "mmr"

    def rerank(
        self,
        query: str,  # noqa: ARG002
        candidates: list[PackItem],
    ) -> list[PackItem]:
        if not candidates:
            return []

        # Normalise relevance scores to [0, 1] for MMR formula
        max_score = max(c.relevance_score for c in candidates)
        min_score = min(c.relevance_score for c in candidates)
        score_range = max_score - min_score if max_score != min_score else 1.0

        def norm_score(item: PackItem) -> float:
            return (item.relevance_score - min_score) / score_range

        # Pre-compute shingles for each candidate
        shingles: dict[str, set[str]] = {
            c.item_id: _word_shingles(c.excerpt, self._shingle_size) for c in candidates
        }

        # Build lookup by item_id (candidates may have dups from earlier steps)
        by_id: dict[str, PackItem] = {}
        for c in candidates:
            if c.item_id not in by_id:
                by_id[c.item_id] = c

        remaining = set(by_id.keys())
        selected_ids: list[str] = []
        selected_shingles: list[set[str]] = []
        mmr_scores: dict[str, float] = {}

        while remaining:
            best_id: str | None = None
            best_mmr = -1.0

            for cid in remaining:
                relevance = norm_score(by_id[cid])

                # Max similarity to any already-selected item
                if selected_shingles:
                    max_sim = max(
                        _jaccard(shingles[cid], sel_sh) for sel_sh in selected_shingles
                    )
                else:
                    max_sim = 0.0

                mmr = self._lambda * relevance - (1.0 - self._lambda) * max_sim

                if mmr > best_mmr:
                    best_mmr = mmr
                    best_id = cid

            if best_id is None:
                break

            selected_ids.append(best_id)
            selected_shingles.append(shingles[best_id])
            remaining.discard(best_id)
            mmr_scores[best_id] = best_mmr

        # Assign descending scores so downstream budget logic works
        result: list[PackItem] = []
        n = len(selected_ids)
        for rank_0, item_id in enumerate(selected_ids):
            item = by_id[item_id]
            # Linear score from 1.0 down to near-zero, preserving ordering
            fused_score = (n - rank_0) / n if n > 0 else 0.0
            result.append(
                item.model_copy(
                    update={
                        "relevance_score": fused_score,
                        "score_breakdown": {
                            "mmr_score": mmr_scores.get(item_id, 0.0),
                            "original_score": item.relevance_score,
                            "mmr_rank": rank_0 + 1,
                        },
                    }
                )
            )

        return result
