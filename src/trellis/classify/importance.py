"""Composite importance scoring from classification tags."""

from __future__ import annotations

from trellis.schemas.classification import ContentTags

_QUALITY_BOOST: dict[str, float] = {
    "high": 0.3,
    "standard": 0.0,
    "low": -0.2,
    "noise": -0.5,
}

_SCOPE_BOOST: dict[str, float] = {
    "universal": 0.15,
    "org": 0.05,
    "project": 0.0,
    "ephemeral": -0.2,
}


def compute_importance(tags: ContentTags, base_importance: float = 0.0) -> float:
    """Composite importance from classification tags and LLM score.

    Combines a base importance (from LLM or caller) with deterministic
    boosts from signal_quality and scope facets.
    """
    score = base_importance
    score += _QUALITY_BOOST.get(tags.signal_quality, 0.0)
    if tags.scope:
        score += _SCOPE_BOOST.get(tags.scope, 0.0)
    return max(0.0, min(1.0, score))
