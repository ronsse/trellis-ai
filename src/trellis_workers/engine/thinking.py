"""Workflow engine — thinking policy for internal curation workers."""

from __future__ import annotations

from enum import StrEnum

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel

logger = structlog.get_logger(__name__)


class WorkflowTier(StrEnum):
    """Cognition tiers for worker tasks."""

    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"
    CRITICAL = "critical"


class ReasoningEffort(StrEnum):
    """How much reasoning to apply."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TierConfig(TrellisModel):
    """Configuration for a single tier."""

    tier: WorkflowTier
    model: str = "default"
    reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM
    max_tokens: int = 2000
    temperature: float = 0.3
    max_context_tokens: int = 4000
    use_verification: bool = False


# Default tier configurations
DEFAULT_TIERS: dict[WorkflowTier, TierConfig] = {
    WorkflowTier.FAST: TierConfig(
        tier=WorkflowTier.FAST,
        reasoning_effort=ReasoningEffort.LOW,
        max_tokens=500,
        max_context_tokens=2000,
    ),
    WorkflowTier.STANDARD: TierConfig(
        tier=WorkflowTier.STANDARD,
        reasoning_effort=ReasoningEffort.MEDIUM,
        max_tokens=2000,
        max_context_tokens=4000,
    ),
    WorkflowTier.DEEP: TierConfig(
        tier=WorkflowTier.DEEP,
        reasoning_effort=ReasoningEffort.HIGH,
        max_tokens=4000,
        max_context_tokens=8000,
    ),
    WorkflowTier.CRITICAL: TierConfig(
        tier=WorkflowTier.CRITICAL,
        reasoning_effort=ReasoningEffort.HIGH,
        max_tokens=8000,
        max_context_tokens=16000,
        use_verification=True,
    ),
}


class EscalationConfig(TrellisModel):
    """Configuration for automatic escalation."""

    enabled: bool = True
    max_escalations: int = 2
    confidence_threshold: float = 0.7


class ThinkingPolicy(TrellisModel):
    """Output of the engine — what settings to use for a task."""

    tier: WorkflowTier
    tier_config: TierConfig
    escalation_reason: str | None = None
    escalated_from: WorkflowTier | None = None


class EscalationAttempt(TrellisModel):
    """Record of an escalation attempt."""

    from_tier: WorkflowTier
    to_tier: WorkflowTier
    trigger: str
    reason: str
    success: bool


class WorkflowSession(TrellisModel):
    """Tracks state across escalation attempts within one workflow task."""

    current_tier: WorkflowTier = WorkflowTier.STANDARD
    escalation_count: int = 0
    max_escalations: int = 2
    attempts: list[EscalationAttempt] = Field(default_factory=list)
    gate_failures: list[str] = Field(default_factory=list)

    def can_escalate(self) -> bool:
        """Check if further escalation is possible."""
        if self.escalation_count >= self.max_escalations:
            return False
        tier_order = list(WorkflowTier)
        current_idx = tier_order.index(self.current_tier)
        return current_idx < len(tier_order) - 1

    def next_tier(self) -> WorkflowTier | None:
        """Get the next tier up, or None if at max."""
        tier_order = list(WorkflowTier)
        current_idx = tier_order.index(self.current_tier)
        if current_idx >= len(tier_order) - 1:
            return None
        return tier_order[current_idx + 1]


class WorkflowEngine:
    """Controls thinking policy for worker tasks.

    Implements "Attempt -> Gate -> Escalate" pattern:
    1. Start at a baseline tier
    2. Execute the task
    3. Check quality/confidence
    4. If insufficient, escalate to higher tier
    """

    def __init__(
        self,
        tiers: dict[WorkflowTier, TierConfig] | None = None,
        escalation: EscalationConfig | None = None,
    ) -> None:
        self._tiers = tiers or dict(DEFAULT_TIERS)
        self._escalation = escalation or EscalationConfig()

    def create_session(
        self,
        starting_tier: WorkflowTier = WorkflowTier.STANDARD,
    ) -> WorkflowSession:
        """Create a new workflow session."""
        return WorkflowSession(
            current_tier=starting_tier,
            max_escalations=self._escalation.max_escalations,
        )

    def get_policy(self, session: WorkflowSession) -> ThinkingPolicy:
        """Get the current thinking policy for a session."""
        tier_config = self._tiers.get(
            session.current_tier,
            DEFAULT_TIERS[WorkflowTier.STANDARD],
        )

        escalation_reason = None
        escalated_from = None
        if session.attempts:
            last = session.attempts[-1]
            if last.success:
                escalation_reason = last.reason
                escalated_from = last.from_tier

        return ThinkingPolicy(
            tier=session.current_tier,
            tier_config=tier_config,
            escalation_reason=escalation_reason,
            escalated_from=escalated_from,
        )

    def should_escalate(
        self,
        session: WorkflowSession,
        *,
        confidence: float | None = None,
        gate_failures: list[str] | None = None,
        error: str | None = None,
    ) -> tuple[bool, str]:
        """Evaluate whether escalation is warranted.

        Args:
            session: Current workflow session.
            confidence: Task confidence score (0.0-1.0).
            gate_failures: List of quality gate failures.
            error: Error message if task failed.

        Returns:
            Tuple of (should_escalate, reason).
        """
        if not self._escalation.enabled:
            return False, "Escalation disabled"

        if not session.can_escalate():
            return False, "Cannot escalate (at max tier or max attempts)"

        if gate_failures:
            session.gate_failures.extend(gate_failures)
            return True, f"Quality gates failed: {', '.join(gate_failures[:3])}"

        threshold = self._escalation.confidence_threshold
        if confidence is not None and confidence < threshold:
            return (
                True,
                f"Confidence {confidence:.2f} below threshold {threshold}",
            )

        if error:
            return True, f"Task error: {error[:100]}"

        return False, "No escalation triggers matched"

    def escalate(
        self,
        session: WorkflowSession,
        reason: str,
        trigger: str = "auto",
    ) -> bool:
        """Attempt to escalate to the next tier.

        Args:
            session: Current workflow session.
            reason: Reason for escalation.
            trigger: What triggered it.

        Returns:
            True if escalation succeeded.
        """
        next_tier = session.next_tier()
        if next_tier is None or not session.can_escalate():
            session.attempts.append(
                EscalationAttempt(
                    from_tier=session.current_tier,
                    to_tier=session.current_tier,
                    trigger=trigger,
                    reason="Cannot escalate",
                    success=False,
                )
            )
            return False

        old_tier = session.current_tier
        session.current_tier = next_tier
        session.escalation_count += 1
        session.attempts.append(
            EscalationAttempt(
                from_tier=old_tier,
                to_tier=next_tier,
                trigger=trigger,
                reason=reason,
                success=True,
            )
        )

        logger.info(
            "workflow_escalated",
            from_tier=old_tier,
            to_tier=next_tier,
            trigger=trigger,
            reason=reason,
        )

        return True

    _LARGE_CONTEXT_THRESHOLD = 8000

    def determine_initial_tier(
        self,
        *,
        intent: str = "",
        risk_level: str | None = None,
        context_size: int | None = None,
    ) -> WorkflowTier:
        """Heuristically determine starting tier based on task characteristics."""
        tier = WorkflowTier.STANDARD
        _upgradeable = (WorkflowTier.FAST, WorkflowTier.STANDARD)

        intent_lower = intent.lower()
        if "deep" in intent_lower or "complex" in intent_lower:
            tier = WorkflowTier.DEEP
        elif "quick" in intent_lower or "simple" in intent_lower:
            tier = WorkflowTier.FAST

        if risk_level == "high" and tier in _upgradeable:
            tier = WorkflowTier.DEEP

        if (
            context_size
            and context_size >= self._LARGE_CONTEXT_THRESHOLD
            and tier in _upgradeable
        ):
            tier = WorkflowTier.DEEP

        return tier
