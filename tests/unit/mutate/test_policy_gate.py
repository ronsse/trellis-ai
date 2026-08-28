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

    def test_narrow_deny_blocks_under_a_broad_allow(self) -> None:
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


class TestDenyWinsResolution:
    """Pin the actual scope-resolution semantics.

    The gate's docstring used to claim "more specific policies override
    broader ones". It does not: matching policies are evaluated
    broadest-first and the first blocking rule returns, so the *broadest*
    deny wins and a narrow ``allow`` cannot carve an exception out of it.
    These tests pin the behaviour that exists rather than the sentence that
    described it, so a future change to either has to change the other.
    """

    def test_broad_deny_is_not_overridden_by_a_narrow_allow(self) -> None:
        global_deny = _policy(
            level="global",
            rules=[PolicyRule(operation="entity.create", action="deny")],
        )
        domain_allow = _policy(
            level="domain",
            value="permitted",
            rules=[PolicyRule(operation="entity.create", action="allow")],
        )
        gate = DefaultPolicyGate(policies=[global_deny, domain_allow])

        # The narrow allow does NOT rescue the command.
        allowed, msg, _ = gate.check(_cmd(metadata={"domain": "permitted"}))
        assert allowed is False
        assert "Denied" in msg

    def test_allow_rule_alone_grants_nothing_beyond_the_default(self) -> None:
        """``allow`` is inert — an unmatched command is already allowed."""
        gate = DefaultPolicyGate(
            policies=[_policy(rules=[PolicyRule(operation="*", action="allow")])]
        )
        assert gate.check(_cmd()) == (True, "", [])


class TestWarnActionIsLive:
    """``action="warn"`` used to be dead code — no branch ever read it."""

    def test_warn_action_under_enforce_warns_without_blocking(self) -> None:
        policy = _policy(
            rules=[
                PolicyRule(
                    operation="entity.create",
                    action="warn",
                    condition="review this",
                )
            ],
            enforcement=Enforcement.ENFORCE,
        )
        gate = DefaultPolicyGate(policies=[policy])
        allowed, msg, warnings = gate.check(_cmd())

        assert allowed is True
        assert msg == ""
        assert len(warnings) == 1
        assert "review this" in warnings[0]

    def test_warn_action_under_warn_enforcement_warns(self) -> None:
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="warn")],
            enforcement=Enforcement.WARN,
        )
        gate = DefaultPolicyGate(policies=[policy])
        allowed, _, warnings = gate.check(_cmd())
        assert allowed is True
        assert len(warnings) == 1

    def test_warn_action_under_audit_only_is_silent(self) -> None:
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="warn")],
            enforcement=Enforcement.AUDIT_ONLY,
        )
        gate = DefaultPolicyGate(policies=[policy])
        allowed, _, warnings = gate.check(_cmd())
        assert allowed is True
        assert warnings == []

    def test_require_approval_under_warn_enforcement_warns(self) -> None:
        """Previously fell through silently — neither blocked nor warned."""
        policy = _policy(
            rules=[PolicyRule(operation="entity.create", action="require_approval")],
            enforcement=Enforcement.WARN,
        )
        gate = DefaultPolicyGate(policies=[policy])
        allowed, _, warnings = gate.check(_cmd())
        assert allowed is True
        assert len(warnings) == 1

    def test_warnings_accumulate_across_matching_policies(self) -> None:
        gate = DefaultPolicyGate(
            policies=[
                _policy(
                    rules=[PolicyRule(operation="entity.create", action="warn")],
                    enforcement=Enforcement.WARN,
                ),
                _policy(
                    rules=[PolicyRule(operation="*", action="warn")],
                    enforcement=Enforcement.WARN,
                ),
            ]
        )
        allowed, _, warnings = gate.check(_cmd())
        assert allowed is True
        assert len(warnings) == 2

    def test_a_later_deny_still_blocks_after_earlier_warnings(self) -> None:
        """Warnings collected before a block are returned with the rejection."""
        gate = DefaultPolicyGate(
            policies=[
                _policy(
                    rules=[PolicyRule(operation="entity.create", action="warn")],
                    enforcement=Enforcement.WARN,
                ),
                _policy(
                    level="entity_type",
                    value="service",
                    rules=[PolicyRule(operation="entity.create", action="deny")],
                    enforcement=Enforcement.ENFORCE,
                ),
            ]
        )
        allowed, msg, warnings = gate.check(_cmd(target_type="service"))
        assert allowed is False
        assert "Denied" in msg
        assert len(warnings) == 1
