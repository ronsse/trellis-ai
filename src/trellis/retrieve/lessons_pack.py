"""The ``PACK_ASSEMBLED`` event for ``get_lessons``, behind a default-off knob.

``get_lessons`` serves promoted precedents. Without this event nothing it
serves can be graded, because ``record_feedback`` joins an agent's verdict to
the ``PACK_ASSEMBLED`` row that carries the pack's ``injected_items[]``.

The event is written only when ``TRELLIS_GET_LESSONS_PACK_EVENT`` is on
(:func:`trellis.retrieve.builder_factory._get_lessons_pack_event_enabled`).
Off, ``get_lessons`` replies exactly as it did before and writes nothing, so
a reader that counts retrievals from ``PACK_ASSEMBLED`` sees no new stream.

**Holdout exemption.** ``get_lessons`` is never drawn for the pack holdout,
and the row carries no ``holdout`` or ``holdout_rate`` key, so the holdout
analysis reads it as it reads a row written before the experiment: never a
second rate in the window, never a unit. ``is_holdout`` needs a literal
``True``, so ``drop_holdout`` keeps the row and the feedback naming it, and
:func:`trellis.core.variants.active_variants` stamps ``variants == {}``: this
pack is under no experiment. Nothing here calls ``holdout_draw`` or
``is_held_out``, and the reply is always the full list. The knob also reads
as off while a holdout rate is set, so no lessons row is written inside an
experiment's window at all.
"""

from __future__ import annotations

from typing import Any

import structlog

from trellis.core.ids import generate_ulid
from trellis.core.variants import VARIANTS_KEY, active_variants
from trellis.retrieve.token_tracker import estimate_tokens
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger(__name__)

#: ``payload["surface"]`` of the event. Pack rows from ``PackBuilder`` carry no
#: such key, so a reader can tell a lessons row from a built pack.
GET_LESSONS_SURFACE = "get_lessons"

#: ``injected_items[].item_type`` and ``strategy_source`` of a served lesson.
LESSON_ITEM_TYPE = "precedent"


def lessons_pack_payload(
    pack_id: str,
    served: list[dict[str, Any]],
    *,
    domain: str | None,
    max_tokens: int,
) -> dict[str, Any]:
    """The ``PACK_ASSEMBLED`` payload for the lessons that were rendered.

    ``injected_items[]`` rows have the keys ``PackBuilder`` writes, with
    ``item_id`` the precedent's entity id; a lesson that did not fit the token
    budget is not a row, because it was not served.
    """
    rows: list[dict[str, Any]] = [
        {
            "item_id": str(lesson.get("entity_id") or ""),
            "item_type": LESSON_ITEM_TYPE,
            "rank": rank,
            "selection_reason": "promoted_precedent",
            "score_breakdown": {},
            "estimated_tokens": estimate_tokens(
                f"{lesson.get('title', '')}\n{lesson.get('description', '')}"
            ),
            "strategy_source": GET_LESSONS_SURFACE,
            "injected_advisory_ids": [],
            "title": lesson.get("title") or None,
            "category": None,
            "domain_system": None,
            "domain": [lesson["domain"]] if lesson.get("domain") else [],
            "signal_quality": None,
        }
        for rank, lesson in enumerate(served, start=1)
    ]
    ids = [row["item_id"] for row in rows]
    return {
        "surface": GET_LESSONS_SURFACE,
        "pack_id": pack_id,
        "intent": "get_lessons",
        "domain": domain,
        "agent_id": None,
        "session_id": None,
        "run_id": None,
        "intent_family": None,
        "items_count": len(rows),
        "injected_item_ids": ids,
        "injected_items": rows,
        "budget_max_items": len(rows),
        "budget_max_tokens": max_tokens,
        # No holdout keys: see the module docstring.
        VARIANTS_KEY: active_variants(holdout_rate=0.0, held_out=False),
    }


def record_lessons_pack(
    event_log: EventLog,
    served: list[dict[str, Any]],
    *,
    rendered: str,
    domain: str | None,
    max_tokens: int,
) -> str:
    """Emit the event and return ``rendered`` with the pack id and ids appended.

    The reply gains a footer only once the event is stored, so it never names
    a ``pack_id`` that ``record_feedback`` cannot find. A failed emit is
    post-success telemetry: it is logged and the reply stays unchanged.
    """
    pack_id = generate_ulid()
    try:
        event_log.emit(
            EventType.PACK_ASSEMBLED,
            source="mcp.get_lessons",
            entity_id=pack_id,
            entity_type="pack",
            payload=lessons_pack_payload(
                pack_id, served, domain=domain, max_tokens=max_tokens
            ),
        )
    except Exception:
        # GRACEFUL-DEGRADATION: the lessons were read; losing their grade
        # handle must not fail the call.
        logger.exception("get_lessons_pack_event_failed")
        return rendered
    footer = [f"**pack_id:** `{pack_id}`", "", "Served ids (cite in record_feedback):"]
    footer.extend(
        f"- `{lesson.get('entity_id', '')}` {lesson.get('title', 'Untitled')}"
        for lesson in served
    )
    return f"{rendered}\n\n" + "\n".join(footer)
