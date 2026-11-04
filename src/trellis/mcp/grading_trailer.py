"""The grading trailer on an MCP retrieval reply, behind ``TRELLIS_GRADING_TRAILER``."""

from __future__ import annotations

import structlog

from trellis.core.write_config import resolve_write_behaviour
from trellis.stores.registry import StoreRegistry
from trellis_wire.loop_text import grading_trailer

logger = structlog.get_logger(__name__)


def with_grading_trailer(
    registry: StoreRegistry, reply: str, pack_id: str | None
) -> str:
    """``reply`` plus a one-line trailer on how to grade the pack, when enabled.

    Off by default, and then ``reply`` comes back untouched, so a build with the
    knob unset serves what it served before the knob existed. The line comes
    from :func:`trellis_wire.loop_text.grading_trailer`, which names the pack
    and the minimal grading call and echoes nothing but a well-formed pack id
    (``get_items`` takes the id from the caller).

    Wrapped whole: the reply is already assembled, and one escaped exception
    here would fail a retrieval for the sake of a hint.
    """
    try:
        if not resolve_write_behaviour(registry).grading_trailer:
            return reply
        line = grading_trailer(pack_id)
    except Exception:
        # GRACEFUL-DEGRADATION: wrapped whole, not by exception type. The
        # trailer is advice appended to a reply that is already complete.
        logger.exception("grading_trailer_failed", pack_id=pack_id)
        return reply
    return reply if line is None else f"{reply}\n{line}"
