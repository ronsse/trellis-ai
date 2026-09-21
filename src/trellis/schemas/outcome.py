"""OutcomeEvent schema — per-call signal for the feedback loop.

OutcomeEvents are the raw, high-volume signal stream consumed by the
feedback loop's tuners (parameter optimization, advisory fitness,
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


# ---------------------------------------------------------------------------
# Component-id vocabulary — the ``component_id`` axis of an OutcomeEvent.
#
# ``component_id`` is a *join key* between three sites that are written
# independently and never import each other: the component that resolves its
# parameters under the id (``retrieve.strategies``), the tuning rule that
# targets the id (``learning.tuners.rule_tuner.DEFAULT_RULES``), and the
# feedback bridge that stamps the id onto an emitted outcome
# (``feedback.recording``).  Spelled inline at each site, they drift — the
# same failure :data:`trellis.schemas.memory_op.REF_TYPE_DOCUMENT` exists to
# prevent, and the one #557 found live: the bridge stamped the PackBuilder
# while every shipped rule targeted a strategy, so the two halves of the
# learning loop could not meet.  They live here, beside the other axis
# vocabularies, because ``schemas`` is the one package all three import.
# ---------------------------------------------------------------------------

PACK_BUILDER_COMPONENT_ID: Final = "retrieve.pack_builder.PackBuilder"
KEYWORD_SEARCH_COMPONENT_ID: Final = "retrieve.strategies.KeywordSearch"
SEMANTIC_SEARCH_COMPONENT_ID: Final = "retrieve.strategies.SemanticSearch"
GRAPH_SEARCH_COMPONENT_ID: Final = "retrieve.strategies.GraphSearch"
OBSERVATION_SEARCH_COMPONENT_ID: Final = "retrieve.strategies.ObservationSearch"
RRF_RERANKER_COMPONENT_ID: Final = "retrieve.rerankers.RRFReranker"
MMR_RERANKER_COMPONENT_ID: Final = "retrieve.rerankers.MMRReranker"

#: Maps a served item's ``strategy_source`` — the short name a strategy
#: writes onto every item it returns, carried through to
#: ``PACK_ASSEMBLED.injected_items[].strategy_source`` — to the
#: ``component_id`` that strategy reads its parameters under.
#:
#: This is what lets a pack's per-item record be re-expressed as
#: per-component outcomes: the pack knows *which strategy served each item*,
#: so it supplies the denominator (``items_served``) that an agent's citation
#: signal cannot.  A ``strategy_source`` with no entry here is **dropped**
#: rather than guessed at — an unattributable serving must not inflate some
#: other component's denominator.
#:
#: Rerankers are deliberately absent.  A reranker reorders every candidate
#: and serves none of them under its own name, so no item ever carries its
#: ``strategy_source``; a rule targeting one cannot be reached by this map,
#: and pretending otherwise would manufacture the attribution.
COMPONENT_ID_BY_SOURCE_STRATEGY: Final[dict[str, str]] = {
    "keyword": KEYWORD_SEARCH_COMPONENT_ID,
    "semantic": SEMANTIC_SEARCH_COMPONENT_ID,
    "graph": GRAPH_SEARCH_COMPONENT_ID,
    "observation": OBSERVATION_SEARCH_COMPONENT_ID,
}


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
