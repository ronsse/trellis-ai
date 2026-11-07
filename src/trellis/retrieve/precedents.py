"""Shared precedent listing logic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog

from trellis.core.precedent_document import is_promoted_precedent_document
from trellis.retrieve.sensitivity import readable_ids
from trellis.schemas.classification import lifecycle_state_of
from trellis.stores.base.document import DocumentStore
from trellis.stores.base.event_log import DEFAULT_SCAN_LIMIT, Event, EventLog, EventType
from trellis.stores.base.graph import GraphStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from trellis.schemas.classification import Sensitivity

logger = structlog.get_logger(__name__)

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

    A precedent carries no label of its own, so below ``restricted`` a row is
    **left out** (never rewritten) when either of these holds, judged with
    :func:`~trellis.retrieve.sensitivity.readable_ids` against ``document_store``:

    * a document row exists at its entity id (a promotion writes one, and the
      keyword and semantic axes withhold it above the caller's clearance) and
      ``clearance`` may not read it;
    * its source id (the event's ``source_item_id``, else the node's, the
      first that is a string) names a document ``clearance`` may not read,
      unless the row holds nothing of that source: the node's
      ``source_excerpt_status`` is ``source_labelled`` (a promotion made after
      the source carried its label copies no text and no id from it), the
      node's ``description`` equals the event's (a node vouches only for the
      event whose text it holds, and the listing serves a row per promotion, so
      an older event of a precedent promoted again stays judged by its own
      text), and the source id appears in neither the served title nor the
      served description.

    Both are decided **before** the ``limit`` cap, with one ``readable_ids``
    call per fetch window, so a withheld row never takes a slot from a
    servable one past the window. A document store that raises, or a
    judgement that does, leaves unjudged ids unreadable (an outage must not
    read as permission), so the rows that need them are withheld; a graph store
    read that raises propagates out of this function, as it did before this
    check, and nothing is served. A ``clearance`` below ``restricted`` with no
    ``document_store`` has nothing to judge against: every id reads as
    unreadable, so a row with a document row (an id in the precedent
    namespace) is withheld, and so is a row that names a source unless the
    exemption above holds for it, in which case it is served with the source id
    left out of its payload. A trace precedent, which has neither a document row
    nor a source, is served.

    A third rule fails closed on what is left of a promotion
    (:meth:`PrecedentClearance._nothing_to_judge`): a learning promotion whose
    document row the store answered with none, whose event and node name no
    source, and whose node is gone, has no label to judge it by, and is withheld
    below ``restricted`` as unreadable rather than served at ``internal``.

    A served learning promotion's ``payload.source_item_id`` is also left out of
    the ``payload`` when ``clearance`` may not read that document, once the rows
    are chosen, so no title or description changes. The default ``restricted``
    serves every row as stored and reads no store.
    """
    payload_filters = {"domain": domain} if domain else None
    gate = (
        None
        if clearance == "restricted"
        else PrecedentClearance(document_store, clearance, graph_store)
    )
    nodes: dict[str, dict[str, Any] | None] = {}

    def node_of(e: Event) -> dict[str, Any] | None:
        if graph_store is None or not e.entity_id:
            return None
        if e.entity_id not in nodes:
            nodes[e.entity_id] = graph_store.get_node(e.entity_id)
        return nodes[e.entity_id]

    fetch_limit = max(limit, 1)
    results: list[dict[str, Any]] = []
    while True:
        events = event_log.get_events(
            event_type=EventType.PRECEDENT_PROMOTED,
            limit=fetch_limit,
            order="desc",
            payload_filters=payload_filters,
        )
        barred = (
            gate.barred_events(
                [e for e in events if _is_listed(e, node_of(e))],
                node_of,
            )
            if gate is not None
            else frozenset()
        )
        results = []
        for e in events:
            if len(results) >= limit:
                break
            if is_miner_candidate_event(e):
                continue
            if graph_store is None:
                if e.event_id not in barred:
                    results.append(_serialize(e))
                continue
            node = node_of(e)
            if node is not None and _node_is_withheld(node):
                continue
            if e.event_id in barred:
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
    return _conceal_source_pointers(results, gate)


def _is_listed(event: Event, node: dict[str, Any] | None) -> bool:
    """Whether the listing considers ``event`` at all: not mined, not demoted."""
    if is_miner_candidate_event(event):
        return False
    return node is None or not _node_is_withheld(node)


