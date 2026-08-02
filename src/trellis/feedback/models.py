"""PackFeedback model — feedback signal for a single context pack delivery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from trellis.core.ids import generate_prefixed_id

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PackFeedback:
    """Feedback signal for a single context pack delivery.

    Captures which pack items were served, which the agent actually
    referenced, and the phase outcome so signals can be aggregated
    to tune retrieval ranking.

    ``feedback_id`` is a stable ULID minted at construction time. It is
    the idempotency key that bridges the JSONL append-log and the
    ``FEEDBACK_RECORDED`` EventLog entry, so a record or replay cannot
    double-count the same feedback in either source.
    """

    run_id: str
    phase: str
    intent: str
    outcome: str  # "success" | "failure" | "partial" | "unknown"
    items_served: list[str]  # item_ids from the pack
    items_referenced: list[str] = field(
        default_factory=list
    )  # items agent actually used
    relevance_scores: dict[str, float] = field(default_factory=dict)  # item_id → score
    # Graded quality of the delivery, 0.0 to 1.0.  ``None`` means "not
    # graded" and :meth:`to_event_payload` derives 1.0/0.0 from the
    # outcome — what boolean-only callers effectively sent before.  It is
    # a first-class field rather than a ``metadata`` key because the
    # fitness loops read ``payload["rating"]`` at the top level and
    # reconciliation replays JSONL rows through ``to_event_payload``; a
    # rating hidden in ``metadata`` would not survive that round trip in
    # a form those consumers can see.
    rating: float | None = None
    # Attribution the agent supplies alongside ``items_referenced``.
    # "Actively unhelpful" is a stronger claim than "not referenced" (see
    # :meth:`to_event_payload`) and a followed advisory is not a pack item
    # at all, so neither can be inferred from served/referenced.
    unhelpful_item_ids: list[str] = field(default_factory=list)
    followed_advisory_ids: list[str] = field(default_factory=list)
    intent_family: str = ""
    timestamp_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    agent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    feedback_id: str = field(default_factory=lambda: generate_prefixed_id("fb"))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (suitable for JSON serialization)."""
        return asdict(self)

    def to_event_payload(self, *, pack_id: str | None = None) -> dict[str, Any]:
        """Shape this feedback as a ``FEEDBACK_RECORDED`` event payload.

        Bridges the file-based PackFeedback wire format to the EventLog
        consumed by :class:`~trellis.retrieve.advisory_generator.AdvisoryGenerator`
        and :func:`~trellis.retrieve.effectiveness.analyze_effectiveness`.

        Semantic mapping:

        * ``items_referenced`` → ``helpful_item_ids``.  Referenced means
          the agent actually used the item, which is the positive signal
          AdvisoryGenerator looks for.
        * Items in ``items_served`` that are **not** referenced are left
          implicit rather than inferred into ``unhelpful_item_ids``.
          "Not referenced" is a weaker signal than "actively unhelpful";
          only the ``unhelpful_item_ids`` a caller states explicitly are
          emitted under that key.
        * ``outcome in {"success", "completed"}`` → ``success=True``.
        * ``rating`` is always emitted.  When ungraded it falls back to
          1.0/0.0 from ``success`` so the key is never missing — consumers
          read it as ``payload.get("rating", 0.0)`` and an absent key is
          indistinguishable from a genuine 0.0 grade.
        * ``unhelpful_item_ids`` / ``followed_advisory_ids`` are emitted
          only when populated, keeping the payload free of empty lists
          the way ``pack_id`` / ``agent_id`` / ``metadata`` are.

        Args:
            pack_id: Pack identifier, stored in ``payload.pack_id`` so
                AdvisoryGenerator can join against ``PACK_ASSEMBLED``
                events.  Callers should also pass this as the event's
                ``entity_id`` when emitting.
        """
        success = self.outcome in ("success", "completed")
        payload: dict[str, Any] = {
            "feedback_id": self.feedback_id,
            "run_id": self.run_id,
            "phase": self.phase,
            "intent": self.intent,
            "intent_family": self.intent_family,
            "outcome": self.outcome,
            "success": success,
            "rating": self.rating if self.rating is not None else float(success),
            "items_served": list(self.items_served),
            "helpful_item_ids": list(self.items_referenced),
            "relevance_scores": dict(self.relevance_scores),
            "timestamp_utc": self.timestamp_utc,
        }
        if self.unhelpful_item_ids:
            payload["unhelpful_item_ids"] = list(self.unhelpful_item_ids)
        if self.followed_advisory_ids:
            payload["followed_advisory_ids"] = list(self.followed_advisory_ids)
        if pack_id is not None:
            payload["pack_id"] = pack_id
        if self.agent_id is not None:
            payload["agent_id"] = self.agent_id
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload
