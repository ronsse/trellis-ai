"""Tests for workflow engine thinking policy."""

from __future__ import annotations

import pytest

from trellis_workers.engine.thinking import (
    DEFAULT_TIERS,
    EscalationConfig,
    ReasoningEffort,
    TierConfig,
    WorkflowEngine,
    WorkflowSession,
    WorkflowTier,
)

# ---------------------------------------------------------------------------
# WorkflowTier & ReasoningEffort
# ---------------------------------------------------------------------------


class TestWorkflowTier:
    def test_values(self):
        assert WorkflowTier.FAST == "fast"
        assert WorkflowTier.STANDARD == "standard"
        assert WorkflowTier.DEEP == "deep"
        assert WorkflowTier.CRITICAL == "critical"

    def test_all_tiers_present(self):
        assert len(WorkflowTier) == 4

    def test_str_enum(self):
        assert isinstance(WorkflowTier.FAST, str)


class TestReasoningEffort:
    def test_values(self):
        assert ReasoningEffort.LOW == "low"
        assert ReasoningEffort.MEDIUM == "medium"
        assert ReasoningEffort.HIGH == "high"


# ---------------------------------------------------------------------------
# TierConfig
# ---------------------------------------------------------------------------


class TestTierConfig:
    def test_defaults(self):
        cfg = TierConfig(tier=WorkflowTier.STANDARD)
        assert cfg.model == "default"
        assert cfg.reasoning_effort == ReasoningEffort.MEDIUM
        assert cfg.max_tokens == 2000
        assert cfg.temperature == 0.3
        assert cfg.max_context_tokens == 4000
        assert cfg.use_verification is False

    def test_custom_values(self):
        cfg = TierConfig(
            tier=WorkflowTier.CRITICAL,
            model="gpt-4",
            reasoning_effort=ReasoningEffort.HIGH,
            max_tokens=8000,
            temperature=0.1,
            max_context_tokens=16000,
            use_verification=True,
        )
        assert cfg.tier == WorkflowTier.CRITICAL
        assert cfg.model == "gpt-4"
        assert cfg.use_verification is True

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValueError):
            TierConfig(tier=WorkflowTier.FAST, unknown_field="x")


# ---------------------------------------------------------------------------
# DEFAULT_TIERS
# ---------------------------------------------------------------------------


class TestDefaultTiers:
    def test_all_tiers_have_config(self):
        for tier in WorkflowTier:
            assert tier in DEFAULT_TIERS

    def test_fast_tier_config(self):
        cfg = DEFAULT_TIERS[WorkflowTier.FAST]
        assert cfg.reasoning_effort == ReasoningEffort.LOW
        assert cfg.max_tokens == 500

    def test_critical_tier_config(self):
        cfg = DEFAULT_TIERS[WorkflowTier.CRITICAL]
        assert cfg.reasoning_effort == ReasoningEffort.HIGH
        assert cfg.use_verification is True
        assert cfg.max_tokens == 8000


# ---------------------------------------------------------------------------
# WorkflowSession
# ---------------------------------------------------------------------------


class TestWorkflowSession:
    def test_can_escalate_from_fast(self):
        session = WorkflowSession(current_tier=WorkflowTier.FAST)
        assert session.can_escalate() is True

    def test_can_escalate_from_standard(self):
        session = WorkflowSession(current_tier=WorkflowTier.STANDARD)
        assert session.can_escalate() is True

    def test_cannot_escalate_from_critical(self):
        session = WorkflowSession(current_tier=WorkflowTier.CRITICAL)
        assert session.can_escalate() is False

    def test_cannot_escalate_at_max_attempts(self):
        session = WorkflowSession(
            current_tier=WorkflowTier.STANDARD,
            escalation_count=2,
            max_escalations=2,
        )
        assert session.can_escalate() is False

    def test_next_tier_fast(self):
        session = WorkflowSession(current_tier=WorkflowTier.FAST)
        assert session.next_tier() == WorkflowTier.STANDARD

    def test_next_tier_standard(self):
        session = WorkflowSession(current_tier=WorkflowTier.STANDARD)
        assert session.next_tier() == WorkflowTier.DEEP

    def test_next_tier_deep(self):
        session = WorkflowSession(current_tier=WorkflowTier.DEEP)
        assert session.next_tier() == WorkflowTier.CRITICAL

    def test_next_tier_critical_is_none(self):
        session = WorkflowSession(current_tier=WorkflowTier.CRITICAL)
        assert session.next_tier() is None


