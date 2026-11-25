"""Memory yield: which way of creating a memory ever earns a helpful serving.

The system can say how much it writes and how much it serves, but not which
*kind* of write is ever cited. This report is that number. For every document
created in the window it asks how the document came to exist (its **creation
path**), whether any pack served it, and whether any grader named it helpful or
unhelpful. It is read-only and decides nothing.

**Universe.** Rows of the document store whose ``created_at`` falls in the
window, parents only. A chunk (``<parent>#chunk-N``, read by
:func:`~trellis.ingest_corpus.models.chunk_parent_id`) is credited to its
parent, so a long document served chunk by chunk counts once as a created item
and once per ``(pack_id, item_id)`` serving. Documents created before the
window are out of scope; graph nodes and advisories are not document rows
and are not in the universe (their servings are counted under
``limits.unattributed_servings``). A trace's ``trace-summary:<id>`` document is
a document row like any other: the trace-embed worker writes it, so it is
counted under that worker's ``requested_by`` (``worker:embed-traces``), or under
``source_system:trellis-trace`` when its creation event fell outside the scan.

**Traces.** Given a trace store, the report adds a separate block,
:class:`TraceYield`: traces whose ``created_at`` falls in the window, how many
were ever served in a form whose id names the trace, and how they were graded.
Two forms carry the link in the id itself, so crediting them is a fact in the
data, not an inference:

* ``trace-summary:<id>`` (:data:`TRACE_FORM_SUMMARY`), the document and vector
  row the trace-embed worker renders from one trace, served by the keyword
  (``item_type`` ``document``) and semantic (``vector``) axes;
* ``trace:<id>`` (:data:`TRACE_FORM_ACTIVITY`), the Activity node trace
  extraction mints for one trace, served by the graph axis as ``entity``.

No retrieval strategy reads the trace store, so a raw trace id is never
served. Precedents and the entity nodes extraction draws out of a trace are not
credited: a precedent distils several traces, and an entity node is mentioned
by many, so spreading their servings over traces would invent a link. They stay
where they were (document rows or ``unattributed_servings``).

The block is a second view, not a second universe: it is not added to
``totals``. A summary document written in the window is still owned by its
document row, and the block counts those servings again under
``servings_counted_as_documents`` so the two are never summed. Only servings no
document row owned leave ``limits.unattributed_servings``, which therefore
falls by exactly ``totals.servings - servings_counted_as_documents`` of the
block. A trace recorded before the window is out of scope, even when its
summary, written later by a backlog pass, is a document created inside it.

**Creation path.** Read by :func:`classify_creation_path` from the earliest
creation evidence the event log holds for the document id, in this order:

1. ``requested_by`` of a ``MEMORY_STORED`` event (``save_memory``, corpus and
   conversation ingest), falling back to that event's ``source``;
2. ``requested_by`` of a successful ``MUTATION_EXECUTED`` whose ``operation``
   creates an item (:data:`CREATION_OPERATIONS`) and whose ``entity_id`` is the
   document id (``evidence.ingest`` from every surface that builds its command
   with :func:`~trellis.mutate.evidence_ingest.build_evidence_ingest_command`,
   ``precedent.promote``);
3. failing both, the document's own ``metadata["source_system"]``, labelled
   ``source_system:<value>`` so it is never mistaken for a write surface.

A document with none of the three lands in :data:`UNKNOWN_PATH`, counted,
never dropped and never assigned to a neighbour.

**Servings and grades.** ``PACK_ASSEMBLED.injected_items[]`` rows, flat and
sectioned alike, counted per ``(pack_id, item_id)``; held-out packs and the
feedback naming them are dropped with
:func:`~trellis.core.pack_holdout.drop_holdout` and the count is reported. A
document is graded helpful (unhelpful) when some pack that served it named the
served id in ``helpful_item_ids`` (``unhelpful_item_ids``); named both ways it
counts in both and in ``mixed``. Feedback with no item ids grades nothing, so
``served_ungraded`` is not a verdict of unhelpfulness.

**Capture breakdown.** Opt-in (``capture_breakdown=True``), a second view of
the :data:`SESSION_CAPTURE_PATH` row: the same documents split by the kind of
session that produced them (:func:`session_kind`, read from
``metadata.subagent``, else the ``agent-`` prefix of ``metadata.session_id``)
and by whether the near-duplicates embedding predicate
(:func:`~trellis.analyze.near_duplicates.embedding_close_pairs`) pairs them with
another capture document in the window, plus the 2-way cross. Off, the report
reads nothing more and its field is ``None``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import Field

from trellis.analyze.near_duplicates import (
    DEFAULT_NEIGHBORS,
    connected_components,
    embedding_close_pairs,
)
from trellis.core.base import TrellisModel
from trellis.core.pack_holdout import drop_holdout, is_holdout
from trellis.core.reserved_doc_ids import TRACE_SUMMARY_DOC_ID_PREFIX
from trellis.feedback.attribution import is_sectioned_payload
from trellis.feedback.pack_rows import (
    has_served_rows,
    iter_feedback_rows,
    served_rows_from_payload,
)
from trellis.ingest_corpus.models import chunk_parent_id
from trellis.stores.base.event_log import (
    DEFAULT_SCAN_LIMIT,
    EventType,
    ScanCoverage,
    merge_coverage,
    resolve_scan_window,
    scan_events,
)

if TYPE_CHECKING:
    from trellis.stores.base.document import DocumentStore
    from trellis.stores.base.event_log import Event, EventLog
    from trellis.stores.base.trace import TraceStore
    from trellis.stores.base.vector import VectorStore

logger = structlog.get_logger(__name__)

#: The path a document with no creation evidence is counted under.
UNKNOWN_PATH = "unknown"

#: ``MUTATION_EXECUTED.payload.operation`` values that bring an item into
#: existence. Metadata-only and lifecycle writes are deliberately absent: they
#: touch an item some other path created.
CREATION_OPERATIONS: frozenset[str] = frozenset(
    {
        "evidence.ingest",
        "precedent.promote",
        "precedent.candidate",
        "entity.create",
        "trace.ingest",
    }
)

#: Where a path label was read from, reported per path.
BASIS_EVENT = "event:requested_by"
BASIS_SOURCE_SYSTEM = "document:metadata.source_system"
BASIS_NONE = "none"

#: The two forms a served item id names its trace in (see the module
#: docstring), as reported in :attr:`TraceYield.servings_by_form`.
TRACE_FORM_SUMMARY = "trace_summary"
TRACE_FORM_ACTIVITY = "graph_activity"

#: Id prefix of the Activity node trace extraction mints for one trace
#: (``trellis.extract.trace``).
TRACE_ACTIVITY_ID_PREFIX = "trace:"

#: ``outcome.status`` values the trace block breaks out; every other status,
#: and a trace with no outcome, is :data:`OTHER_OUTCOME`.
TRACE_OUTCOMES: tuple[str, ...] = ("success", "failure")
OTHER_OUTCOME = "other"

_PAGE = 500
_SUCCESS = "success"

#: The creation path the capture breakdown covers (the session-capture
#: worker's ``requested_by``).
SESSION_CAPTURE_PATH = "worker:session-capture"

#: Session kinds of the capture breakdown.
KIND_SUBAGENT = "subagent"
KIND_TOP_LEVEL = "top_level"
KIND_UNKNOWN = "unknown"
SESSION_KINDS: tuple[str, ...] = (KIND_SUBAGENT, KIND_TOP_LEVEL, KIND_UNKNOWN)

#: Where a session kind was read from.
KIND_BASIS_SUBAGENT_FLAG = "metadata.subagent"
KIND_BASIS_SESSION_ID = "metadata.session_id"
KIND_BASIS_NONE = "none"

#: Claude Code names a sub-agent transcript ``agent-*.jsonl``, and the capture
#: worker records the transcript stem as ``metadata.session_id``.
SUBAGENT_SESSION_PREFIX = "agent-"

#: Duplication classes of the capture breakdown.
DUP_NEAR_DUPLICATE = "near_duplicate"
DUP_SINGLETON = "singleton"
DUP_UNCHECKED = "unchecked"
DUPLICATION_CLASSES: tuple[str, ...] = (
    DUP_NEAR_DUPLICATE,
    DUP_SINGLETON,
    DUP_UNCHECKED,
)

#: Defaults of the duplication predicate's bound and floor.
DEFAULT_CAPTURE_THRESHOLD = 0.90
DEFAULT_CAPTURE_MAX_DOCS = 5000

_CAPTURE_LIMIT_STATEMENTS: tuple[str, ...] = (
    (
        "The breakdown covers documents whose creation path is "
        "worker:session-capture; a capture whose creation event fell outside the "
        "scan is counted under its source_system path instead and is not here."
    ),
    (
        "Session kind is metadata.subagent when the capture worker recorded it, "
        "else whether metadata.session_id begins with 'agent-' (the sub-agent "
        "transcript name), else unknown. Document text is never read."
    ),
    (
        "Duplication is the near-duplicates embedding predicate, restricted to "
        "session-capture documents in the window: a document is a near_duplicate "
        "when some pair with another one reaches the cosine threshold. "
        "near_duplicate is a lower bound and some singletons may have a missed "
        "partner, for two reasons: each probe is a bounded top-k query, and a "
        "partner beyond max_docs (an older capture document) is never a "
        "candidate."
    ),
    (
        "unchecked means the document's partners are unknown: no vector store, "
        "no usable embedding, its own neighbour query raised "
        "(neighbor_queries_failed), or beyond max_docs (the newest max_docs are "
        "probed). A document whose query raised is still a near_duplicate when "
        "another document's probe found it."
    ),
    (
        "Unlike trellis analyze near-duplicates, the population includes "
        "archived, superseded and noise documents."
    ),
)

_LIMIT_STATEMENTS: tuple[str, ...] = (
    (
        "Only document-store rows created in the window are counted; documents "
        "created before the window are out of scope, and graph nodes, traces "
        "not rendered to documents and advisories are not document rows."
    ),
    (
        "An item created in the window counts every serving the pack scan "
        "holds, up to now, so a recent window has had less time to be served."
    ),
    (
        "served_ungraded means no feedback named the item either way. It is "
        "not unhelpful: feedback without item ids (the CLI and POST /feedback "
        "family) grades nothing, and feedback_without_item_ids counts it."
    ),
    (
        "A citation counts only when it names the exact id its pack served; a "
        "grader citing a parent for a served chunk, or the reverse, is a stray."
    ),
    (
        "Creation path is the earliest creation event's requested_by; a "
        "document whose creation event fell outside the scan, or whose writer "
        "emits none, falls back to metadata.source_system and then to "
        "'unknown'."
    ),
)


class PathYield(TrellisModel):
    """Created, served and graded counts for one creation path."""

    path: str
    basis: str
    created: int = 0
    served: int = 0
    servings: int = 0
    graded_helpful: int = 0
    helpful_servings: int = 0
    graded_unhelpful: int = 0
    unhelpful_servings: int = 0
    mixed: int = 0
    served_ungraded: int = 0
    never_served: int = 0
    tokens_served: float = 0
    tokens_helpful: float = 0
    tokens_unhelpful: float = 0
    servings_without_token_estimate: int = 0


class YieldLimits(TrellisModel):
    """What the report cannot see, stated beside what it counted."""

    holdout_packs_dropped: int = 0
    holdout_feedback_dropped: int = 0
    packs: int = 0
    sectioned_packs: int = 0
    packs_without_rows: int = 0
    feedback_events: int = 0
    feedback_without_item_ids: int = 0
    stray_citations: int = 0
    unattributed_servings: int = 0
    unattributed_servings_by_item_type: dict[str, int] = Field(default_factory=dict)
    documents_scanned: int = 0
    documents_without_created_at: int = 0
    statements: list[str] = Field(default_factory=lambda: list(_LIMIT_STATEMENTS))


class TraceYieldRow(TrellisModel):
    """Recorded, served and graded counts for a set of traces."""

    outcome: str
    recorded: int = 0
    served: int = 0
    servings: int = 0
    graded_helpful: int = 0
    helpful_servings: int = 0
    graded_unhelpful: int = 0
    unhelpful_servings: int = 0
    mixed: int = 0
    served_ungraded: int = 0
    never_served: int = 0


class TraceYield(TrellisModel):
    """Traces recorded in the window, credited through the forms naming them."""

    totals: TraceYieldRow = Field(
        default_factory=lambda: TraceYieldRow(outcome="(all)")
    )
    by_outcome: list[TraceYieldRow] = Field(default_factory=list)
    servings_by_form: dict[str, int] = Field(default_factory=dict)
    #: Servings in ``totals`` a document row also owns (an in-window summary
    #: document); never add the block to the document totals.
    servings_counted_as_documents: int = 0
    scan_limit: int = 0
    traces_scanned: int = 0
    #: True when the trace store held more than ``scan_limit`` traces in the
    #: window; the newest ``scan_limit`` were counted.
    truncated: bool = False


class CaptureYieldRow(TrellisModel):
    """The per-path counts for one slice of the session-capture documents."""

    group: str
    created: int = 0
    served: int = 0
    servings: int = 0
    graded_helpful: int = 0
    helpful_servings: int = 0
    graded_unhelpful: int = 0
    unhelpful_servings: int = 0
    mixed: int = 0
    served_ungraded: int = 0
    never_served: int = 0
    tokens_served: float = 0
    tokens_helpful: float = 0
    tokens_unhelpful: float = 0
    servings_without_token_estimate: int = 0


class CaptureCrossRow(TrellisModel):
    """One cell of the session-kind by duplication cross."""

    session_kind: str
    duplication: str
    created: int = 0
    served: int = 0
    servings: int = 0
    helpful_servings: int = 0
    unhelpful_servings: int = 0
    graded_helpful: int = 0
    graded_unhelpful: int = 0


class CaptureDuplication(TrellisModel):
    """How duplication was decided, and the clusters it found."""

    predicate: str = (
        "trellis.analyze.near_duplicates.embedding_close_pairs: stored-embedding "
        "cosine at or above threshold, one bounded top-k probe per document"
    )
    threshold: float
    neighbors: int
    max_docs: int
    #: Session-capture documents created in the window.
    population: int = 0
    #: Of those, the newest ``max_docs`` handed to the predicate.
    probed: int = 0
    #: Documents left out by ``max_docs`` (counted as ``unchecked``). Zero
    #: without a vector store, where nothing is probed at all.
    over_bound: int = 0
    no_vector_store: bool = False
    embedded: int = 0
    no_embedding: int = 0
    embedding_unusable: int = 0
    #: Probe queries that raised; those documents are ``unchecked`` unless
    #: another document's probe paired them.
    neighbor_queries_failed: int = 0
    dimension_mismatches: int = 0
    probes_saturated: int = 0
    pairs: int = 0
    pairs_same_session: int = 0
    pairs_cross_session: int = 0
    pairs_unknown_session: int = 0
    clusters: int = 0
    largest_cluster: int = 0
    #: Clusters whose members all name one source session.
    clusters_same_session: int = 0
    #: Clusters whose members name two or more source sessions.
    clusters_cross_session: int = 0
    #: Clusters with a member whose source session is not recorded, and whose
    #: recorded members name at most one session.
    clusters_unknown_session: int = 0
    members_in_same_session_clusters: int = 0
    members_in_cross_session_clusters: int = 0
    members_in_unknown_session_clusters: int = 0


class CaptureBreakdown(TrellisModel):
    """Session-capture documents split by session kind and by duplication.

    A second view of the :data:`SESSION_CAPTURE_PATH` row: ``totals`` equals
    that row's counts, and each split partitions it.
    """

    path: str = ""
    totals: CaptureYieldRow = Field(
        default_factory=lambda: CaptureYieldRow(group="(all)")
    )
    by_session_kind: list[CaptureYieldRow] = Field(default_factory=list)
    #: Where each document's session kind was read from.
    session_kind_basis: dict[str, int] = Field(default_factory=dict)
    by_duplication: list[CaptureYieldRow] = Field(default_factory=list)
    cross: list[CaptureCrossRow] = Field(default_factory=list)
    duplication: CaptureDuplication
    limits: list[str] = Field(default_factory=list)


class MemoryYieldReport(TrellisModel):
    """Per-creation-path yield over a window."""

    window_days: int
    since: str
    paths: list[PathYield] = Field(default_factory=list)
    totals: PathYield = Field(
        default_factory=lambda: PathYield(path="(all)", basis="(all)")
    )
    limits: YieldLimits = Field(default_factory=YieldLimits)
    scan: ScanCoverage = Field(default_factory=ScanCoverage)
    traces: TraceYield | None = None
    #: Present only when asked for (``capture_breakdown=True``); a caller that
    #: serialises the report omits the key when it is ``None``.
    capture_breakdown: CaptureBreakdown | None = None

    def dump(self) -> dict[str, Any]:
        """``model_dump`` without the ``capture_breakdown`` key when unset."""
        exclude = {"capture_breakdown"} if self.capture_breakdown is None else None
        return self.model_dump(exclude=exclude)


def classify_creation_path(
    surface: str | None, metadata: Mapping[str, Any] | None
) -> tuple[str, str]:
    """Name the creation path of one document, and the field it was read from.

    Args:
        surface: ``requested_by`` (or ``MEMORY_STORED.source``) of the earliest
            creation event for the document, or ``None`` when there is none.
        metadata: The document row's metadata.

    Returns:
        ``(path, basis)``. ``path`` is the surface verbatim,
        ``source_system:<value>``, or :data:`UNKNOWN_PATH`; ``basis`` is one of
        :data:`BASIS_EVENT`, :data:`BASIS_SOURCE_SYSTEM`, :data:`BASIS_NONE`.
    """
    if isinstance(surface, str) and surface.strip():
        return surface.strip(), BASIS_EVENT
    source_system = (metadata or {}).get("source_system")
    if isinstance(source_system, str) and source_system.strip():
        return f"source_system:{source_system.strip()}", BASIS_SOURCE_SYSTEM
    return UNKNOWN_PATH, BASIS_NONE


@dataclass
class _Doc:
    path: str
    basis: str
    created: datetime | None = None
    session_id: str | None = None
    subagent: bool | None = None
    servings: int = 0
    helpful: int = 0
    unhelpful: int = 0
    tokens: float = 0
    tokens_helpful: float = 0
    tokens_unhelpful: float = 0
    untokened: int = 0


@dataclass
class _TraceTally:
    outcome: str
    servings: int = 0
    helpful: int = 0
    unhelpful: int = 0


@dataclass
class _Verdicts:
    helpful: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    unhelpful: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    without_ids: int = 0


def summarize_memory_yield(
    document_store: DocumentStore,
    event_log: EventLog,
    *,
    trace_store: TraceStore | None = None,
    days: int = 30,
    limit: int = DEFAULT_SCAN_LIMIT,
    now: datetime | None = None,
    capture_breakdown: bool = False,
    vector_store: VectorStore | None = None,
    capture_threshold: float = DEFAULT_CAPTURE_THRESHOLD,
    capture_max_docs: int = DEFAULT_CAPTURE_MAX_DOCS,
    capture_neighbors: int = DEFAULT_NEIGHBORS,
) -> MemoryYieldReport:
    """Count, per creation path, what was created, served and graded.

    Args:
        document_store: Source of the universe (rows created in the window).
        event_log: Holds the creation, pack and feedback events.
        trace_store: Source of the trace block; ``None`` omits the block.
        days: Look-back window for creation and for the scans.
        limit: Per-event-type scan cap; truncation is reported in ``scan``.
            Also caps the trace read, reported in ``traces.truncated``.
        now: Window end, for tests. Default: the current time.
        capture_breakdown: Add :class:`CaptureBreakdown` for the
            :data:`SESSION_CAPTURE_PATH` documents. Off, nothing else is read.
        vector_store: Read (``get`` and ``query`` only) for the breakdown's
            duplication split; ``None`` leaves every document ``unchecked``.
        capture_threshold: Cosine floor of the duplication predicate.
        capture_max_docs: Most capture documents probed, newest first.
        capture_neighbors: ``top_k`` of each probe query.

    Returns:
        A :class:`MemoryYieldReport`. Reads only.
    """
    moment = now if now is not None else datetime.now(tz=UTC)
    since, until = resolve_scan_window(days, now=moment)

    def _scan(event_type: EventType) -> Any:
        return scan_events(
            event_log, event_type=event_type, since=since, until=until, limit=limit
        )

    stored = _scan(EventType.MEMORY_STORED)
    executed = _scan(EventType.MUTATION_EXECUTED)
    pack_scan = _scan(EventType.PACK_ASSEMBLED)
    feedback_scan = _scan(EventType.FEEDBACK_RECORDED)
    scan = merge_coverage(
        stored.coverage, executed.coverage, pack_scan.coverage, feedback_scan.coverage
    )

    limits = YieldLimits()
    surfaces = _creation_surfaces(stored.events, executed.events)
    docs = _created_documents(document_store, since, surfaces, limits)
    traces = (
        _recorded_traces(trace_store, since, until, limit)
        if trace_store is not None
        else None
    )

    held = sum(1 for event in pack_scan.events if is_holdout(event.payload))
    packs, feedback = drop_holdout(pack_scan.events, feedback_scan.events)
    limits.holdout_packs_dropped = held
    limits.holdout_feedback_dropped = len(feedback_scan.events) - len(feedback)
    limits.feedback_events = len(feedback)

    served_by_pack = _served_ids(packs, limits)
    verdicts = _collect_verdicts(feedback)
    limits.feedback_without_item_ids = verdicts.without_ids
    limits.stray_citations = sum(
        len(
            (verdicts.helpful.get(pid, set()) | verdicts.unhelpful.get(pid, set()))
            - served_by_pack.get(pid, {}).keys()
        )
        for pid in set(verdicts.helpful) | set(verdicts.unhelpful)
    )

    unattributed: Counter[str] = Counter()
    for pack_id, rows in served_by_pack.items():
        for item_id, (item_type, tokens) in rows.items():
            doc = docs.get(item_id)
            if doc is None:
                parent = chunk_parent_id(item_id)
                doc = docs.get(parent) if parent is not None else None
            is_helpful = item_id in verdicts.helpful.get(pack_id, ())
            is_unhelpful = item_id in verdicts.unhelpful.get(pack_id, ())
            credited = traces is not None and traces.credit(
                item_id,
                helpful=is_helpful,
                unhelpful=is_unhelpful,
                owned_by_document=doc is not None,
            )
            if doc is None:
                if not credited:
                    unattributed[item_type or "(none)"] += 1
                continue
            _add_serving(doc, tokens, helpful=is_helpful, unhelpful=is_unhelpful)
    limits.unattributed_servings = sum(unattributed.values())
    limits.unattributed_servings_by_item_type = dict(sorted(unattributed.items()))

    paths, totals = _rollup(docs.values())
    report = MemoryYieldReport(
        window_days=days,
        since=since.isoformat(),
        paths=paths,
        totals=totals,
        limits=limits,
        scan=scan,
        traces=traces.rollup() if traces is not None else None,
    )
    if capture_breakdown:
        report.capture_breakdown = _capture_breakdown(
            {
                doc_id: doc
                for doc_id, doc in docs.items()
                if doc.path == SESSION_CAPTURE_PATH
            },
            vector_store,
            threshold=capture_threshold,
            max_docs=capture_max_docs,
            neighbors=capture_neighbors,
        )
    logger.debug(
        "memory_yield_summarized",
        window_days=days,
        created=totals.created,
        served=totals.served,
        paths=len(paths),
    )
    return report


def _creation_surfaces(
    stored: Iterable[Event], executed: Iterable[Event]
) -> dict[str, str | None]:
    """Earliest creation surface per entity id, across both event types."""
    found: dict[str, tuple[datetime, str | None]] = {}

    def _offer(entity_id: str | None, at: datetime, surface: str | None) -> None:
        if not entity_id:
            return
        prior = found.get(entity_id)
        if prior is None or at < prior[0]:
            found[entity_id] = (at, surface)

    for event in stored:
        payload = event.payload or {}
        requested_by = payload.get("requested_by")
        surface: str | None = (
            requested_by if isinstance(requested_by, str) else event.source
        )
        _offer(event.entity_id, _as_utc(event.occurred_at), surface)
    for event in executed:
        payload = event.payload or {}
        if payload.get("status") != _SUCCESS:
            continue
        if payload.get("operation") not in CREATION_OPERATIONS:
            continue
        requested_by = payload.get("requested_by")
        surface = requested_by if isinstance(requested_by, str) else None
        _offer(event.entity_id, _as_utc(event.occurred_at), surface)
    return {entity_id: surface for entity_id, (_, surface) in found.items()}


def _created_documents(
    document_store: DocumentStore,
    since: datetime,
    surfaces: Mapping[str, str | None],
    limits: YieldLimits,
) -> dict[str, _Doc]:
    """Every parent row created at or after ``since``, classified.

    Pages the whole store rather than trusting a backend's sort order to stop
    early: the window is a filter over ``created_at``, not a property of the
    listing.
    """
    docs: dict[str, _Doc] = {}
    offset = 0
    while True:
        page = document_store.list_documents(
            limit=_PAGE, offset=offset, include_chunks=False
        )
        for row in page:
            limits.documents_scanned += 1
            created = _parse_time(row.get("created_at"))
            if created is None:
                limits.documents_without_created_at += 1
                continue
            doc_id = row.get("doc_id")
            if not isinstance(doc_id, str) or created < since:
                continue
            path, basis = classify_creation_path(
                surfaces.get(doc_id), row.get("metadata")
            )
            metadata = row.get("metadata")
            meta = metadata if isinstance(metadata, Mapping) else {}
            session_id = meta.get("session_id")
            subagent = meta.get("subagent")
            docs[doc_id] = _Doc(
                path=path,
                basis=basis,
                created=created,
                session_id=session_id
                if isinstance(session_id, str) and session_id
                else None,
                subagent=subagent if isinstance(subagent, bool) else None,
            )
        if len(page) < _PAGE:
            return docs
        offset += _PAGE


def trace_form(item_id: str) -> tuple[str, str] | None:
    """``(form, trace_id)`` when *item_id* names one trace, else ``None``.

    A chunk of a summary document is credited to the summary's trace.
    """
    parent = chunk_parent_id(item_id) or item_id
    for prefix, form in (
        (TRACE_SUMMARY_DOC_ID_PREFIX, TRACE_FORM_SUMMARY),
        (TRACE_ACTIVITY_ID_PREFIX, TRACE_FORM_ACTIVITY),
    ):
        if parent.startswith(prefix) and len(parent) > len(prefix):
            return form, parent[len(prefix) :]
    return None


@dataclass
class _TraceLedger:
    """The trace block's working state: in-window traces and their servings."""

    tallies: dict[str, _TraceTally]
    scan_limit: int
    scanned: int
    truncated: bool
    by_form: Counter[str] = field(default_factory=Counter)
    owned_by_documents: int = 0

    def credit(
        self, item_id: str, *, helpful: bool, unhelpful: bool, owned_by_document: bool
    ) -> bool:
        """Credit one ``(pack_id, item_id)`` serving; whether a trace took it."""
        named = trace_form(item_id)
        if named is None:
            return False
        form, trace_id = named
        tally = self.tallies.get(trace_id)
        if tally is None:
            return False
        tally.servings += 1
        tally.helpful += int(helpful)
        tally.unhelpful += int(unhelpful)
        self.by_form[form] += 1
        self.owned_by_documents += int(owned_by_document)
        return True

    def rollup(self) -> TraceYield:
        totals = TraceYieldRow(outcome="(all)")
        rows = {name: TraceYieldRow(outcome=name) for name in TRACE_OUTCOMES}
        rows[OTHER_OUTCOME] = TraceYieldRow(outcome=OTHER_OUTCOME)
        for tally in self.tallies.values():
            for target in (rows[tally.outcome], totals):
                _accumulate_trace(target, tally)
        return TraceYield(
            totals=totals,
            by_outcome=list(rows.values()),
            servings_by_form=dict(sorted(self.by_form.items())),
            servings_counted_as_documents=self.owned_by_documents,
            scan_limit=self.scan_limit,
            traces_scanned=self.scanned,
            truncated=self.truncated,
        )


def _recorded_traces(
    trace_store: TraceStore, since: datetime, until: datetime | None, limit: int
) -> _TraceLedger:
    """Traces created in the window, newest ``limit`` of them, by outcome.

    Reads one more than ``limit`` so a full window is reported as truncated
    rather than silently cut.
    """
    found = trace_store.query(since=since, until=until, limit=limit + 1)
    truncated = len(found) > limit
    kept = found[:limit]
    tallies: dict[str, _TraceTally] = {}
    for trace in kept:
        status = trace.outcome.status.value if trace.outcome is not None else None
        outcome = status if status in TRACE_OUTCOMES else OTHER_OUTCOME
        tallies[trace.trace_id] = _TraceTally(outcome=outcome)
    return _TraceLedger(
        tallies=tallies, scan_limit=limit, scanned=len(kept), truncated=truncated
    )


def _accumulate_trace(target: TraceYieldRow, tally: _TraceTally) -> None:
    target.recorded += 1
    target.servings += tally.servings
    target.helpful_servings += tally.helpful
    target.unhelpful_servings += tally.unhelpful
    if not tally.servings:
        target.never_served += 1
        return
    target.served += 1
    target.graded_helpful += int(tally.helpful > 0)
    target.graded_unhelpful += int(tally.unhelpful > 0)
    target.mixed += int(tally.helpful > 0 and tally.unhelpful > 0)
    target.served_ungraded += int(not tally.helpful and not tally.unhelpful)


