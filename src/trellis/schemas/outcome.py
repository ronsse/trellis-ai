"""OutcomeEvent schema — per-call signal for the self-learning loop.

OutcomeEvents are the raw, high-volume signal stream consumed by the
self-learning loop's tuners (parameter optimization, advisory fitness,
precedent promotion).  They are deliberately separate from the audit
EventLog: the EventLog holds a curated trail of governance-visible events
(``PARAMS_UPDATED``, ``TUNER_PROPOSAL_CREATED``, etc.) while raw call-
level outcomes live in a dedicated ops store with rollup semantics.

Identity vs. learning vs. audit dimensions
------------------------------------------

An OutcomeEvent carries three kinds of fields:

* **Learning axes** — the dimensions along which tuners learn and
  backoff: ``domain``, ``intent_family``, ``tool_name``, ``phase``.
  Component decisions key off of these.

* **Identity axes** — what component made the call and under which
  parameter version: ``component_id``, ``params_version``.

* **Audit axes** — who/when/where the call happened: ``agent_id``,
  ``agent_role``, ``run_id``, ``session_id``, ``pack_id``, ``trace_id``,
  ``occurred_at``.  Tuners ignore these; operators use them to
  reconstruct individual calls.

The ``ComponentOutcome`` nested type captures the call result: did it
succeed, how long did it take, what was referenced, and a freeform
``metrics`` dict for component-specific numeric signals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from pydantic import Field

from trellis.core.base import VersionedModel, utc_now
from trellis.core.ids import generate_ulid

# ---------------------------------------------------------------------------
# Intent family catalog — the 10 well-known verbs agents use to describe
# their task.  Callers may pass arbitrary custom strings; the catalog is
# a soft default used for cold-start backoff and closest-match warnings.
# ---------------------------------------------------------------------------

INTENT_FAMILIES: Final[tuple[str, ...]] = (
    "discover",
    "lookup",
    "verify",
    "diagnose",
    "plan",
    "implement",
    "review",
    "summarize",
    "compare",
    "classify",
)

# ---------------------------------------------------------------------------
# Phase catalog — the 7 pipeline stages a call can occur in.
# ---------------------------------------------------------------------------

PHASES: Final[tuple[str, ...]] = (
    "ingest",
    "enrich",
    "extract",
    "retrieve",
    "assemble",
    "advise",
    "feedback",
)


class ComponentOutcome(VersionedModel):
    """The call-level result recorded alongside an OutcomeEvent.

    Named ``ComponentOutcome`` to avoid collision with
    :class:`trellis.schemas.trace.Outcome` which captures the outer
    trace-level status.  A single trace may produce many
    ``ComponentOutcome`` records — one per governed call within it.

    Fields:
        success: Coarse boolean — did the call produce a usable result?
            Tuners treat this as the primary reward signal.
        latency_ms: Wall-clock duration of the call in milliseconds.
            Tuners use this as a cost signal.
        items_served: Number of items the component returned.  Optional;
            only meaningful for retrieve/assemble phases.
        items_referenced: Subset of items that downstream consumers
            actually used.  Optional; populated when the caller has a
            reference signal (e.g. agent citations).
        metrics: Freeform numeric signals specific to the component.
            Recommended keys: ``precision``, ``recall``, ``confidence``,
            ``tokens_used``, ``cache_hit_rate``.  Tuners opt-in to
            specific keys; unknown keys are preserved but ignored.
        error: Optional error message when ``success=False``.  Kept
            short (the full stack trace belongs in logs).
    """

    success: bool
    latency_ms: float = Field(ge=0.0)
    items_served: int | None = Field(default=None, ge=0)
    items_referenced: int | None = Field(default=None, ge=0)
    metrics: dict[str, float] = Field(default_factory=dict)
    error: str | None = None


class OutcomeEvent(VersionedModel):
    """A single governed call's signal.

    Emitted by :func:`trellis.ops.record_outcome` from every tuneable
    component in Trellis (rerankers, strategies, classifiers, advisory
    generator, extraction dispatcher, ...).  Stored in the
    :class:`~trellis.stores.base.outcome.OutcomeStore` — **not** the
    EventLog — because the volume is too high for audit-tier storage
    and the consumers are different.

    Learning axes
    -------------

    Tuners key parameter cells and rollups off ``(component_id, domain,
    intent_family, tool_name)`` with a backoff chain::

        (component_id, domain, intent_family, tool_name)
        -> (component_id, domain, intent_family)
        -> (component_id, domain)
        -> (component_id, intent_family)
        -> (component_id)

    ``domain`` is single-valued per call.  Multi-domain items back off
    to wider cells.

    ``phase`` is a loose hierarchical axis separate from ``component_id``
    and ``tool_name`` — a single component like ``PackBuilder`` crosses
    the ``retrieve`` and ``assemble`` phases.

    Identity axes
    -------------

    ``component_id`` is the stable name of the emitting component (e.g.
    ``retrieve.strategies.KeywordSearch``).  ``params_version`` pins the
    parameter snapshot in effect; the :class:`ParameterStore` holds the
    full snapshot so tuners can correlate outcomes with the exact
    parameters in effect.

    ``agent_role`` is the deployment-time role name of the calling agent
    (e.g. ``claude-code``, ``trellis.classifier`` for internal LLM
    agents).  Distinct from ``agent_id`` which is a runtime instance
    identifier.

    Audit axes
    ----------

    Tuners ignore ``run_id`` / ``session_id`` / ``pack_id`` / ``trace_id``;
    operators use them to reconstruct individual calls.  ``occurred_at``
    is the call start time.
    """

    # --- Identity
    event_id: str = Field(default_factory=generate_ulid)
    component_id: str
    params_version: str | None = None

    # --- Learning axes
    domain: str | None = None
    intent_family: str | None = None
    tool_name: str | None = None
    phase: str | None = None
    agent_role: str | None = None

    # --- Audit axes
    agent_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    pack_id: str | None = None
    trace_id: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    recorded_at: datetime = Field(default_factory=utc_now)

    # --- Outcome payload
    outcome: ComponentOutcome

    # --- Policy-assigned (reserved for cohort / segment routing)
    cohort: str | None = None
    segment: str | None = None

    # --- Freeform metadata (non-learning, non-audit)
    metadata: dict[str, Any] = Field(default_factory=dict)
