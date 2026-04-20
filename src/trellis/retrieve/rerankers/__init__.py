"""Reranker implementations for post-retrieval score fusion and diversification."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.retrieve.rerankers.base import RankedItem, Reranker
from trellis.retrieve.rerankers.mmr import MMRReranker
from trellis.retrieve.rerankers.rrf import RRFReranker

if TYPE_CHECKING:
    from trellis.ops.registry import ParameterRegistry


def build_reranker(
    kind: str = "rrf",
    *,
    parameter_registry: ParameterRegistry | None = None,
) -> Reranker:
    """Construct a reranker by name, plumbing the optional registry.

    Args:
        kind: ``"rrf"`` (default) or ``"mmr"``.
        parameter_registry: Optional :class:`ParameterRegistry`.  Each
            reranker pulls its own constants (RRF ``k``, MMR
            ``lambda_param`` / ``shingle_size``) at construction time
            from the component-scoped snapshot, falling back to the
            hardcoded defaults when no snapshot is present.

    Raises:
        ValueError: when ``kind`` is not recognised.
    """
    if kind == "rrf":
        return RRFReranker(registry=parameter_registry)
    if kind == "mmr":
        return MMRReranker(registry=parameter_registry)
    msg = f"Unknown reranker kind: {kind!r}"
    raise ValueError(msg)


__all__ = [
    "MMRReranker",
    "RRFReranker",
    "RankedItem",
    "Reranker",
    "build_reranker",
]