# ---------------------------------------------------------------------------
# WorkflowEngine
# ---------------------------------------------------------------------------


class TestWorkflowEngineCreateSession:
    def test_default_starting_tier(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        assert session.current_tier == WorkflowTier.STANDARD
        assert session.escalation_count == 0

    def test_custom_starting_tier(self):
        engine = WorkflowEngine()
        session = engine.create_session(starting_tier=WorkflowTier.FAST)
        assert session.current_tier == WorkflowTier.FAST

    def test_session_inherits_max_escalations(self):
        engine = WorkflowEngine(escalation=EscalationConfig(max_escalations=5))
        session = engine.create_session()
        assert session.max_escalations == 5


class TestWorkflowEngineGetPolicy:
    def test_returns_correct_tier_config(self):
        engine = WorkflowEngine()
        session = engine.create_session(starting_tier=WorkflowTier.DEEP)
        policy = engine.get_policy(session)
        assert policy.tier == WorkflowTier.DEEP
        assert policy.tier_config.reasoning_effort == ReasoningEffort.HIGH
        assert policy.tier_config.max_tokens == 4000

    def test_no_escalation_info_initially(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        policy = engine.get_policy(session)
        assert policy.escalation_reason is None
        assert policy.escalated_from is None

    def test_escalation_info_after_escalation(self):
        engine = WorkflowEngine()
        session = engine.create_session(starting_tier=WorkflowTier.STANDARD)
        engine.escalate(session, reason="low confidence", trigger="auto")
        policy = engine.get_policy(session)
        assert policy.escalation_reason == "low confidence"
        assert policy.escalated_from == WorkflowTier.STANDARD
        assert policy.tier == WorkflowTier.DEEP


class TestWorkflowEngineShouldEscalate:
    def test_low_confidence(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        should, reason = engine.should_escalate(session, confidence=0.5)
        assert should is True
        assert "0.50" in reason
        assert "threshold" in reason

    def test_high_confidence_no_escalation(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        should, _reason = engine.should_escalate(session, confidence=0.9)
        assert should is False

    def test_gate_failures(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        should, reason = engine.should_escalate(
            session, gate_failures=["missing_tags", "low_quality"]
        )
        assert should is True
        assert "missing_tags" in reason
        assert "low_quality" in reason

    def test_gate_failures_recorded_in_session(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        engine.should_escalate(session, gate_failures=["bad_format"])
        assert "bad_format" in session.gate_failures

    def test_error_triggers_escalation(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        should, reason = engine.should_escalate(session, error="timeout occurred")
        assert should is True
        assert "timeout occurred" in reason

    def test_disabled_escalation(self):
        engine = WorkflowEngine(escalation=EscalationConfig(enabled=False))
        session = engine.create_session()
        should, reason = engine.should_escalate(session, confidence=0.1)
        assert should is False
        assert "disabled" in reason.lower()

    def test_at_max_tier_no_escalation(self):
        engine = WorkflowEngine()
        session = engine.create_session(starting_tier=WorkflowTier.CRITICAL)
        should, reason = engine.should_escalate(session, confidence=0.1)
        assert should is False
        assert "max" in reason.lower()

    def test_at_max_attempts_no_escalation(self):
        engine = WorkflowEngine(escalation=EscalationConfig(max_escalations=0))
        session = engine.create_session()
        should, _reason = engine.should_escalate(session, confidence=0.1)
        assert should is False

    def test_no_triggers_no_escalation(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        should, reason = engine.should_escalate(session)
        assert should is False
        assert "no escalation triggers" in reason.lower()


class TestWorkflowEngineEscalate:
    def test_escalate_succeeds(self):
        engine = WorkflowEngine()
        session = engine.create_session(starting_tier=WorkflowTier.STANDARD)
        result = engine.escalate(session, reason="low quality")
        assert result is True
        assert session.current_tier == WorkflowTier.DEEP
        assert session.escalation_count == 1
        assert len(session.attempts) == 1
        assert session.attempts[0].success is True
        assert session.attempts[0].from_tier == WorkflowTier.STANDARD
        assert session.attempts[0].to_tier == WorkflowTier.DEEP

    def test_escalate_twice(self):
        engine = WorkflowEngine()
        session = engine.create_session(starting_tier=WorkflowTier.STANDARD)
        engine.escalate(session, reason="first")
        engine.escalate(session, reason="second")
        assert session.current_tier == WorkflowTier.CRITICAL
        assert session.escalation_count == 2

    def test_escalate_fails_at_max_tier(self):
        engine = WorkflowEngine()
        session = engine.create_session(starting_tier=WorkflowTier.CRITICAL)
        result = engine.escalate(session, reason="try escalate")
        assert result is False
        assert session.current_tier == WorkflowTier.CRITICAL
        assert session.escalation_count == 0
        assert session.attempts[-1].success is False

    def test_escalate_fails_at_max_attempts(self):
        engine = WorkflowEngine(escalation=EscalationConfig(max_escalations=1))
        session = engine.create_session(starting_tier=WorkflowTier.FAST)
        engine.escalate(session, reason="first")
        result = engine.escalate(session, reason="second")
        assert result is False
        assert session.current_tier == WorkflowTier.STANDARD
        assert session.escalation_count == 1

    def test_escalate_custom_trigger(self):
        engine = WorkflowEngine()
        session = engine.create_session()
        engine.escalate(session, reason="manual", trigger="human_review")
        assert session.attempts[-1].trigger == "human_review"


class TestWorkflowEngineDetermineInitialTier:
    def test_default_is_standard(self):
        engine = WorkflowEngine()
        assert engine.determine_initial_tier() == WorkflowTier.STANDARD

    def test_deep_intent(self):
        engine = WorkflowEngine()
        tier = engine.determine_initial_tier(intent="deep analysis")
        assert tier == WorkflowTier.DEEP

    def test_complex_intent(self):
        engine = WorkflowEngine()
        tier = engine.determine_initial_tier(intent="complex pattern")
        assert tier == WorkflowTier.DEEP

    def test_quick_intent(self):
        engine = WorkflowEngine()
        assert engine.determine_initial_tier(intent="quick check") == WorkflowTier.FAST

    def test_simple_intent(self):
        engine = WorkflowEngine()
        tier = engine.determine_initial_tier(intent="simple classification")
        assert tier == WorkflowTier.FAST

    def test_high_risk_upgrades_to_deep(self):
        engine = WorkflowEngine()
        tier = engine.determine_initial_tier(risk_level="high")
        assert tier == WorkflowTier.DEEP

    def test_high_risk_does_not_downgrade_deep(self):
        engine = WorkflowEngine()
        tier = engine.determine_initial_tier(intent="deep analysis", risk_level="high")
        assert tier == WorkflowTier.DEEP

    def test_large_context_upgrades_to_deep(self):
        engine = WorkflowEngine()
        tier = engine.determine_initial_tier(context_size=10000)
        assert tier == WorkflowTier.DEEP

    def test_small_context_keeps_standard(self):
        engine = WorkflowEngine()
        tier = engine.determine_initial_tier(context_size=2000)
        assert tier == WorkflowTier.STANDARD

    def test_case_insensitive_intent(self):
        engine = WorkflowEngine()
        assert engine.determine_initial_tier(intent="DEEP review") == WorkflowTier.DEEP
