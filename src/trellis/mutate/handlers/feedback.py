"""The ``feedback.record`` handler."""

from __future__ import annotations

from typing import Any

from trellis.feedback.models import SUCCESS_RATING_THRESHOLD
from trellis.mutate.commands import Command
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


class FeedbackRecordHandler:
    """Emit FEEDBACK_RECORDED event.

    ``pack_id`` is forwarded when the caller supplied one. It is the join
    key: :func:`trellis.learning.pack_observations.join_pack_feedback`
    matches feedback to packs strictly on ``payload["pack_id"]`` and skips
    events that lack it. ``POST /feedback`` has accepted a ``pack_id``
    field since the wire DTO was written ("Link feedback to a context
    pack") and the route has always put it in ``command.args`` — but this
    handler dropped it before emitting, so the link the caller asked for
    was never made and the event could not join. Nothing infers a pack
    here: an absent ``pack_id`` stays absent.

    ``success`` rides along with it, and has to. Every consumer reads
    ``payload["success"]`` and only then falls back to ``rating``;
    ``_join_one`` in particular resolves an absent key to ``"failure"``.
    Forwarding the join key alone would therefore have made a governed
    ``rating=0.9`` join as a *failed* delivery — a wrong signal reaching
    the loop, which is worse than the unjoinable silence it replaced. The
    derivation is :data:`~trellis.feedback.models.SUCCESS_RATING_THRESHOLD`,
    the same one :meth:`PackFeedback.from_agent_signal` applies, so the
    two feedback families cannot disagree about what a given rating means.

    This path still carries no per-item attribution — the governed
    command schema has no place for it, and the surfaces that do (the MCP
    ``record_feedback`` tool, ``POST /packs/{pack_id}/feedback``) route
    through ``PackFeedback`` instead. A joined observation from here has
    an outcome and no cited items, which is exactly what was sent.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        rating = command.args["rating"]
        payload: dict[str, Any] = {
            "target_id": command.args["target_id"],
            "rating": rating,
            "comment": command.args.get("comment"),
        }
        if isinstance(rating, int | float):
            payload["success"] = float(rating) >= SUCCESS_RATING_THRESHOLD
        pack_id = command.args.get("pack_id")
        if isinstance(pack_id, str) and pack_id.strip():
            payload["pack_id"] = pack_id.strip()
        event = self._registry.operational.event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mutation_executor",
            entity_id=command.target_id,
            payload=payload,
        )
        return event.event_id, f"Feedback recorded: rating={command.args['rating']}"