def _source_id_of(payload: dict[str, Any], node: dict[str, Any] | None) -> str | None:
    """The id of the item a precedent came from: the event's, else the node's."""
    props = (node or {}).get("properties") or {}
    # Each candidate is tested for str on its own: a truthy non-string id on the
    # event must not hide the node's string id from the check.
    for source in (payload.get("source_item_id"), props.get("source_item_id")):
        if isinstance(source, str) and source:
            return source
    return None


def _holds_nothing_of_source(
    event: Event, node: dict[str, Any] | None, source: str
) -> bool:
    """Whether ``event``'s served row holds nothing of ``source``.

    True only when the node reads ``source_labelled``, the node's description is
    the event's (it vouches only for the event whose text it holds), and the
    source id is in neither the served title nor the served description.
    """
    # Deferred: ``trellis.learning`` imports the retrieve package.
    from trellis.learning.scoring import SOURCE_EXCERPT_LABELLED  # noqa: PLC0415

    props = (node or {}).get("properties") or {}
    if props.get("source_excerpt_status") != SOURCE_EXCERPT_LABELLED:
        return False
    description = event.payload.get("description")
    if not isinstance(description, str) or props.get("description") != description:
        return False
    title = _served_title(node, event.payload, event.entity_id)
    if title is None:
        title = event.payload.get("title")
    served = _served_description(node, event.payload)
    text = f"{title or ''}\n{description if served is None else served}"
    return source not in text


#: A precedent withheld because its source (or its own row) is above the
#: caller's clearance: the label said no.
PRECEDENT_WITHHELD_LABELLED = "labelled"

#: A precedent withheld because the lookup that would have judged it failed (a
#: store that raised). Fails closed, and is counted apart from a label that said
#: no so an outage does not read as a labelled corpus.
PRECEDENT_WITHHELD_UNREADABLE = "unreadable"


class _FaultRecordingStore:
    """A document store that remembers which ids it failed to read.

    ``readable_ids`` fails closed on a store error and logs it, which leaves "the
    label said no" and "the store could not answer" looking alike in its result.
    This wraps the store handed to it and notes each id whose ``get`` raised, so
    the caller can tell the two apart without changing ``readable_ids``. It notes
    each id the store answered with no row as well (``absent``): ``readable_ids``
    reads an id with no row as readable, as it must for a graph node or a trace,
    and a caller that needs to know the row was not there asks the recorder.
    """

    def __init__(self, inner: DocumentStore) -> None:
        self._inner = inner
        self.faulted: set[str] = set()
        self.absent: set[str] = set()

    def get(self, doc_id: str) -> dict[str, Any] | None:
        try:
            row = self._inner.get(doc_id)
        except Exception:
            self.faulted.add(doc_id)
            raise
        if row is None:
            self.absent.add(doc_id)
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _node_holds_nothing_of_source(node: dict[str, Any], source: str) -> bool:
    """Whether ``node``'s own text holds nothing of ``source``.

    :func:`_holds_nothing_of_source` for a caller with the node and no event
    (a by-id read, a search hit, a pack item): the node reads ``source_labelled``
    and the source id is in neither its name nor its description. The node's
    text is what those surfaces serve, so there is no older event to compare.
    """
    # Deferred: ``trellis.learning`` imports the retrieve package.
    from trellis.learning.scoring import SOURCE_EXCERPT_LABELLED  # noqa: PLC0415

    props = node.get("properties") or {}
    if props.get("source_excerpt_status") != SOURCE_EXCERPT_LABELLED:
        return False
    name = props.get("name")
    description = props.get("description")
    text = "\n".join(v for v in (name, description) if isinstance(v, str))
    return source not in text