def _served_ids(
    packs: Iterable[Event], limits: YieldLimits
) -> dict[str, dict[str, tuple[str | None, float | None]]]:
    """Per pack, each served item id once, with its type and token estimate."""
    served: dict[str, dict[str, tuple[str | None, float | None]]] = {}
    for event in packs:
        pack_id = event.entity_id
        payload = event.payload or {}
        if not pack_id or pack_id in served:
            continue
        limits.packs += 1
        if is_sectioned_payload(payload):
            limits.sectioned_packs += 1
        if not has_served_rows(payload):
            limits.packs_without_rows += 1
        rows = served[pack_id] = {}
        for row in served_rows_from_payload(
            pack_id, payload, include_sectioned=True, include_holdout=False
        ):
            if row.item_id and row.item_id not in rows:
                tokens = (
                    float(row.estimated_tokens)
                    if row.estimated_tokens is not None
                    else None
                )
                rows[row.item_id] = (row.item_type, tokens)
    return served


def _collect_verdicts(feedback: Iterable[Event]) -> _Verdicts:
    verdicts = _Verdicts()
    for row in iter_feedback_rows(feedback):
        helpful = set(row.helpful_item_ids)
        unhelpful = set(row.unhelpful_item_ids)
        if not (helpful or unhelpful):
            verdicts.without_ids += 1
            continue
        if not row.pack_id:
            continue
        verdicts.helpful[row.pack_id] |= helpful
        verdicts.unhelpful[row.pack_id] |= unhelpful
    return verdicts


