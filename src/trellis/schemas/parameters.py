"""Parameter snapshot schemas — versioned, scoped values for tuneable components.

The ops layer treats component parameters as immutable versioned
snapshots keyed by a learning-axis scope.  Tuners propose a new
:class:`ParameterSet` via the governed mutation pipeline; once promoted
it becomes the active snapshot for its scope and receives a monotonic
``params_version``.

Precedence chain for resolution::

    (component_id, domain, intent_family, tool_name)
    -> (component_id, domain, intent_family)
    -> (component_id, domain)
    -> (component_id, intent_family)
    -> (component_id)

A missing optional axis means the value is unset at that scope, not a
wildcard — callers must walk the chain to find the first match.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from trellis.core.base import VersionedModel, utc_now
from trellis.core.ids import generate_ulid


class ParameterScope(VersionedModel):
    """The learning-axis scope a parameter snapshot binds to.

    A scope with only ``component_id`` is the component-wide default.
    Adding axes narrows the scope; resolution uses the narrowest match
    first and falls back through the precedence chain when axes are
    missing.
    """

    component_id: str
    domain: str | None = None
    intent_family: str | None = None
    tool_name: str | None = None

    def key(self) -> tuple[str, str | None, str | None, str | None]:
        """Return the tuple used as the deterministic scope key."""
        return (self.component_id, self.domain, self.intent_family, self.tool_name)

    def specificity(self) -> int:
        """Return the count of non-None learning axes beyond ``component_id``.

        Used by the resolver to pick the narrowest match.
        """
        axes = (self.domain, self.intent_family, self.tool_name)
        return sum(1 for v in axes if v is not None)


class ParameterSet(VersionedModel):
    """An immutable snapshot of parameters for one scope.

    Parameters are freeform ``dict[str, float | int | str | bool]`` so
    each component can define its own keys without schema churn.  The
    tuner and consumers must agree on keys; validation happens inside
    each component, not here.

    ``params_version`` is the id used by :class:`OutcomeEvent` to pin
    which snapshot was in effect for a call.  It's monotonic per scope.
    """

    params_version: str = Field(default_factory=generate_ulid)
    scope: ParameterScope
    values: dict[str, float | int | str | bool] = Field(default_factory=dict)
    source: str = "default"  # "default" | "tuner:<name>" | "operator"
    created_at: datetime = Field(default_factory=utc_now)
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParameterProposal(VersionedModel):
    """A pending parameter change awaiting canary / promotion.

    Emitted by tuners and persisted in the :class:`TunerStateStore`.
    The governed mutation pipeline reads proposals, runs the canary
    gate, and on success calls :meth:`ParameterStore.put` with the
    resulting :class:`ParameterSet`.
    """

    proposal_id: str = Field(default_factory=generate_ulid)
    scope: ParameterScope
    proposed_values: dict[str, float | int | str | bool] = Field(default_factory=dict)
    baseline_version: str | None = None
    tuner: str
    created_at: datetime = Field(default_factory=utc_now)
    sample_size: int = Field(default=0, ge=0)
    effect_size: float | None = None
    status: str = "pending"  # "pending" | "canary" | "promoted" | "rejected"
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
