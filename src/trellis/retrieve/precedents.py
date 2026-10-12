"""Shared precedent listing logic."""

from __future__ import annotations

from typing import Any

from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.stores.base.event_log import DEFAULT_SCAN_LIMIT, Event, EventLog, EventType
from trellis.stores.base.graph import GraphStore

#: Hard ceiling on how far :func:`list_precedents` will widen its
#: over-fetch window chasing ``limit`` servable rows. Shares
#: :data:`~trellis.stores.base.event_log.DEFAULT_SCAN_LIMIT` with
#: :func:`~trellis.stores.base.event_log.scan_events` and the
#: ``/precedents/domains`` route's own scan, rather than inventing a
#: second number for "how much of the event log is reasonable to read
#: in one request" — this module is the exception that needs the window
#: to grow at all, since the filter it applies lives in a different
#: store than the one being paged.
_MAX_PRECEDENT_SCAN = DEFAULT_SCAN_LIMIT

#: Lifecycle states a current precedent listing withholds. Shared
#: between :func:`list_precedents` (which skips demoted/archived rows
#: before the ``limit`` cap, not after -- see :func:`is_withheld_precedent`) and
#: ``trellis_api.routes.retrieve.list_precedent_domains`` (which must
#: not count a withheld precedent's domain), so the two surfaces cannot
#: drift on which states "excluded" means.
WITHHELD_PRECEDENT_STATES = frozenset({"demoted", "archived"})


def is_withheld_precedent(graph_store: GraphStore | None, entity_id: str) -> bool:
    """Whether ``entity_id``'s current graph node is demoted or archived.

    ``False`` whenever there is nothing to check against: no store, no
    id, no node, or no lifecycle record at all -- matching
    :mod:`trellis.retrieve.lifecycle`'s own fail-open convention that an
    absent or malformed stamp is never by itself a reason to withhold.
    """
    if graph_store is None or not entity_id:
        return False
    node = graph_store.get_node(entity_id)
    if node is None:
        return False
    lifecycle = node.get("properties", {}).get(LIFECYCLE_KEY) or {}
    return lifecycle.get("state") in WITHHELD_PRECEDENT_STATES


def list_precedents(
    event_log: EventLog,
    *,
    domain: str | None = None,
    limit: int = 20,
    graph_store: GraphStore | None = None,
) -> list[dict[str, Any]]:
    """Query PRECEDENT_PROMOTED events, optionally filtered by domain.

    Returns a list of dicts (newest precedent first) with event_id,
    entity_id, title, description, domain, and occurred_at. The
    underlying ``get_events`` call uses ``order="desc"`` so the
    ``limit`` cap shows the most recently promoted precedents — with
    the default ``order="asc"`` the listing would show the *oldest*
    precedents and miss any new ones once the table grows past
    ``limit`` rows.

    When ``domain`` is set the predicate is pushed into the backend via
    :paramref:`EventLog.get_events.payload_filters`, so the ``limit``
    cap applies *after* the filter. The previous post-fetch shape would
    truncate the SQL window and then drop non-matching rows in Python,
    silently returning fewer than ``limit`` matches even when more
    matches existed beyond the cap.

    ``PRECEDENT_PROMOTED`` events are immutable (Hard Rule: traces/events
    never mutate), so a demoted precedent's original promotion event is
    returned by this scan forever, unconditionally — the event log alone
    has no lifecycle awareness. When ``graph_store`` is given, each row's
    current node is checked and skipped if :func:`is_withheld_precedent` (state
    ``"demoted"`` or ``"archived"``; retention archives a demoted
    precedent, and the pack collect seam withholds both states).

    The lifecycle filter is applied **before** the ``limit`` cap, not
    after: a naive "fetch ``limit``, then filter" shape can return fewer
    than ``limit`` rows even when ``limit``-or-more servable rows exist
    just past the window, because every demoted/archived row inside the
    window consumes a slot without serving anything. Instead, when
    ``graph_store`` is given, this widens the underlying fetch (4x per
    retry) until either ``limit`` servable rows are found, the store is
    exhausted (a fetch returns fewer events than it asked for), or
    :data:`_MAX_PRECEDENT_SCAN` is reached -- the same bounded-retry
    shape :func:`~trellis.stores.base.event_log.scan_events` uses for an
    unrelated reason (truncation reporting). Omitted, a caller with no
    graph store handy gets the pre-demotion, event-log-only behaviour
    with no retry, since there is nothing to filter.
    """
    payload_filters = {"domain": domain} if domain else None

    if graph_store is None:
        events = event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED,
            limit=limit,
            order="desc",
            payload_filters=payload_filters,
        )
        return [_serialize(e) for e in events]

    fetch_limit = max(limit, 1)
    results: list[dict[str, Any]] = []
    while True:
        events = event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED,
            limit=fetch_limit,
            order="desc",
            payload_filters=payload_filters,
        )
        results = []
        for e in events:
            if e.entity_id is not None and is_withheld_precedent(
                graph_store, e.entity_id
            ):
                continue
            results.append(_serialize(e))
            if len(results) >= limit:
                break
        exhausted = len(events) < fetch_limit
        if len(results) >= limit or exhausted or fetch_limit >= _MAX_PRECEDENT_SCAN:
            break
        fetch_limit = min(fetch_limit * 4, _MAX_PRECEDENT_SCAN)
    return results


def _serialize(e: Event) -> dict[str, Any]:
    return {
        "event_id": e.event_id,
        "entity_id": e.entity_id,
        "title": e.payload.get("title", ""),
        "description": e.payload.get("description", ""),
        "domain": e.payload.get("domain"),
        "occurred_at": e.occurred_at.isoformat(),
        "payload": e.payload,
    }