def _add_serving(
    doc: _Doc, tokens: float | None, *, helpful: bool, unhelpful: bool
) -> None:
    doc.servings += 1
    if tokens is None:
        doc.untokened += 1
    spent = tokens or 0.0
    doc.tokens += spent
    if helpful:
        doc.helpful += 1
        doc.tokens_helpful += spent
    if unhelpful:
        doc.unhelpful += 1
        doc.tokens_unhelpful += spent


def _rollup(docs: Iterable[_Doc]) -> tuple[list[PathYield], PathYield]:
    rows: dict[str, PathYield] = {}
    totals = PathYield(path="(all)", basis="(all)")
    for doc in docs:
        row = rows.get(doc.path)
        if row is None:
            row = rows[doc.path] = PathYield(path=doc.path, basis=doc.basis)
        for target in (row, totals):
            _accumulate(target, doc)
    ordered = sorted(rows.values(), key=lambda r: (r.path == UNKNOWN_PATH, r.path))
    return ordered, totals


def _accumulate(target: PathYield | CaptureYieldRow, doc: _Doc) -> None:
    target.created += 1
    target.servings += doc.servings
    target.helpful_servings += doc.helpful
    target.unhelpful_servings += doc.unhelpful
    target.tokens_served += doc.tokens
    target.tokens_helpful += doc.tokens_helpful
    target.tokens_unhelpful += doc.tokens_unhelpful
    target.servings_without_token_estimate += doc.untokened
    if not doc.servings:
        target.never_served += 1
        return
    target.served += 1
    target.graded_helpful += int(doc.helpful > 0)
    target.graded_unhelpful += int(doc.unhelpful > 0)
    target.mixed += int(doc.helpful > 0 and doc.unhelpful > 0)
    target.served_ungraded += int(not doc.helpful and not doc.unhelpful)


