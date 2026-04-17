"""Tests for Precedent schema."""

from __future__ import annotations

from trellis.schemas import Feedback, Precedent


class TestPrecedent:
    """Tests for Precedent model."""

    def test_precedent_basic_creation(self) -> None:
        p = Precedent(
            source_trace_ids=["tr_1", "tr_2"],
            title="Use retry with backoff",
            description="When calling flaky APIs, use exponential backoff.",
            promoted_by="agent_alpha",
        )
        assert len(p.precedent_id) == 26
        assert p.title == "Use retry with backoff"
        assert p.source_trace_ids == ["tr_1", "tr_2"]
        assert p.promoted_by == "agent_alpha"
        assert p.confidence == 0.0
        assert p.feedback == []

    def test_precedent_with_feedback(self) -> None:
        fb = Feedback(rating=0.9, label="helpful", given_by="user_1")
        p = Precedent(
            title="Cache DNS lookups",
            description="Cache DNS to reduce latency.",
            promoted_by="agent_beta",
            confidence=0.85,
            applicability=["networking", "performance"],
            evidence_refs=["ev_1", "ev_2"],
            feedback=[fb],
        )
        assert len(p.feedback) == 1
        assert p.feedback[0].rating == 0.9
        assert p.confidence == 0.85
        assert p.applicability == ["networking", "performance"]
