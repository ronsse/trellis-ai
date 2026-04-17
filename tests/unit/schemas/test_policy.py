"""Tests for Policy schema."""

from __future__ import annotations

from trellis.schemas import Enforcement, Policy, PolicyRule, PolicyScope, PolicyType


class TestPolicy:
    """Tests for Policy model."""

    def test_policy_basic_creation(self) -> None:
        rule = PolicyRule(
            operation="precedent.promote",
            condition="confidence > 0.8",
            action="allow",
        )
        p = Policy(
            policy_type=PolicyType.MUTATION,
            scope=PolicyScope(level="global"),
            rules=[rule],
        )
        assert len(p.policy_id) == 26
        assert p.policy_type == PolicyType.MUTATION
        assert p.scope.level == "global"
        assert p.scope.value is None
        assert len(p.rules) == 1
        assert p.enforcement == Enforcement.ENFORCE

    def test_policy_audit_only(self) -> None:
        p = Policy(
            policy_type=PolicyType.ACCESS,
            scope=PolicyScope(level="team", value="platform"),
            enforcement=Enforcement.AUDIT_ONLY,
        )
        assert p.enforcement == Enforcement.AUDIT_ONLY
        assert p.scope.value == "platform"
        assert p.rules == []
