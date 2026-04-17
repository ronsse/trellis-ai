"""Reranker protocol — post-retrieval rescoring of pack candidates."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import Field

from trellis.core.base import VersionedModel
from trellis.schemas.pack import PackItem


class RankedItem(VersionedModel):
    """A pack item annotated with a reranker-assigned score."""

    item: PackItem
    reranker_score: float = 0.0
    reranker_details: dict[str, Any] = Field(default_factory=dict)


class Reranker(ABC):
    """Protocol for post-retrieval reranking strategies.

    Rerankers receive the query and a list of deduplicated candidates from
    all search strategies, then return them in a new order with updated
    ``relevance_score`` values.  Applied in ``PackBuilder`` after strategy
    union + dedup, before budget enforcement.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Reranker name for telemetry."""

    @abstractmethod
    def rerank(
        self,
        query: str,
        candidates: list[PackItem],
    ) -> list[PackItem]:
        """Rerank candidates and return them with updated scores.

        Implementations must:
        - Return items in the desired order (best first).
        - Update ``relevance_score`` to reflect the reranked ordering.
        - Optionally populate ``score_breakdown`` with component scores.
        """
