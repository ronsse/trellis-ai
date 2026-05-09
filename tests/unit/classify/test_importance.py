"""Tests for composite importance scoring."""

from __future__ import annotations

import pytest

from trellis.classify.importance import compute_importance
from trellis.schemas.classification import ContentTags


class TestComputeImportance:
    """compute_importance combines tags + LLM score."""

    def test_default_returns_base(self) -> None:
        tags = ContentTags()
        assert compute_importance(tags, 0.5) == 0.5

    def test_high_signal_quality_boosts(self) -> None:
        tags = ContentTags(signal_quality="high")
        result = compute_importance(tags, 0.5)
        assert result > 0.5

    def test_noise_signal_quality_penalizes(self) -> None:
        tags = ContentTags(signal_quality="noise")
        result = compute_importance(tags, 0.5)
        assert result < 0.5

    def test_low_signal_quality_penalizes(self) -> None:
        tags = ContentTags(signal_quality="low")
        result = compute_importance(tags, 0.5)
        assert result < 0.5

    def test_universal_scope_boosts(self) -> None:
        tags = ContentTags(scope="universal")
        result = compute_importance(tags, 0.5)
        assert result > 0.5

    def test_ephemeral_scope_penalizes(self) -> None:
        tags = ContentTags(scope="ephemeral")
        result = compute_importance(tags, 0.5)
        assert result < 0.5

    def test_project_scope_neutral(self) -> None:
        tags = ContentTags(scope="project")
        result = compute_importance(tags, 0.5)
        assert result == 0.5

    def test_combined_boosts_stack(self) -> None:
        tags = ContentTags(signal_quality="high", scope="universal")
        result = compute_importance(tags, 0.5)
        assert result > 0.65  # 0.5 + 0.3 + 0.15 = 0.95, clamped to 1.0

    def test_combined_penalties_stack(self) -> None:
        tags = ContentTags(signal_quality="noise", scope="ephemeral")
        result = compute_importance(tags, 0.5)
        assert result < 0.1  # 0.5 - 0.5 - 0.2 = clamped to 0.0

    def test_clamped_to_zero(self) -> None:
        tags = ContentTags(signal_quality="noise")
        result = compute_importance(tags, 0.0)
        assert result == 0.0

    def test_clamped_to_one(self) -> None:
        tags = ContentTags(signal_quality="high", scope="universal")
        result = compute_importance(tags, 0.9)
        assert result <= 1.0

    def test_no_base_importance_still_gets_boosts(self) -> None:
        tags = ContentTags(signal_quality="high", scope="universal")
        result = compute_importance(tags, 0.0)
        assert result > 0.0  # 0.0 + 0.3 + 0.15 = 0.45

    def test_base_importance_kwarg_default_is_zero(self) -> None:
        """``base_importance`` defaults to 0.0 for callers that pre-date
        the kwarg (adr-importance-score-freshness §3.3 + §5 step 1)."""
        tags = ContentTags(signal_quality="high")
        # Calling without the kwarg should be equivalent to passing 0.0.
        assert compute_importance(tags) == compute_importance(tags, 0.0)

    def test_base_importance_kwarg_preserves_prior_score(self) -> None:
        """The new kwarg lets callers re-apply tag boosts on top of an
        existing LLM-derived score, instead of discarding it. Used by
        :func:`reclassify_item` to avoid wiping the LLM contribution
        on every refresh (adr-importance-score-freshness §3.3)."""
        tags = ContentTags(signal_quality="high")
        # base=0.5 + signal_quality=high (+0.3) = 0.8
        assert compute_importance(tags, base_importance=0.5) == pytest.approx(0.8)