def session_kind(subagent: bool | None, session_id: str | None) -> tuple[str, str]:
    """``(kind, basis)`` of one capture document's source session.

    ``metadata.subagent`` wins when recorded; else a ``metadata.session_id``
    beginning :data:`SUBAGENT_SESSION_PREFIX` is a sub-agent and any other id a
    top-level session; else :data:`KIND_UNKNOWN`.
    """
    if subagent is not None:
        return (KIND_SUBAGENT if subagent else KIND_TOP_LEVEL), KIND_BASIS_SUBAGENT_FLAG
    if session_id:
        kind = (
            KIND_SUBAGENT
            if session_id.startswith(SUBAGENT_SESSION_PREFIX)
            else KIND_TOP_LEVEL
        )
        return kind, KIND_BASIS_SESSION_ID
    return KIND_UNKNOWN, KIND_BASIS_NONE


def _capture_breakdown(
    docs: Mapping[str, _Doc],
    vector_store: VectorStore | None,
    *,
    threshold: float,
    max_docs: int,
    neighbors: int,
) -> CaptureBreakdown:
    dup = CaptureDuplication(
        threshold=threshold,
        neighbors=neighbors,
        max_docs=max_docs,
        population=len(docs),
        no_vector_store=vector_store is None,
    )
    newest = sorted(
        docs,
        key=lambda doc_id: (
            docs[doc_id].created or datetime.min.replace(tzinfo=UTC),
            doc_id,
        ),
        reverse=True,
    )
    probed = newest[: max(max_docs, 0)]
    pairs: dict[tuple[str, str], float] = {}
    checked: set[str] = set()
    if vector_store is not None:
        # Without a vector store nothing is probed, so no document is "over"
        # the bound either: all of them are unchecked for that one reason.
        dup.probed = len(probed)
        dup.over_bound = len(docs) - len(probed)
        found = embedding_close_pairs(
            probed, vector_store, threshold=threshold, neighbors=neighbors
        )
        # ``probed``, not ``embedded``: a document whose own neighbour query
        # raised has unknown partners and must not read as a singleton.
        pairs, checked, counts = found.pairs, set(found.probed), found.counts
        dup.embedded = counts.embedded
        dup.no_embedding = counts.no_embedding
        dup.embedding_unusable = counts.embedding_unusable
        dup.neighbor_queries_failed = counts.neighbor_queries_failed
        dup.dimension_mismatches = counts.dimension_mismatches
        dup.probes_saturated = counts.probes_saturated
    in_pair = {doc_id for pair in pairs for doc_id in pair}
    _tally_clusters(dup, pairs, docs)

    kind_rows = {k: CaptureYieldRow(group=k) for k in SESSION_KINDS}
    dup_rows = {d: CaptureYieldRow(group=d) for d in DUPLICATION_CLASSES}
    cross = {
        (k, d): CaptureCrossRow(session_kind=k, duplication=d)
        for k in SESSION_KINDS
        for d in DUPLICATION_CLASSES
    }
    totals = CaptureYieldRow(group="(all)")
    basis_counts: Counter[str] = Counter()
    for doc_id, doc in docs.items():
        kind, basis = session_kind(doc.subagent, doc.session_id)
        basis_counts[basis] += 1
        if doc_id in in_pair:
            klass = DUP_NEAR_DUPLICATE
        elif doc_id in checked:
            klass = DUP_SINGLETON
        else:
            klass = DUP_UNCHECKED
        for target in (totals, kind_rows[kind], dup_rows[klass]):
            _accumulate(target, doc)
        _accumulate_cross(cross[(kind, klass)], doc)
    return CaptureBreakdown(
        path=SESSION_CAPTURE_PATH,
        totals=totals,
        by_session_kind=list(kind_rows.values()),
        session_kind_basis=dict(sorted(basis_counts.items())),
        by_duplication=list(dup_rows.values()),
        cross=list(cross.values()),
        duplication=dup,
        limits=list(_CAPTURE_LIMIT_STATEMENTS),
    )


