"""Policy gate -- matches and enforces policies on mutation commands."""

from __future__ import annotations

import structlog

from trellis.mutate.commands import Command
from trellis.schemas.enums import Enforcement
from trellis.schemas.policy import Policy, PolicyRule

logger = structlog.get_logger()

_SCOPE_SPECIFICITY: dict[str, int] = {
    "global": 0,
    "domain": 1,
    "team": 2,
    "entity_type": 3,
}


class DefaultPolicyGate:
    """Matches policies by scope and enforces rules on commands.

    Scope matching priority: global < domain < team < entity_type.
    More specific policies override broader ones.

    Enforcement levels:
    - enforce: block the command, return not allowed
    - warn: allow but add warning
    - audit_only: allow silently, just log
    """

    def __init__(self, policies: list[Policy] | None = None) -> None:
        self._policies: list[Policy] = policies or []

    def add_policy(self, policy: Policy) -> None:
        """Add a policy."""
        self._policies.append(policy)

    def remove_policy(self, policy_id: str) -> bool:
        """Remove a policy by ID. Returns ``True`` if found."""
        before = len(self._policies)
        self._policies = [p for p in self._policies if p.policy_id != policy_id]
        return len(self._policies) < before

    def check(self, command: Command) -> tuple[bool, str, list[str]]:
        """Check command against all matching policies.

        Returns ``(allowed, message, warnings)``.
        """
        warnings: list[str] = []

        matching = self._match_policies(command)
        if not matching:
            return True, "", []

        for policy in matching:
            for rule in policy.rules:
                if not self._rule_matches_operation(rule, command.operation):
                    continue

                action = rule.action  # allow, deny, require_approval, warn

                if action == "deny" and policy.enforcement == Enforcement.ENFORCE:
                    logger.warning(
                        "policy_denied",
                        policy_id=policy.policy_id,
                        operation=command.operation,
                        rule_condition=rule.condition,
                    )
                    return False, f"Denied by policy: {rule.condition}", warnings

                if (
                    action == "require_approval"
                    and policy.enforcement == Enforcement.ENFORCE
                ):
                    logger.warning(
                        "policy_requires_approval",
                        policy_id=policy.policy_id,
                        operation=command.operation,
                    )
                    return False, f"Approval required: {rule.condition}", warnings

                if action == "deny" and policy.enforcement == Enforcement.WARN:
                    warnings.append(
                        f"Policy warning ({policy.policy_id}): {rule.condition}"
                    )
                    logger.info("policy_warning", policy_id=policy.policy_id)

                if action == "deny" and policy.enforcement == Enforcement.AUDIT_ONLY:
                    logger.info(
                        "policy_audit",
                        policy_id=policy.policy_id,
                        operation=command.operation,
                    )
                    # audit_only: don't block, don't warn

        return True, "", warnings

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _match_policies(self, command: Command) -> list[Policy]:
        """Find policies whose scope matches the command.

        Returns policies sorted by specificity (global first, entity_type last).
        """
        matching: list[Policy] = []

        for policy in self._policies:
            level = policy.scope.level
            value = policy.scope.value

            if (
                level == "global"
                or (level == "domain" and command.metadata.get("domain") == value)
                or (level == "team" and command.metadata.get("team") == value)
                or (level == "entity_type" and command.target_type == value)
            ):
                matching.append(policy)

        matching.sort(key=lambda p: _SCOPE_SPECIFICITY.get(p.scope.level, 99))
        return matching

    @staticmethod
    def _rule_matches_operation(rule: PolicyRule, operation: str) -> bool:
        """Check if a rule applies to a given operation."""
        if rule.operation == "*":
            return True
        if rule.operation == operation:
            return True
        # Wildcard prefix: "entity.*" matches "entity.create", etc.
        if rule.operation.endswith(".*"):
            prefix = rule.operation[:-2]
            return operation.startswith(prefix + ".")
        return False
