"""Configurable retrieval budgets.

Resolves per-tool and per-domain budget overrides from the ``retrieval.budgets``
section of ``~/.config/trellis/config.yaml``.  Resolution order:

1. Explicit caller override (``max_tokens`` argument on a context tool)
2. Tool-specific config under ``by_tool``
3. Global defaults under ``default``
4. Hardcoded fallbacks (``max_tokens=4000``, ``max_items=30``)

A domain multiplier under ``by_domain`` can scale the resolved value up or
down for specific domains (e.g. a domain that needs more context).

Example config::

    retrieval:
      budgets:
        default:
          max_tokens: 4000
          max_items: 30
        by_tool:
          get_objective_context:
            max_tokens: 3500
          get_task_context:
            max_tokens: 2500
        by_domain:
          sportsbook:
            max_tokens_multiplier: 1.25
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.schemas.pack import PackBudget

# Hardcoded fallbacks if nothing is configured
_FALLBACK_MAX_TOKENS = 4000
_FALLBACK_MAX_ITEMS = 30


class BudgetSpec(TrellisModel):
    """A max_tokens + max_items pair used for default and per-tool entries."""

    max_tokens: int = _FALLBACK_MAX_TOKENS
    max_items: int = _FALLBACK_MAX_ITEMS


class DomainBudgetSpec(TrellisModel):
    """A per-domain multiplier applied on top of the resolved budget."""

    max_tokens_multiplier: float = 1.0
    max_items_multiplier: float = 1.0


class BudgetConfig(TrellisModel):
    """Resolves retrieval budgets from configuration.

    Loaded from the ``retrieval.budgets`` section of ``config.yaml``.
    """

    default: BudgetSpec = Field(default_factory=BudgetSpec)
    by_tool: dict[str, BudgetSpec] = Field(default_factory=dict)
    by_domain: dict[str, DomainBudgetSpec] = Field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BudgetConfig:
        """Build a ``BudgetConfig`` from a raw config dict.

        Accepts the ``retrieval.budgets`` sub-dict.  Missing or malformed
        entries fall back to defaults rather than raising.
        """
        if not data:
            return cls()
        try:
            return cls.model_validate(data)
        except Exception:
            return cls()

    def resolve(
        self,
        *,
        tool: str,
        domain: str | None = None,
        caller_override_tokens: int | None = None,
        caller_override_items: int | None = None,
    ) -> PackBudget:
        """Resolve a :class:`PackBudget` for a tool/domain pair.

        Args:
            tool: The tool name (e.g. ``"get_objective_context"``).
            domain: Optional domain name for multiplier lookup.
            caller_override_tokens: If provided and > 0, wins over config.
            caller_override_items: If provided and > 0, wins over config.
        """
        tool_spec = self.by_tool.get(tool, self.default)
        max_tokens = tool_spec.max_tokens
        max_items = tool_spec.max_items

        if domain:
            dom_spec = self.by_domain.get(domain)
            if dom_spec is not None:
                max_tokens = int(max_tokens * dom_spec.max_tokens_multiplier)
                max_items = int(max_items * dom_spec.max_items_multiplier)

        if caller_override_tokens is not None and caller_override_tokens > 0:
            max_tokens = caller_override_tokens
        if caller_override_items is not None and caller_override_items > 0:
            max_items = caller_override_items

        return PackBudget(max_tokens=max_tokens, max_items=max_items)