def _tally_clusters(
    dup: CaptureDuplication,
    pairs: Mapping[tuple[str, str], float],
    docs: Mapping[str, _Doc],
) -> None:
    dup.pairs = len(pairs)
    for a, b in pairs:
        sa, sb = docs[a].session_id, docs[b].session_id
        if sa is None or sb is None:
            dup.pairs_unknown_session += 1
        elif sa == sb:
            dup.pairs_same_session += 1
        else:
            dup.pairs_cross_session += 1
    clusters = connected_components(pairs)
    dup.clusters = len(clusters)
    dup.largest_cluster = max((len(c) for c in clusters), default=0)
    for members in clusters:
        sessions = {docs[m].session_id for m in members}
        known = sessions - {None}
        if len(known) > 1:
            dup.clusters_cross_session += 1
            dup.members_in_cross_session_clusters += len(members)
        elif None in sessions:
            dup.clusters_unknown_session += 1
            dup.members_in_unknown_session_clusters += len(members)
        else:
            dup.clusters_same_session += 1
            dup.members_in_same_session_clusters += len(members)


def _accumulate_cross(target: CaptureCrossRow, doc: _Doc) -> None:
    target.created += 1
    target.servings += doc.servings
    target.helpful_servings += doc.helpful
    target.unhelpful_servings += doc.unhelpful
    if doc.servings:
        target.served += 1
        target.graded_helpful += int(doc.helpful > 0)
        target.graded_unhelpful += int(doc.unhelpful > 0)


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str) and value:
        try:
            return _as_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


