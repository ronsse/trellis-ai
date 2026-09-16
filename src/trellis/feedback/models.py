"""PackFeedback model — feedback signal for a single context pack delivery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from trellis.core.ids import generate_prefixed_id

logger = structlog.get_logger(__name__)

#: Rating at or above which a delivery counts as a success.
#:
#: One value serving two roles. At *read* time the fitness loops use it
#: as the fallback when a ``FEEDBACK_RECORDED`` payload has no
#: ``success`` key (``retrieve.effectiveness`` /
#: ``retrieve.advisory_generator``, each overridable per component via
#: the ``success_rating_threshold`` parameter-registry key). At *write*
#: time :meth:`PackFeedback.from_agent_signal` uses it to derive
#: ``outcome`` from a graded call that gave no explicit ``success``.
#:
#: Note the asymmetry that follows: because :meth:`to_event_payload`
#: always emits an explicit ``success``, rows written through this model
#: are binarized at write time and re-tuning the registry parameter does
#: not re-grade them. Consumers that want the raw gradient should read
#: ``payload["rating"]``, which is always present.
SUCCESS_RATING_THRESHOLD = 0.5

#: ``outcome`` values that count as a win.
_SUCCESS_OUTCOMES = frozenset({"success", "completed"})


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

    @property
    def succeeded(self) -> bool:
        """Whether ``outcome`` records a win."""
        return self.outcome in _SUCCESS_OUTCOMES

    @property
    def effective_rating(self) -> float:
        """The grade consumers see — explicit ``rating``, else 1.0/0.0.

        Ungraded feedback still has to answer "how good was it?", and
        the honest boolean-only answer is the outcome itself.
        """
        return self.rating if self.rating is not None else float(self.succeeded)

    @classmethod
    def from_agent_signal(
        cls,
        *,
        run_id: str,
        success: bool | None = None,
        rating: float | None = None,
        helpful_item_ids: Sequence[str] = (),
        unhelpful_item_ids: Sequence[str] = (),
        followed_advisory_ids: Sequence[str] = (),
        pack_id: str | None = None,
        trace_id: str | None = None,
        notes: str | None = None,
    ) -> PackFeedback:
        """Build feedback from what an agent-facing surface can observe.

        Shared by the MCP ``record_feedback`` tool and the REST
        ``POST /packs/{id}/feedback`` route so the two surfaces cannot
        drift on what identical inputs mean.

        Two decisions worth stating outright:

        * **``success`` is derived from ``rating`` when not given.**
          Every consumer reads ``payload["success"]`` first and only
          falls back to ``rating``, so a graded call that also shipped a
          defaulted ``success=True`` would be read as a plain win and the
          gradient would never reach the loops. An explicit ``success``
          still wins — the caller is making a claim
          :data:`SUCCESS_RATING_THRESHOLD` should not overrule.
        * **``items_served`` stays empty.** The cited ids are what the
          agent *referenced*, not what the pack *contained*; unioning
          them would claim a 100% reference rate for every graded pack.
          Empty is falsy, so
          :func:`~trellis.retrieve.metrics_timeseries.compute_timeseries`
          keeps falling back to the joined ``PACK_ASSEMBLED``
          ``injected_item_ids`` — the real served list — and
          :func:`~trellis.feedback.aggregation.compute_item_effectiveness`
          skips the row rather than scoring a fabricated one.

        Args:
            run_id: Feedback ``run_id`` (the trace or pack the signal
                belongs to).
            success: Explicit outcome claim, or ``None`` to derive it.
            rating: Graded usefulness, 0.0 to 1.0. ``None`` means
                ungraded and ``to_event_payload`` falls back to 1.0/0.0.
            helpful_item_ids: Items the agent actually used.
            unhelpful_item_ids: Items the agent found to be noise.
            followed_advisory_ids: Advisories whose guidance was acted on.
            pack_id: Stamped into ``metadata`` — it is not a
                :class:`PackFeedback` field, so this is what lets
                :func:`~trellis.feedback.recording.reconcile_feedback_log_to_event_log`
                replay a row with its pack association after a
                soft-failed emit.
            trace_id: Stamped into ``metadata`` for the same reason.
            notes: Free text; the only thing that stays in ``metadata``
                because no consumer reads it structurally.
        """
        if success is None:
            succeeded = (
                rating >= SUCCESS_RATING_THRESHOLD if rating is not None else True
            )
        else:
            succeeded = success

        metadata: dict[str, Any] = {}
        if pack_id:
            metadata["pack_id"] = pack_id
        if trace_id:
            metadata["trace_id"] = trace_id
        if notes:
            metadata["notes"] = notes

        return cls(
            run_id=run_id,
            phase="feedback",
            intent="",
            outcome="success" if succeeded else "failure",
            items_served=[],
            items_referenced=list(helpful_item_ids),
            rating=rating,
            unhelpful_item_ids=list(unhelpful_item_ids),
            followed_advisory_ids=list(followed_advisory_ids),
            metadata=metadata,
        )

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
        success = self.succeeded
        payload: dict[str, Any] = {
            "feedback_id": self.feedback_id,
            "run_id": self.run_id,
            "phase": self.phase,
            "intent": self.intent,
            "intent_family": self.intent_family,
            "outcome": self.outcome,
            "success": success,
            "rating": self.effective_rating,
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

    @classmethod
    def from_event_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        default_timestamp_utc: str | None = None,
    ) -> PackFeedback | None:
        """Rebuild the feedback that wrote a ``FEEDBACK_RECORDED`` payload.

        The inverse of :meth:`to_event_payload`, for replaying history
        through a bridge that did not exist when the event was written
        (:mod:`trellis.feedback.backfill`).

        Returns ``None`` for a payload this model did not write.  The
        discriminator is ``feedback_id``, which :meth:`to_event_payload`
        always emits and the governed surface never does — ``CLAUDE.md``
        § "Feedback path" tabulates the two families, and the governed
        one emits ``{target_id, rating, comment, success}``.  That is a
        fact about **which writer produced the row**, not an inference
        from its contents: those rows never reached the outcome bridge,
        they carry no item attribution to fan out, and replaying them
        would hand every strategy a fabricated ``0/N`` reference rate.

        Two things do not survive the round trip.  ``rating`` is written
        as :attr:`effective_rating`, so an ungraded signal comes back
        graded 1.0/0.0 and the payload cannot distinguish it from a real
        grade — nothing in the outcome bridge reads ``rating``.  And
        ``pack_id`` is not a field of this model: it rides the payload
        alongside, and callers pass it to the bridge themselves.

        Args:
            payload: A ``FEEDBACK_RECORDED`` event payload.
            default_timestamp_utc: Used when the payload carries no
                ``timestamp_utc``.  Pass the event row's own
                ``occurred_at`` — the outcome bridge stamps
                ``OutcomeEvent.occurred_at`` from this field, so
                falling through to "now" would file a historical row in
                the current window.

        Returns:
            The reconstructed feedback, or ``None`` when the payload
            carries no ``feedback_id``.
        """
        feedback_id = payload.get("feedback_id")
        if not isinstance(feedback_id, str) or not feedback_id:
            return None
        timestamp_utc = _coerce_str(payload.get("timestamp_utc"))
        if not timestamp_utc:
            timestamp_utc = default_timestamp_utc or datetime.now(UTC).isoformat()
        agent_id = payload.get("agent_id")
        return cls(
            run_id=_coerce_str(payload.get("run_id")),
            phase=_coerce_str(payload.get("phase")),
            intent=_coerce_str(payload.get("intent")),
            outcome=_coerce_str(payload.get("outcome")),
            items_served=_coerce_str_list(payload.get("items_served")),
            items_referenced=_coerce_str_list(payload.get("helpful_item_ids")),
            relevance_scores=_coerce_score_map(payload.get("relevance_scores")),
            rating=_coerce_rating(payload.get("rating")),
            unhelpful_item_ids=_coerce_str_list(payload.get("unhelpful_item_ids")),
            followed_advisory_ids=_coerce_str_list(
                payload.get("followed_advisory_ids")
            ),
            intent_family=_coerce_str(payload.get("intent_family")),
            timestamp_utc=timestamp_utc,
            agent_id=agent_id if isinstance(agent_id, str) and agent_id else None,
            metadata=_coerce_metadata(payload.get("metadata")),
            feedback_id=feedback_id,
        )


def _coerce_str(value: object) -> str:
    """A payload string, or ``""`` for anything else.

    Event payloads are jsonb on the Postgres backend and free-form JSON
    on SQLite, so a reader cannot assume a writer's types held.
    """
    return value if isinstance(value, str) else ""


def _coerce_str_list(value: object) -> list[str]:
    """The string members of a payload list, dropping anything else."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _coerce_score_map(value: object) -> dict[str, float]:
    """A payload ``item_id → score`` map, dropping unusable entries."""
    if not isinstance(value, dict):
        return {}
    return {
        key: float(score)
        for key, score in value.items()
        if isinstance(key, str)
        and isinstance(score, int | float)
        and not isinstance(score, bool)
    }


def _coerce_rating(value: object) -> float | None:
    """A payload rating, or ``None`` when absent or unusable."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _coerce_metadata(value: object) -> dict[str, Any]:
    """A payload metadata bag with non-string keys dropped."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}