class PrecedentClearance:
    """What a caller at ``clearance`` may be served of a promoted precedent.

    The one predicate every route asks (#1080), so the listing, the by-id reads,
    search and the pack cannot disagree about a precedent. A precedent carries no
    label, so it is judged by two others, resolved when it is served: its own
    document row (stored at its entity id) and the document named by its
    ``source_item_id``, unless it holds nothing of that source
    (:func:`_holds_nothing_of_source`). A caller who cannot read either does not
    get the precedent; its text is never rewritten.

    One instance per request. Each id is judged once, in a batch per fetch window
    for the listing (:meth:`barred_events`) or per precedent for a node or id
    (:meth:`verdict_of`), and the verdict is kept for the pointer concealment that
    follows (:func:`_conceal_source_pointers`).

    A lookup that fails withholds (:data:`PRECEDENT_WITHHELD_UNREADABLE`) and is
    counted apart from a label that said no (:attr:`counts`). So does a listed
    promotion with nothing left to judge it by (:meth:`_nothing_to_judge`).
    """

    def __init__(
        self,
        document_store: DocumentStore | None,
        clearance: Sensitivity,
        graph_store: GraphStore | None = None,
    ) -> None:
        self._store = document_store
        self._clearance = clearance
        self._graph = graph_store
        self._verdicts: dict[str, bool] = {}
        self._faulted: set[str] = set()
        self._absent: set[str] = set()
        self._served: dict[str, str | None] = {}
        self._reasons: dict[str, str] = {}

    @property
    def counts(self) -> dict[str, int]:
        """How many distinct precedents were withheld, by why."""
        out = {PRECEDENT_WITHHELD_LABELLED: 0, PRECEDENT_WITHHELD_UNREADABLE: 0}
        for reason in self._reasons.values():
            out[reason] += 1
        return out

    def judge(self, ids: Iterable[str]) -> None:
        """Record whether the caller may read each id not yet judged, in one call."""
        fresh = [i for i in dict.fromkeys(ids) if i not in self._verdicts]
        if not fresh:
            return
        readable: set[str] = set()
        if self._store is None:
            self._faulted.update(fresh)
        else:
            recording = _FaultRecordingStore(self._store)
            try:
                # The recorder forwards every call, so it stands in for the store.
                readable = set(
                    readable_ids(
                        cast("DocumentStore", recording), fresh, self._clearance
                    )
                )
            except Exception as exc:  # advisory step; fails closed
                # GRACEFUL-DEGRADATION: an outage must not read as permission, so
                # an id the judgement could not answer for is unreadable.
                logger.warning(
                    "precedent_listing_clearance_failed",
                    error_type=type(exc).__name__,
                )
                self._faulted.update(fresh)
            self._faulted.update(i for i in fresh if self._read_faulted(i, recording))
            self._absent.update(
                i for i in fresh if i in recording.absent and i not in self._faulted
            )
        for item_id in fresh:
            self._verdicts[item_id] = item_id in readable
        failed = [i for i in fresh if i not in readable and i in self._faulted]
        if failed:
            logger.warning("precedent_source_lookup_failed", count=len(failed))

    @staticmethod
    def _read_faulted(item_id: str, recording: _FaultRecordingStore) -> bool:
        """Whether reading ``item_id`` (or, for a chunk, its parent) raised."""
        from trellis.ingest_corpus.models import chunk_parent_id  # noqa: PLC0415

        parent = chunk_parent_id(item_id)
        return item_id in recording.faulted or (
            parent is not None and parent in recording.faulted
        )

    def may_read(self, item_id: str) -> bool:
        """The recorded verdict; an id never judged is unreadable."""
        return self._verdicts.get(item_id, False)

    def _why_unreadable(self, item_id: str) -> str:
        if item_id in self._faulted:
            return PRECEDENT_WITHHELD_UNREADABLE
        return PRECEDENT_WITHHELD_LABELLED

    def _bar_reason(
        self, entity_id: str | None, source: str | None, *, exempt: bool
    ) -> str | None:
        """Why a precedent may not be served, or ``None`` when it may."""
        own = self._own_row_reason(entity_id)
        if own is not None:
            return own
        if source is not None and not exempt and not self.may_read(source):
            return self._why_unreadable(source)
        return None

    def _own_row_reason(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        if self._store is None:
            # No store to ask whether a row exists: a learning promotion has one
            # by construction, a trace precedent has none.
            return (
                PRECEDENT_WITHHELD_UNREADABLE
                if is_promoted_precedent_document(entity_id)
                else None
            )
        if self.may_read(entity_id):
            return None
        return self._why_unreadable(entity_id)

    def _nothing_to_judge(
        self, entity_id: str | None, node: dict[str, Any] | None, source: str | None
    ) -> bool:
        """Whether a listed promotion has no label left to judge it by.

        The event log is immutable, so a promotion's event outlives what the two
        rules above read: its node (a redaction purges it, and a failed re-create
        leaves none), its document row, and the source id its payload may never
        have carried. With all three gone the rules find no label, and an id with
        no row reads as readable, so the row was served at ``internal`` for want
        of anything to say no. Every other read fails closed on what it cannot
        judge, so this one does too: a promotion in the learning namespace, whose
        row the store answered with none, naming no source and holding no node, is
        withheld as :data:`PRECEDENT_WITHHELD_UNREADABLE` below ``restricted``.

        Only the learning namespace: a trace precedent has no row and no source by
        construction and is served. Only a row the store *answered* absent: one it
        failed to read is already unreadable. Only the listings ask it, because a
        by-id read, a search hit or a pack item holds a node or a row to judge.
        ``restricted`` reads nothing, so nothing is withheld.
        """
        return (
            self._clearance != "restricted"
            and source is None
            and node is None
            and entity_id is not None
            and is_promoted_precedent_document(entity_id)
            and entity_id in self._absent
        )

    def barred_events(
        self,
        events: list[Event],
        node_of: Callable[[Event], dict[str, Any] | None],
    ) -> frozenset[str]:
        """The ``event_id`` of each of ``events`` the caller may not be served.

        Judges every id the window needs (each entity id, each source id) in one
        :meth:`judge` call, then applies the two rules in
        :func:`list_precedents` and the third, :meth:`_nothing_to_judge`.
        """
        planned = []
        wanted: list[str] = []
        for e in events:
            node = node_of(e)
            source = _source_id_of(e.payload, node)
            exempt = source is not None and _holds_nothing_of_source(e, node, source)
            planned.append((e, node, source, exempt))
            if e.entity_id:
                wanted.append(e.entity_id)
            if source is not None:
                wanted.append(source)
        self.judge(wanted)
        barred: set[str] = set()
        for e, node, source, exempt in planned:
            reason = self._bar_reason(e.entity_id, source, exempt=exempt)
            if reason is None and self._nothing_to_judge(e.entity_id, node, source):
                reason = PRECEDENT_WITHHELD_UNREADABLE
            if reason is not None:
                barred.add(e.event_id)
                self._reasons[e.event_id] = reason
        return frozenset(barred)

    def verdict_of(
        self, entity_id: str, node: dict[str, Any] | None = None
    ) -> str | None:
        """Why ``entity_id`` may not be served to this caller, or ``None``.

        For a route that holds a graph node (``node``) or only an id: a by-id
        read, a search hit, a pack item. :data:`PRECEDENT_WITHHELD_LABELLED` when
        its own row or its source is above the clearance, and
        :data:`PRECEDENT_WITHHELD_UNREADABLE` when the lookup that would have
        said failed. An id that is not a promoted precedent (not in the reserved
        namespace, the one place a promotion stores text taken from its source)
        is ``None`` without a read; a node outside it keeps the pointer
        concealment of :func:`~trellis.retrieve.sensitivity.conceal_node_pointers`.
        ``restricted`` reads nothing.
        """
        if self._clearance == "restricted":
            return None
        if entity_id not in self._served:
            reason = self._verdict(entity_id, node)
            self._served[entity_id] = reason
            if reason is not None:
                self._reasons[entity_id] = reason
        return self._served[entity_id]

    def _verdict(self, entity_id: str, node: dict[str, Any] | None) -> str | None:
        if not is_promoted_precedent_document(entity_id):
            return None
        if node is None and self._graph is not None:
            try:
                node = self._graph.get_node(entity_id)
            except Exception as exc:  # advisory step; fails closed
                # GRACEFUL-DEGRADATION: with no node the source cannot be
                # named, and an outage must not read as permission.
                logger.warning(
                    "precedent_source_lookup_failed",
                    count=1,
                    error_type=type(exc).__name__,
                )
                return PRECEDENT_WITHHELD_UNREADABLE
        source = _source_id_of({}, node)
        exempt = (
            source is not None
            and node is not None
            and _node_holds_nothing_of_source(node, source)
        )
        self.judge([entity_id, *([source] if source else [])])
        return self._bar_reason(entity_id, source, exempt=exempt)


def _conceal_source_pointers(
    rows: list[dict[str, Any]], gate: PrecedentClearance | None
) -> list[dict[str, Any]]:
    """``rows`` with each unreadable ``payload.source_item_id`` left out.

    A row's ``payload`` may be the event's own dict (:func:`_serialize` aliases
    it when neither text was overridden), so a concealed row gets a copy and the
    event is never written to. Only a string id is judged; one id is judged once
    however many rows name it. ``gate`` is ``None`` at ``restricted``, which
    conceals nothing.
    """
    if gate is None:
        return rows
    gate.judge(
        r["payload"]["source_item_id"]
        for r in rows
        if isinstance(r["payload"].get("source_item_id"), str)
    )
    for row in rows:
        source = row["payload"].get("source_item_id")
        if isinstance(source, str) and not gate.may_read(source):
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