__all__ = [
    "BASIS_EVENT",
    "BASIS_NONE",
    "BASIS_SOURCE_SYSTEM",
    "DEFAULT_CAPTURE_MAX_DOCS",
    "DEFAULT_CAPTURE_THRESHOLD",
    "DUPLICATION_CLASSES",
    "DUP_NEAR_DUPLICATE",
    "DUP_SINGLETON",
    "DUP_UNCHECKED",
    "KIND_BASIS_NONE",
    "KIND_BASIS_SESSION_ID",
    "KIND_BASIS_SUBAGENT_FLAG",
    "KIND_SUBAGENT",
    "KIND_TOP_LEVEL",
    "KIND_UNKNOWN",
    "SESSION_CAPTURE_PATH",
    "SESSION_KINDS",
    "SUBAGENT_SESSION_PREFIX",
    "CREATION_OPERATIONS",
    "OTHER_OUTCOME",
    "TRACE_ACTIVITY_ID_PREFIX",
    "TRACE_FORM_ACTIVITY",
    "TRACE_FORM_SUMMARY",
    "TRACE_OUTCOMES",
    "UNKNOWN_PATH",
    "CaptureBreakdown",
    "CaptureCrossRow",
    "CaptureDuplication",
    "CaptureYieldRow",
    "MemoryYieldReport",
    "PathYield",
    "TraceYield",
    "TraceYieldRow",
    "YieldLimits",
    "classify_creation_path",
    "session_kind",
    "summarize_memory_yield",
    "trace_form",
]
