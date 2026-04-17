"""Tests for DefaultPolicyGate."""

from __future__ import annotations

from trellis.mutate.commands import Command, Operation
from trellis.mutate.policy_gate import DefaultPolicyGate
from trellis.schemas.enums import Enforcement, PolicyType
from trellis.schemas.policy import Policy, PolicyRule, PolicyScope


def _cmd(
    op: Operation = Operation.ENTITY_CREATE,
    target_type: str | None = None,
    metadata: dict[str, str] | None = None,
) -> Command:
    return Command(
        operation=op,
        args={"entity_type": "service", "name": "auth"},
        target_type=target_type,
        metadata=metadata or {},
    )


def _policy(
    level: str = "global",
    value: str | None = None,
    rules: list[PolicyRule] | None = None,
    enforcement: Enforcement = Enforcement.ENFORCE,
) -> Policy:
    return Policy(
        policy_type=PolicyType.MUTATION,
        scope=PolicyScope(level=level, value=value),
        rules=rules or [],
        enforcement=enforcement,
    )


class TestDefaultPolicyGate:
    def test_no_policies_allows(self) -> None:
        gate = DefaultPolicyGate()
        allowed, _msg, _warnings = gate.check(_cmd())
        assert allowed is True

    def test_global_deny_blocks(self) -> None:
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="deny")])
        gate = DefaultPolicyGate(policies=[policy])
        allowed, msg, _warnings = gate.check(_cmd())
        assert allowed is False
        assert "Denied" in msg

    def test_global_allow_passes(self) -> None:
        policy = _policy(rules=[PolicyRule(operation="entity.create", action="allow")])
        gate = DefaultPolicyGate(policies=[policy])
        allowed, _msg, _warnings = gate.check(_cmd())
        assert allowed is True

    def test_require_approval_blocks(self) -> None:
        policy = _policy(
            rules=[
                PolicyRule(
                    operation="precedent.promote",
                    action="require_approval",
                    condition="always",
                )
            ]
        )
        gate = DefaultPolicyGate(policies=[policy])
        cmd = Command(
            operation=Operation.PRECEDENT_PROMOTE,
            args={"trace_id": "t1", "title": "x", "description": "y"},
        )
        allowed, msg, _warnings = gate.check(cmd)
        assert allowed is False
        assert "Approval required" in msg

    def test_warn_enforcement_allows_with_warning(self) -> None:
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.WARN,
        )
        gate = DefaultPolicyGate(policies=[policy])
        allowed, _msg, warnings = gate.check(_cmd())
        assert allowed is True
        assert len(warnings) == 1

    def test_audit_only_allows_silently(self) -> None:
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="deny")],
            enforcement=Enforcement.AUDIT_ONLY,
        )
        gate = DefaultPolicyGate(policies=[policy])
        allowed, _msg, warnings = gate.check(_cmd())
        assert allowed is True
        assert len(warnings) == 0

    def test_domain_scope_matches(self) -> None:
        policy = _policy(
            level="domain",
            value="platform",
            rules=[PolicyRule(operation="entity.create", action="deny")],
        )
        gate = DefaultPolicyGate(policies=[policy])
        # Command with matching domain
        allowed, _, _ = gate.check(_cmd(metadata={"domain": "platform"}))
        assert allowed is False
        # Command with different domain - should pass
        allowed, _, _ = gate.check(_cmd(metadata={"domain": "data"}))
        assert allowed is True

    def test_entity_type_scope_matches(self) -> None:
        policy = _policy(
            level="entity_type",
            value="trace",
            rules=[PolicyRule(operation="*", action="deny")],
        )
        gate = DefaultPolicyGate(policies=[policy])
        # Matching target_type
        allowed, _, _ = gate.check(_cmd(target_type="trace"))
        assert allowed is False
        # Non-matching target_type
        allowed, _, _ = gate.check(_cmd(target_type="entity"))
        assert allowed is True

    def test_wildcard_operation(self) -> None:
        policy = _policy(rules=[PolicyRule(operation="*", action="deny")])
        gate = DefaultPolicyGate(policies=[policy])
        allowed, _, _ = gate.check(_cmd())
        assert allowed is False

    def test_wildcard_prefix_operation(self) -> None:
        policy = _policy(rules=[PolicyRule(operation="entity.*", action="deny")])
        gate = DefaultPolicyGate(policies=[policy])
        # entity.create should match
        allowed, _, _ = gate.check(_cmd(op=Operation.ENTITY_CREATE))
        assert allowed is False
        # trace.ingest should not match
        cmd = Command(operation=Operation.TRACE_INGEST, args={"trace": {}})
        allowed, _, _ = gate.check(cmd)
        assert allowed is True

    def test_unmatched_rule_passes(self) -> None:
        policy = _policy(rules=[PolicyRule(operation="trace.ingest", action="deny")])
        gate = DefaultPolicyGate(policies=[policy])
        # entity.create doesn't match trace.ingest rule
        allowed, _, _ = gate.check(_cmd())
        assert allowed is True

    def test_add_and_remove_policy(self) -> None:
        gate = DefaultPolicyGate()
        policy = _policy(rules=[PolicyRule(operation="*", action="deny")])
        gate.add_policy(policy)
        allowed, _, _ = gate.check(_cmd())
        assert allowed is False
        # Remove
        assert gate.remove_policy(policy.policy_id) is True
        allowed, _, _ = gate.check(_cmd())
        assert allowed is True

    def test_remove_nonexistent(self) -> None:
        gate = DefaultPolicyGate()
        assert gate.remove_policy("nope") is False

    def test_multiple_policies_most_specific_wins(self) -> None:
        # Global allows, but domain-level denies
        global_policy = _policy(
            level="global",
            rules=[PolicyRule(operation="entity.create", action="allow")],
        )
        domain_policy = _policy(
            level="domain",
            value="restricted",
            rules=[PolicyRule(operation="entity.create", action="deny")],
        )
        gate = DefaultPolicyGate(policies=[global_policy, domain_policy])
        # Domain=restricted should be denied (domain policy checked after global)
        allowed, _, _ = gate.check(_cmd(metadata={"domain": "restricted"}))
        assert allowed is False
