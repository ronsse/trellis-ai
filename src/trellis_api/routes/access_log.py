"""Admin read of the per-key access log: last-used time and read counts per key.

Derived from the ``access.key_read`` events alone (:mod:`trellis.core.access_log`);
the key store is read only for each key's name and revoked flag. Admin scope,
and refused to an ``external`` key like every route that reads something with no
sensitivity label.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from trellis.core.access_log import KeyReadReport, summarize_key_reads
from trellis_api.app import get_registry
from trellis_api.routes._principal import NOT_FOR_PUBLIC_CALLERS

router = APIRouter()

#: 7 days; 90 days at most (the scan is capped and says so when it bites).
DEFAULT_WINDOW_HOURS = 168.0
MAX_WINDOW_HOURS = 24.0 * 90


@router.get(
    "/access-log/keys",
    response_model=KeyReadReport,
    dependencies=NOT_FOR_PUBLIC_CALLERS,
)
def key_reads(
    hours: float = Query(
        DEFAULT_WINDOW_HOURS, gt=0, le=MAX_WINDOW_HOURS, description="Window, hours"
    ),
    key_id: str | None = Query(None, description="Only this key"),
) -> KeyReadReport:
    """Per key: reads, items returned and last read inside the window.

    A key last used before the window is absent. ``truncated`` says the scan
    cap bit and older reads in the window are missing; ``log_failures`` counts
    reads the log failed to record.
    """
    registry = get_registry()
    return summarize_key_reads(
        registry.operational.event_log,
        window_hours=hours,
        key_id=key_id,
        key_store=registry.operational.api_key_store,
    )
