"""Reciprocal Rank Fusion (RRF) reranker.

Combines heterogeneous score distributions from multiple search strategies
into a single fused ranking.  Each strategy's results are treated as an
independent ranked list; the RRF score for an item is the sum of
``1 / (k + rank_in_list)`` across all lists the item appears in.

This is the highest-ROI reranker: it's deterministic, requires no LLM,
and handles the fundamental problem of merging keyword BM25 scores,
cosine similarities, and graph position scores onto one scale.

Reference: Cormack, Clarke & Butt, "Reciprocal Rank Fusion outperforms
Condorcet and individual Rank Learning Methods", SIGIR 2009.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.retrieve.rerankers.base import Reranker
from trellis.schemas.pack import PackItem
from trellis.schemas.parameters import ParameterScope

if TYPE_CHECKING:
    from trellis.ops.registry import ParameterRegistry

#: Default RRF smoothing constant. Standard value from Cormack et al.
DEFAULT_RRF_K = 60

_COMPONENT_ID = "retrieve.rerankers.RRFReranker"


class RRFReranker(Reranker):
    """Reciprocal Rank Fusion across strategy-grouped candidate lists.

    Args:
        k: Smoothing constant (default 60, standard RRF value).
            Higher values dampen the influence of top-ranked items.
        registry: Optional :class:`ParameterRegistry` to resolve ``k``
            at construction time. Scope is component-only; RRF is a
            cross-strategy fusion layer with no natural domain axis.
    """

    def __init__(
        self,
        *,
        k: int = DEFAULT_RRF_K,
        registry: ParameterRegistry | None = None,
    ) -> None:
        if registry is not None:
            k = registry.get(ParameterScope(component_id=_COMPONENT_ID), "k", k)
        self._k = k

    @property
    def name(self) -> str:
        return "rrf"

    def rerank(
        self,
        query: str,  # noqa: ARG002
        candidates: list[PackItem],
    ) -> list[PackItem]:
        if not candidates:
            return []

        # Group candidates by strategy source into per-strategy ranked lists.
        # Items without a strategy_source go into an "_unknown" bucket.
        strategy_lists: dict[str, list[PackItem]] = {}
        for item in candidates:
            key = item.strategy_source or "_unknown"
            strategy_lists.setdefault(key, []).append(item)

        # Sort each strategy list by its native relevance_score descending
        for items in strategy_lists.values():
            items.sort(key=lambda x: x.relevance_score, reverse=True)

        # Compute RRF score for each item: sum of 1/(k + rank) across lists
        rrf_scores: dict[str, float] = {}
        rrf_components: dict[str, dict[str, float]] = {}
        for strategy_name, items in strategy_lists.items():
            for rank_0, item in enumerate(items):
                rank = rank_0 + 1  # 1-indexed
                contribution = 1.0 / (self._k + rank)
                rrf_scores[item.item_id] = (
                    rrf_scores.get(item.item_id, 0.0) + contribution
                )
                components = rrf_components.setdefault(item.item_id, {})
                components[f"rrf_{strategy_name}"] = contribution

        # Build result: update relevance_score and score_breakdown
        result: list[PackItem] = []
        seen: set[str] = set()
        for item in candidates:
            if item.item_id in seen:
                continue
            seen.add(item.item_id)
            fused_score = rrf_scores.get(item.item_id, 0.0)
            breakdown = rrf_components.get(item.item_id, {})
            breakdown["rrf_total"] = fused_score
            breakdown["original_score"] = item.relevance_score
            result.append(
                item.model_copy(
                    update={
                        "relevance_score": fused_score,
                        "score_breakdown": breakdown,
                    }
                )
            )

        result.sort(key=lambda x: x.relevance_score, reverse=True)
        return result
