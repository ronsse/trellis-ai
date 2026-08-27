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

    **Deny wins.** Every policy whose scope matches the command is
    evaluated, and the first rule that resolves to a block stops the
    command. Scope specificity (``global`` < ``domain`` < ``team`` <
    ``entity_type``) determines *evaluation order only* — it is not an
    override mechanism. A narrow ``allow`` therefore cannot carve an
    exception out of a broad ``deny``: the broad policy is evaluated
    first and returns. This is the same posture as an explicit-deny-wins
    IAM policy, and it is deliberate — for an access-control mechanism,
    the conservative resolution is the safe one. Express an exception by
    narrowing the ``deny`` rule's ``operation`` pattern or its scope, not
    by layering an ``allow`` on top of it.

    ``allow`` rules are consequently inert: they document intent and
    match the default posture, but they grant nothing that was not
    already permitted (a command matching no policy is allowed).

    Enforcement level scales what a matching rule *does*, never which
    rules match:

    ==================  ==========================  =====================
    ``rule.action``     ``enforce``                 ``warn`` / ``audit_only``
    ==================  ==========================  =====================
    ``allow``           pass                        pass
    ``deny``            **block**                   warn / log only
    ``require_approval``  **block**                 warn / log only
    ``warn``            warn                        warn / log only
    ==================  ==========================  =====================

    Warnings are returned to the caller and surface on the resulting
    ``CommandResult.warnings``; ``audit_only`` is silent to the caller
    and leaves only a structlog record.
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

        Returns ``(allowed, message, warnings)``. An empty gate — and any
        gate whose policies do not match this command — returns
        ``(True, "", [])``, which the executor treats as a pass-through.
        """
        warnings: list[str] = []

        matching = self._match_policies(command)
        if not matching:
            return True, "", []

        for policy in matching:
            for rule in policy.rules:
                if not self._rule_matches_operation(rule, command.operation):
                    continue

                # ``allow`` grants nothing that is not already the default
                # posture, so it never blocks and never warns.
                if rule.action == "allow":
                    continue

                blocking = rule.action in ("deny", "require_approval")

                if blocking and policy.enforcement == Enforcement.ENFORCE:
                    message = (
                        f"Denied by policy: {rule.condition}"
                        if rule.action == "deny"
                        else f"Approval required: {rule.condition}"
                    )
                    logger.warning(
                        "policy_denied"
                        if rule.action == "deny"
                        else "policy_requires_approval",
                        policy_id=policy.policy_id,
                        operation=command.operation,
                        rule_condition=rule.condition,
                    )
                    return False, message, warnings

                if policy.enforcement == Enforcement.AUDIT_ONLY:
                    # Silent to the caller by definition; the structlog
                    # record is the whole point of the level.
                    logger.info(
                        "policy_audit",
                        policy_id=policy.policy_id,
                        operation=command.operation,
                        action=rule.action,
                    )
                    continue

                # Reaches here for: a blocking action under WARN
                # enforcement, or an ``action="warn"`` rule under either
                # ENFORCE or WARN. All three mean "allow, but say so".
                warnings.append(
                    f"Policy warning ({policy.policy_id}): {rule.condition}"
                )
                logger.info(
                    "policy_warning",
                    policy_id=policy.policy_id,
                    operation=command.operation,
                    action=rule.action,
                )

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
