"""Shared precedent listing logic."""

from __future__ import annotations

from typing import Any

from trellis.retrieve.sensitivity import readable_ids
from trellis.schemas.classification import Sensitivity, lifecycle_state_of
from trellis.stores.base.document import DocumentStore
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

#: ``Event.source`` the precedent miner stamps on everything it emits. A
#: governed ``precedent.promote`` stamps ``"mutation_executor"``, so this
#: value alone marks an ungoverned, mined row.
MINER_EVENT_SOURCE = "precedent_miner"

#: ``payload.action`` the miner stamped on an LLM-mined candidate, back when
#: it emitted that candidate as ``PRECEDENT_PROMOTED``.
MINED_CANDIDATE_ACTION = "precedent_candidate_generated"


def is_miner_candidate_event(event: Event) -> bool:
    """Whether ``event`` was written by the precedent miner, not a promotion.

    The miner now emits :attr:`EventType.PRECEDENT_CANDIDATE_MINED`, but
    event rows are immutable and earlier versions of it wrote its
    candidates as ``PRECEDENT_PROMOTED``. Every reader that means "a
    promoted precedent" skips such a row through this one predicate, so
    the two spellings of "mined, not promoted" cannot drift between
    surfaces. Matches the miner's source or its candidate action, never a
    source *other than* the promote handler's: a ``PRECEDENT_PROMOTED``
    row from any other emitter is still a promotion.
    """
    return (
        event.source == MINER_EVENT_SOURCE
        or event.payload.get("action") == MINED_CANDIDATE_ACTION
    )


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
    return _node_is_withheld(node)


def _node_is_withheld(node: dict[str, Any]) -> bool:
    return lifecycle_state_of(node.get("properties")) in WITHHELD_PRECEDENT_STATES


def _served_description(
    node: dict[str, Any] | None, payload: dict[str, Any]
) -> str | None:
    """The description to serve in place of the promoted one, if any.

    ``PRECEDENT_PROMOTED`` is immutable, so its payload still holds the
    description as promoted, excerpt included. A node ``redaction.apply``
    scrubbed serves its own text. Any other node that does not vouch for
    the excerpt (``source_excerpt_status`` other than ``included``), and a
    missing node, serve the promoted text with the excerpt cut out: a scrub
    that purged the node and failed to re-create it must not hand the
    excerpt back through this listing.

    A node vouches only for the event whose text it holds. The listing serves
    a row per promotion, and a source redacted, written again and promoted
    again leaves the node ``included`` over the *new* excerpt, so an older
    event's description is not the node's and is stripped like any other.
    ``None`` means serve it unchanged.
    """
    # Deferred: ``trellis.learning`` imports the retrieve package.
    from trellis.learning.scoring import (  # noqa: PLC0415
        SOURCE_EXCERPT_INCLUDED,
        SOURCE_EXCERPT_REDACTED,
        strip_source_excerpt,
    )

    props = (node or {}).get("properties") or {}
    status = props.get("source_excerpt_status")
    if node is not None and status == SOURCE_EXCERPT_REDACTED:
        description = props.get("description")
        return description if isinstance(description, str) else ""
    if (
        node is not None
        and status == SOURCE_EXCERPT_INCLUDED
        and props.get("description") == payload.get("description")
    ):
        return None
    promoted = payload.get("description")
    if not isinstance(promoted, str):
        return None
    stripped = strip_source_excerpt(promoted, str(payload.get("source_item_id") or ""))
    return None if stripped == promoted else stripped


def _served_title(
    node: dict[str, Any] | None, payload: dict[str, Any], entity_id: str | None
) -> str | None:
    """The title to serve in place of the promoted one, if any.

    The payload title can be the first line of the precedent's source (marked
    ``name_source="source_text"``), and the log that holds it is immutable.
    The same cases fail closed as in :func:`_served_description`:

    * a node a scrub renamed (``name_source="neutral"``) serves its own name;
    * a node scrubbed before names were marked, whose name was kept, serves a
      neutral one, unless its marker says the name held no source text;
    * a missing node, or one that does not vouch for its excerpt, serves a
      neutral name when the payload marks its title source-derived
      (``source_text``, or the source's own ``item_title``, which a redaction
      blanks too).

    A node vouches only for the event whose title it holds
    (``node.name == payload.title``). The listing serves a row per promotion
    and judges each by the *current* node, so without that test a source
    redacted, written again and promoted again would hand the older event's
    title back through a node that now reads ``included``; an event the node
    does not hold is judged by its own title as the scrub would judge it
    (:func:`~trellis.learning.scoring.name_may_hold_source_text`), so an
    unmarked one fails closed unless it is the bare-id name. A scrubbed node
    whose marker does not clear its name (none, or ``source_text``) serves the
    neutral name for every event, held or not.

    A promotion title with no marker (promoted before names were marked) is
    otherwise served as promoted, by a node that holds it or when no node
    exists: it cannot be told from an ordinary title without the pack data
    that chose it, and guessing would rename every such precedent.
    ``None`` means serve the title unchanged.
    """
    # Deferred: ``trellis.learning`` imports the retrieve package.
    from trellis.learning.scoring import (  # noqa: PLC0415
        NAME_SOURCE_NEUTRAL,
        NAME_SOURCES_FROM_SOURCE,
        NAME_SOURCES_WITHOUT_SOURCE_TEXT,
        SOURCE_EXCERPT_INCLUDED,
        SOURCE_EXCERPT_REDACTED,
        name_may_hold_source_text,
        neutral_precedent_name,
    )

    props = (node or {}).get("properties") or {}
    neutral = neutral_precedent_name(entity_id or "", props.get("intent_family"))
    if node is not None:
        marker = props.get("name_source")
        if marker == NAME_SOURCE_NEUTRAL:
            name = props.get("name")
            return name if isinstance(name, str) and name else neutral
        status = props.get("source_excerpt_status")
        holds_this_title = props.get("name") == payload.get("title")
        if status == SOURCE_EXCERPT_REDACTED:
            if marker not in NAME_SOURCES_WITHOUT_SOURCE_TEXT:
                return neutral
            if holds_this_title:
                return None
        elif status == SOURCE_EXCERPT_INCLUDED and holds_this_title:
            return None
        if not holds_this_title:
            # Judge the event's own title as the scrub would: an unmarked one
            # fails closed unless it is the bare-id name.
            event_name = {
                "name": payload.get("title"),
                "name_source": payload.get("name_source"),
                "intent_family": props.get("intent_family"),
                "source_item_id": payload.get("source_item_id")
                or props.get("source_item_id"),
            }
            return (
                neutral
                if name_may_hold_source_text(event_name, entity_id or "")
                else None
            )
    return neutral if payload.get("name_source") in NAME_SOURCES_FROM_SOURCE else None


def list_precedents(
    event_log: EventLog,
    *,
    domain: str | None = None,
    limit: int = 20,
    graph_store: GraphStore | None = None,
    document_store: DocumentStore | None = None,
    clearance: Sensitivity = "restricted",
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

    A row the precedent miner wrote
    (:func:`is_miner_candidate_event`) is skipped whatever the
    ``graph_store``: the miner once emitted its ungoverned, unapproved
    candidates as ``PRECEDENT_PROMOTED``, and a candidate is not a
    promoted precedent.

    The lifecycle and miner filters are applied **before** the ``limit``
    cap, not after: a naive "fetch ``limit``, then filter" shape can
    return fewer than ``limit`` rows even when ``limit``-or-more servable
    rows exist just past the window, because every demoted, archived or
    mined row inside the window consumes a slot without serving anything.
    Instead this widens the underlying fetch (4x per retry) until either
    ``limit`` servable rows are found, the store is exhausted (a fetch
    returns fewer events than it asked for), or :data:`_MAX_PRECEDENT_SCAN`
    is reached -- the same bounded-retry shape
    :func:`~trellis.stores.base.event_log.scan_events` uses for an
    unrelated reason (truncation reporting). Omitted, a caller with no
    graph store handy gets no lifecycle filter, since there is nothing to
    check against, but still the miner filter.

    A learning promotion's ``payload.source_item_id`` names the served item the
    precedent came from, often a document. Below ``restricted`` it is left out
    of the served ``payload`` when :func:`~trellis.retrieve.sensitivity.readable_ids`
    says ``clearance`` may not read that document, judged against
    ``document_store`` once the rows are chosen so no title or description
    changes. The default ``restricted`` serves every row as stored. A
    ``clearance`` below it with no ``document_store`` has nothing to judge
    against and leaves the key out of every row.
    """
    payload_filters = {"domain": domain} if domain else None

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
            if len(results) >= limit:
                break
            if is_miner_candidate_event(e):
                continue
            if graph_store is None:
                results.append(_serialize(e))
                continue
            node = graph_store.get_node(e.entity_id) if e.entity_id else None
            if node is not None and _node_is_withheld(node):
                continue
            results.append(
                _serialize(
                    e,
                    description=_served_description(node, e.payload),
                    title=_served_title(node, e.payload, e.entity_id),
                )
            )
        exhausted = len(events) < fetch_limit
        if len(results) >= limit or exhausted or fetch_limit >= _MAX_PRECEDENT_SCAN:
            break
        fetch_limit = min(fetch_limit * 4, _MAX_PRECEDENT_SCAN)
    return _conceal_source_pointers(results, document_store, clearance)


def _conceal_source_pointers(
    rows: list[dict[str, Any]],
    document_store: DocumentStore | None,
    clearance: Sensitivity,
) -> list[dict[str, Any]]:
    """``rows`` with each unreadable ``payload.source_item_id`` left out.

    A row's ``payload`` may be the event's own dict (:func:`_serialize` aliases
    it when neither text was overridden), so a concealed row gets a copy and the
    event is never written to. Only a string id is judged; one id is judged once
    however many rows name it.
    """
    if clearance == "restricted":
        return rows
    ids = list(
        dict.fromkeys(
            r["payload"]["source_item_id"]
            for r in rows
            if isinstance(r["payload"].get("source_item_id"), str)
        )
    )
    if not ids:
        return rows
    readable = (
        set(readable_ids(document_store, ids, clearance))
        if document_store is not None
        else set()
    )
    for row in rows:
        source = row["payload"].get("source_item_id")
        if isinstance(source, str) and source not in readable:
            row["payload"] = {
                k: v for k, v in row["payload"].items() if k != "source_item_id"
            }
    return rows


def _serialize(
    e: Event, *, description: str | None = None, title: str | None = None
) -> dict[str, Any]:
    payload = e.payload
    if description is not None:
        payload = {**payload, "description": description}
    if title is not None:
        payload = {**payload, "title": title}
    return {
        "event_id": e.event_id,
        "entity_id": e.entity_id,
        "title": payload.get("title", ""),
        "description": payload.get("description", ""),
        "domain": e.payload.get("domain"),
        "occurred_at": e.occurred_at.isoformat(),
        "payload": payload,
    }
