"""Command handlers for curate operations."""

from __future__ import annotations

import json
import math
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import structlog

from trellis.core.document_write import put_document
from trellis.core.error_sanitize import describe_validation_error
from trellis.core.precedent_document import is_promoted_precedent_document
from trellis.core.settings_registry import get_setting, resolve_source, validate_value
from trellis.core.text_safety import (
    contains_nul_or_lone_surrogate,
    replace_nul_and_lone_surrogates,
)
from trellis.core.vector_metadata import (
    VectorSyncOutcome,
    resolve_vector_store,
    sync_vector_metadata_outcome,
)
from trellis.errors import (
    DegradedStoreWriteError,
    NotFoundError,
    StaleStoreWriteError,
    StoreError,
    ValidationError,
)
from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.feedback.models import SUCCESS_RATING_THRESHOLD
from trellis.learning.candidate_auto_promote import AUTO_PROMOTE_REQUESTED_BY
from trellis.learning.tag_evolution import MIN_TAG_KEYWORD_LENGTH
from trellis.mutate.commands import Command, Operation
from trellis.mutate.evidence_ingest import EvidenceEmbedMode
from trellis.mutate.retention import (
    ARCHIVED_STATE,
    CURRENT_STATE,
    MAX_DOCUMENTS_SCANNED,
    ResolutionReport,
    RetentionCandidate,
    RetentionCriteria,
    resolve_candidates,
)
from trellis.retrieve.precedents import is_miner_candidate_event
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.schemas.measurement import Measurement
from trellis.schemas.observation import Observation
from trellis.schemas.trace import Trace
from trellis.schemas.well_known import (
    HAS_MEASUREMENT,
    HAS_OBSERVATION,
    MEASUREMENT,
    OBSERVATION,
    normalize_entity_name,
)
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.base.graph import AliasBindResult, AliasBindStatus, GraphStore
from trellis.stores.null.event_log import NullEventLog
from trellis.stores.registry import StoreRegistry
from trellis.stores.settings_store import SettingsStore, resolve_settings_path

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable


def _evidence_document_id(value: object) -> str:
    """``evidence.doc_id`` as a caller-chosen document id, or a refusal.

    A promoted precedent's document row is written by the promote handler
    alone. Letting a caller choose an id in that namespace would let it exempt
    its own memory from noise demotion and retention (see
    :mod:`trellis.core.precedent_document`).
    """
    if not isinstance(value, str) or not value:
        msg = "evidence.doc_id must be a non-empty string"
        raise ValidationError(msg, code="evidence_ingest_shape")
    if is_promoted_precedent_document(value):
        msg = (
            f"evidence.doc_id {value!r} is in the reserved precedent namespace; "
            "a precedent's document row is written by its promotion"
        )
        raise ValidationError(msg, code="reserved_document_id")
    return value


class EvidenceIngestHandler:
    """Store a document and optionally create its vector row.

    Agent-facing writes use ``soft`` embedding because the document is
    authoritative and must survive an embedder outage. Repair workers use
    ``strict`` because their success contract is the vector row itself.
    ``none`` lets ``save_memory`` keep embedding outside its process-wide
    deduplication lock.
    """

    def __init__(
        self,
        registry: StoreRegistry,
        *,
        embed: EvidenceEmbedMode = "soft",
        embedding_fn: Callable[[str], list[float]] | None = None,
        empty_error_code: str = "evidence_ingest_empty",
    ) -> None:
        self._registry = registry
        self._embed = embed
        self._embedding_fn = embedding_fn
        self._empty_error_code = empty_error_code

    def handle(self, command: Command) -> tuple[str | None, str]:
        evidence = command.args.get("evidence")
        if not isinstance(evidence, dict):
            msg = "evidence must be a mapping"
            raise ValidationError(msg, code="evidence_ingest_shape")

        doc_id = _evidence_document_id(evidence.get("doc_id"))
        content = evidence.get("content")
        uri = evidence.get("uri")
        if not isinstance(content, str):
            msg = "evidence.content must be a string"
            raise ValidationError(msg, code="evidence_ingest_shape")
        if not content.strip() and not (isinstance(uri, str) and bool(uri.strip())):
            msg = "evidence content or uri is required"
            raise ValidationError(msg, code=self._empty_error_code)

        raw_metadata = evidence.get("metadata")
        if raw_metadata is not None and not isinstance(raw_metadata, dict):
            msg = "evidence.metadata must be a mapping"
            raise ValidationError(msg, code="evidence_ingest_shape")
        metadata: dict[str, Any] = dict(raw_metadata or {})
        if isinstance(uri, str) and uri.strip():
            metadata.setdefault("uri", uri)

        preserve_updated_at = evidence.get("preserve_updated_at", False)
        if not isinstance(preserve_updated_at, bool):
            msg = "evidence.preserve_updated_at must be a boolean"
            raise ValidationError(msg, code="evidence_ingest_shape")

        requested_mode = evidence.get("embed_mode")
        if requested_mode is not None and requested_mode != self._embed:
            msg = (
                f"evidence embed_mode {requested_mode!r} does not match "
                f"handler mode {self._embed!r}"
            )
            raise ValidationError(msg, code="evidence_ingest_mode")

        from trellis.classify.ingest import classify_metadata_on_write  # noqa: PLC0415

        metadata = classify_metadata_on_write(
            metadata,
            content,
            source_system=str(metadata.get("source_system") or ""),
            doc_id=doc_id,
            registry=self._registry,
        )
        # Through the seam even though both embed modes below rebuild the
        # whole row: ``run_embed_on_ingest`` is gated on
        # ``TRELLIS_ENABLE_EMBED_ON_INGEST``, so on a deployment with that
        # flag off the ``"soft"`` arm mirrors nothing at all — and an
        # evidence write may be metadata-only (``preserve_updated_at``),
        # where there is no embed to piggyback on in either mode.
        put_document(
            self._registry.knowledge.document_store,
            resolve_vector_store(self._registry),
            doc_id,
            content,
            metadata,
            preserve_updated_at=preserve_updated_at,
        )

        if self._embed == "soft":
            from trellis.retrieve.embed_ingest_hook import (  # noqa: PLC0415
                run_embed_on_ingest,
            )

            run_embed_on_ingest(
                self._registry,
                doc_id,
                content,
                metadata,
                source=command.requested_by,
            )
        elif self._embed == "strict":
            from trellis.retrieve.embed_ingest_hook import (  # noqa: PLC0415
                build_vector_row,
            )

            vector_store = getattr(self._registry.knowledge, "vector_store", None)
            if vector_store is None:
                msg = "no vector store configured"
                raise StoreError(msg, store="vector")
            if self._embedding_fn is None:
                msg = "no embedding function configured"
                raise StoreError(msg, store="vector")
            raw_created_at = evidence.get("created_at")
            created_at = raw_created_at if isinstance(raw_created_at, str) else None
            row = build_vector_row(
                doc_id,
                content,
                metadata,
                self._embedding_fn,
                created_at=created_at,
            )
            vector_store.upsert(
                item_id=row["item_id"],
                vector=row["vector"],
                metadata=row["metadata"],
            )

        return doc_id, f"Evidence stored: {doc_id}"


def _bind_entity_name_alias(
    graph_store: GraphStore,
    *,
    entity_id: str,
    name: object,
) -> AliasBindResult | None:
    """Best-effort derived write inside an already-governed entity command."""
    if not isinstance(name, str):
        return None
    key = normalize_entity_name(name)
    if not key:
        return None

    try:
        twins = graph_store.query(properties={"name": name}, limit=2)
    except Exception:
        logger.exception(
            "entity_resolution_name_alias_twin_check_failed",
            entity_id=entity_id,
        )
        return None
    if any(str(node.get("node_id")) != entity_id for node in twins):
        logger.warning(
            "entity_resolution_name_alias_contested",
            entity_id=entity_id,
        )
        return None

    try:
        result = graph_store.bind_alias_if_absent(
            entity_id,
            NAME_ALIAS_SOURCE_SYSTEM,
            key,
            raw_name=name,
            stale_owner_name_key=key,
        )
    except Exception:
        logger.exception(
            "entity_resolution_alias_bind_failed",
            entity_id=entity_id,
        )
        return None
    if result.status is AliasBindStatus.CONFLICT:
        logger.warning(
            "entity_resolution_name_alias_lost_race",
            entity_id=entity_id,
            existing_entity_id=result.entity_id,
        )
    return result


_REWRITE_ATTEMPTS = 5


def _rewrite_current_node(
    store: GraphStore,
    node_id: str,
    build: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Write a new version of ``node_id`` computed from its current one.

    ``build`` receives the current row and returns the fields to change
    (``properties``, optionally ``node_type`` or ``document_ids``), or
    ``None`` to decline. Every other field is carried forward from the row:
    ``node_role`` and ``generation_spec`` are immutable across versions, and
    ``document_ids`` is normalised from ``[]`` to ``None`` ("no link").

    The write is a compare-and-set on the row's ``valid_from``, so it never
    re-creates a node purged after the read. When another writer replaced
    the version in between, the node is re-read and ``build`` re-applied, at
    most ``_REWRITE_ATTEMPTS`` times in all.

    Returns the fields written, or ``None`` when the node is absent (never
    existed, or purged mid-command) or ``build`` declined. Raises
    :class:`StoreError` when every attempt lost to a concurrent writer.
    """
    for _attempt in range(_REWRITE_ATTEMPTS):
        node = store.get_node(node_id)
        if node is None:
            return None
        changes = build(node)
        if changes is None:
            return None
        fields: dict[str, Any] = {
            "node_type": node["node_type"],
            "node_role": node.get("node_role", "semantic"),
            "generation_spec": node.get("generation_spec"),
            "document_ids": node.get("document_ids") or None,
            **changes,
        }
        if store.update_node_if_current(node_id, node["valid_from"], **fields):
            return fields
    msg = f"Node {node_id} changed during each of {_REWRITE_ATTEMPTS} rewrite attempts"
    raise StoreError(msg, store="graph")


class TraceIngestHandler:
    """Validate a trace, store it, and emit TRACE_INGESTED.

    Wires :data:`Operation.TRACE_INGEST` into the governed mutation pipeline
    so trace ingestion follows the same audit / idempotency / policy contract
    as every other mutation. ``args["trace"]`` may be either a ``Trace``
    instance or a dict; dicts are validated through ``Trace.model_validate``
    so the executor's validate stage owns schema enforcement, not the store.

    Idempotency: if a trace with the given ``trace_id`` already exists, the
    handler returns the existing id without re-emitting an event. Combined
    with ``Command.idempotency_key`` (executor-level FIFO + EventLog-backed
    cross-restart check), repeated submissions are safe.

    A blank ``trace_id`` (empty or whitespace-only) is refused with
    ``ValidationError`` code ``trace_id_empty`` before the store is read.
    Stored under ``""``, it would make every later blank-id trace "already
    ingested". The check sits here, not on ``Trace``, so a stored trace with
    an empty id still loads.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["trace"]
        trace = raw if isinstance(raw, Trace) else Trace.model_validate(raw)
        if not trace.trace_id.strip():
            msg = "trace_id must not be empty; omit it to have one generated"
            raise ValidationError(msg, errors=[msg], code="trace_id_empty")

        store = self._registry.operational.trace_store
        if store.get(trace.trace_id) is not None:
            return trace.trace_id, f"Trace already ingested: {trace.trace_id}"

        try:
            trace_id = store.append(trace)
        except StoreError:
            # Race: another writer landed the same trace between our get()
            # and append(). Treat as idempotent success rather than failure.
            if store.get(trace.trace_id) is not None:
                return trace.trace_id, f"Trace already ingested: {trace.trace_id}"
            raise

        self._registry.operational.event_log.emit(
            EventType.TRACE_INGESTED,
            source="mutation_executor",
            entity_id=trace_id,
            entity_type="trace",
            payload={
                "trace_id": trace_id,
                "source": trace.source.value,
                "intent": trace.intent,
                "domain": trace.context.domain if trace.context else None,
                "agent_id": trace.context.agent_id if trace.context else None,
            },
        )
        return trace_id, f"Trace ingested: {trace_id}"


def _write_precedent_document(
    registry: StoreRegistry,
    precedent_id: str,
    properties: dict[str, Any],
    *,
    source: str,
) -> None:
    """Write a promoted precedent's document row, then embed it when that is on.

    Keyword and semantic retrieval read documents and vector rows, never graph
    nodes, so without this row a precedent outside the graph axis's recency
    window (or unseeded) is unreachable by its own title. The text is the node's
    name and description, built by the same function the redaction scrub uses.
    A content write (``preserve_updated_at=False``); a document-store failure
    propagates, so the promotion reads as not landed and can be retried.
    """
    from trellis.learning.scoring import (  # noqa: PLC0415
        precedent_document_content,
        precedent_document_metadata,
    )
    from trellis.retrieve.embed_ingest_hook import (  # noqa: PLC0415
        run_embed_on_ingest,
    )

    content = precedent_document_content(
        properties.get("name"), properties.get("description")
    )
    if not content:
        return
    metadata = precedent_document_metadata(properties)
    put_document(
        registry.knowledge.document_store,
        resolve_vector_store(registry),
        precedent_id,
        content,
        metadata,
        preserve_updated_at=False,
    )
    run_embed_on_ingest(registry, precedent_id, content, metadata, source=source)


def _stamp_precedent_document(
    registry: StoreRegistry, precedent_id: str, lifecycle: dict[str, Any]
) -> bool:
    """Stamp ``lifecycle`` onto a precedent's document row, if it has one.

    A metadata-only write that also mirrors the stamp onto the vector row
    (:func:`~trellis.core.document_write.put_document`). A precedent promoted
    before it wrote a document has none, and is not given one here. Returns
    whether a row was stamped; a document-store failure propagates.
    """
    document_store = registry.knowledge.document_store
    document = document_store.get(precedent_id)
    if not document:
        return False
    metadata = dict(document.get("metadata") or {})
    metadata[LIFECYCLE_KEY] = lifecycle
    put_document(
        document_store,
        resolve_vector_store(registry),
        precedent_id,
        str(document.get("content") or ""),
        metadata,
        preserve_updated_at=True,
    )
    return True


def _precedent_document_state(registry: StoreRegistry, precedent_id: str) -> str | None:
    """``Lifecycle.state`` stamped on a precedent's document row, if it has one."""
    document = registry.knowledge.document_store.get(precedent_id)
    if not document:
        return None
    record = (document.get("metadata") or {}).get(LIFECYCLE_KEY)
    if not isinstance(record, dict):
        return None
    state = record.get("state")
    return state if isinstance(state, str) else None


def _rewrite_precedent_document(
    registry: StoreRegistry, precedent_id: str, properties: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Re-derive a precedent's existing document row from its scrubbed properties.

    Keeps the row's other metadata (a noise tag, a lifecycle stamp) and
    replaces its text and title with what the node now says. ``None`` when
    the precedent has no document row; nothing is created.
    """
    from trellis.learning.scoring import (  # noqa: PLC0415
        precedent_document_content,
    )

    document_store = registry.knowledge.document_store
    document = document_store.get(precedent_id)
    if not document:
        return None
    content = precedent_document_content(
        properties.get("name"), properties.get("description")
    )
    metadata = dict(document.get("metadata") or {})
    name = properties.get("name")
    if isinstance(name, str) and name.strip():
        metadata["title"] = name.strip()
    put_document(
        document_store,
        resolve_vector_store(registry),
        precedent_id,
        content,
        metadata,
        preserve_updated_at=False,
    )
    return content, metadata


def _tombstone_precedent_document(registry: StoreRegistry, precedent_id: str) -> None:
    """Blank a redacted precedent's document row, which holds the excerpt too.

    The document plane has no governed delete, so the row keeps its id with no
    text, no title and an archived stamp. Nothing is created for a precedent
    that never had one.
    """
    from trellis.learning.scoring import PRECEDENT_DOCUMENT_FORM  # noqa: PLC0415

    document_store = registry.knowledge.document_store
    if not document_store.get(precedent_id):
        return
    lifecycle = Lifecycle(
        state="archived",
        valid_until=datetime.now(UTC),
        deprecation_reason="redacted",
    ).model_dump(mode="json")
    put_document(
        document_store,
        resolve_vector_store(registry),
        precedent_id,
        "",
        {"document_form": PRECEDENT_DOCUMENT_FORM, LIFECYCLE_KEY: lifecycle},
        preserve_updated_at=False,
    )


class PrecedentPromoteHandler:
    """Emit PRECEDENT_PROMOTED event with title/description/domain from args.

    Serves two sources that both feed ``get_lessons`` (which reads
    ``PRECEDENT_PROMOTED`` events via ``list_precedents``):

    * **Trace-mined** precedents pass ``trace_id`` and default to
      ``entity_type="trace"`` — the emitted payload is unchanged.
    * **Learning-scoring** promotions pass the promoted graph entity's id as
      ``target_id`` with ``entity_type="precedent"`` and no ``trace_id``;
      without this the entity landed but stayed invisible to ``get_lessons``.

    The learning path also writes the precedent's **document row** (its name
    and description, tagged ``decision``) before the event, and embeds it when
    embed-on-ingest is on. The graph axis reaches a node only through seeds or
    its recency window, so without the row keyword and semantic retrieval
    never served a promoted precedent. The row goes first: a failure raises, the
    promotion reads as not landed, and the retry rewrites it. The event payload
    is unchanged.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        args = command.args
        payload: dict[str, Any] = {
            "title": args["title"],
            "description": args["description"],
            "domain": args.get("domain"),
        }
        # Preserve the trace-path payload byte-for-byte; only add provenance
        # keys for the entity-sourced (learning) path so nothing downstream
        # that reads ``trace_id`` on a trace precedent is surprised.
        trace_id = args.get("trace_id")
        if trace_id is not None:
            payload["trace_id"] = trace_id
        source_item_id = args.get("source_item_id")
        if source_item_id is not None:
            payload["source_item_id"] = source_item_id
        # How ``title`` was derived (see ``trellis.learning.scoring``'s
        # ``NAME_SOURCE_*``): the log is immutable, so this is how a reader
        # tells a title copied from the source from one that was not.
        name_source = args.get("name_source")
        if name_source is not None:
            payload["name_source"] = name_source

        if (
            args.get("entity_type") == "precedent"
            and trace_id is None
            and command.target_id
        ):
            node = self._registry.knowledge.graph_store.get_node(command.target_id)
            if node is not None and node["node_type"] == "precedent":
                _write_precedent_document(
                    self._registry,
                    command.target_id,
                    node.get("properties") or {},
                    source=command.requested_by,
                )

        event = self._registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="mutation_executor",
            entity_id=command.target_id or trace_id,
            entity_type=args.get("entity_type", "trace"),
            payload=payload,
        )
        return event.event_id, f"Precedent promoted: {args['title']}"


#: Requester identity :mod:`trellis.learning.precedent_auto_demote`'s
#: ``PrecedentAutoDemotePolicy`` stamps on every ``precedent.demote``
#: command it submits. Defined here, not in ``trellis.learning``, so that
#: constant and :class:`PrecedentDemoteHandler`'s own provenance check
#: below agree on the same string by construction rather than by two
#: modules happening to copy the same literal — the auto path imports
#: this name rather than minting its own copy. Mirrors
#: :data:`~trellis.learning.candidate_auto_promote.AUTO_PROMOTE_REQUESTED_BY`'s
#: shape and reason for existing: an audit reader filtering by
#: ``requested_by`` should be able to tell "a human demoted this" from
#: "the automatic policy demoted this" at a glance. Deliberately a
#: *different* string from ``AUTO_PROMOTE_REQUESTED_BY`` — see
#: :meth:`PrecedentDemoteHandler.handle`'s docstring for why the two must
#: never collide.
AUTO_DEMOTE_REQUESTED_BY = "auto:learning_candidate_demote"

#: Mirrors ``MAX_REDACTION_REASON_CHARS``: the recorded justification is
#: written verbatim to the append-only audit log and must stay short and
#: content-free.
MAX_DEMOTION_REASON_CHARS = 2000


def _was_auto_promoted(event_log: EventLog, entity_id: str) -> bool:
    """Whether ``entity_id``'s ``precedent.promote`` event was itself automatic.

    Re-derives provenance from the audit trail rather than trusting a
    caller-supplied flag. ``submit_learning_promotion``'s
    ``PRECEDENT_PROMOTE`` command sets ``target_id=entity_id``
    (``trellis/learning/scoring.py``), and the executor stamps
    ``requested_by`` onto every ``MUTATION_EXECUTED`` row for free
    (``_emit_event``, ``trellis/mutate/executor.py``). A human-approved
    precedent's promote event carries a different ``requested_by`` (e.g.
    ``cli:promote-learning``, ``api:review-queue``) and is therefore
    absent from this filtered query regardless of what the demotion
    caller claims — the narrowing cannot be bypassed by a caller that
    merely asserts "trust me, this one's auto."

    Mirrors ``candidate_auto_promote._scan_already_promoted_entity_ids``'s
    query shape but adds the ``requested_by`` filter that function
    deliberately omits (it answers a different question: "promoted by
    anyone, ever", not "promoted by the automatic policy specifically") —
    which is why this is a new function rather than a call to that one.
    """
    events = event_log.get_events(
        event_type=EventType.MUTATION_EXECUTED,
        entity_id=entity_id,
        order="desc",
        payload_filters={
            "operation": Operation.PRECEDENT_PROMOTE.value,
            "status": "success",
            "requested_by": AUTO_PROMOTE_REQUESTED_BY,
        },
        limit=1,
    )
    return bool(events)


def _is_promoted_precedent_event(event_log: EventLog, entity_id: str) -> bool:
    """Whether ``entity_id`` has a promotion event, whichever handler path wrote it.

    ``PrecedentPromoteHandler`` emits this event for two unlike sources
    (its own docstring): a trace-sourced precedent, whose ``entity_id``
    names a trace and never gets a graph entity, and a learning-candidate
    promotion, whose ``entity_id`` is a graph entity
    ``submit_learning_promotion`` minted first. This answers "is this id
    a real precedent, just not a graph-backed one" so
    :class:`PrecedentDemoteHandler` can name that gap specifically
    instead of reporting the same "Node not found" a garbage id gets.

    A row the precedent miner wrote (:func:`is_miner_candidate_event`) is
    not a promotion: it is an ungoverned candidate, so an id that has only
    that row reads as "not found", the same as an id with no event at all.
    The scan reads every ``PRECEDENT_PROMOTED`` row for the id rather than
    the newest one, so a miner row cannot shadow a real promotion.
    """
    events = event_log.get_events(
        event_type=EventType.PRECEDENT_PROMOTED,
        entity_id=entity_id,
    )
    return any(not is_miner_candidate_event(e) for e in events)


def _current_lifecycle_state(node: dict[str, Any]) -> str:
    """``node``'s current ``Lifecycle.state``, fail-open to ``CURRENT_STATE``.

    A freshly-promoted precedent's entity
    (``trellis.learning.scoring.submit_learning_promotion``) never writes
    ``LIFECYCLE_KEY`` at all, and a malformed record is no more
    informative than an absent one — both read as current, matching
    ``retrieve.lifecycle``'s own fail-open convention for this key: a
    bad or missing lifecycle record is never by itself a reason to
    refuse. An empty ``state`` is malformed, not a state of its own.
    """
    record = node["properties"].get(LIFECYCLE_KEY)
    if not isinstance(record, dict):
        return CURRENT_STATE
    state = record.get("state")
    return state if isinstance(state, str) and state else CURRENT_STATE


def precedent_already_demoted_message(target_id: str) -> str:
    """The exact message :class:`PrecedentDemoteHandler` returns for a no-op.

    A single source of truth rather than a literal repeated at each
    caller surface: the REST route matches against this to populate its
    ``already_demoted`` field (see ``trellis_api.routes.curate.demote``),
    and a test proves the two stay byte-identical.
    """
    return f"Precedent {target_id} is already demoted; no changes made"


class PrecedentDemoteHandler:
    """Stamp a precedent node ``Lifecycle.state="demoted"``, emitting PRECEDENT_DEMOTED.

    Wires :data:`Operation.PRECEDENT_DEMOTE`. Modelled on
    ``RedactionApplyHandler``'s node_type/target-lookup pattern and
    ``RetentionPruneHandler._archive``'s graph write — the difference is
    the state written (``"demoted"`` rather than ``"archived"``) and that
    this PR is manual-trigger only.

    **Provenance depends on who is demoting, not on who is being
    demoted.** A caller whose own ``Command.requested_by`` equals
    :data:`AUTO_DEMOTE_REQUESTED_BY` —
    :mod:`trellis.learning.precedent_auto_demote`'s
    ``PrecedentAutoDemotePolicy`` in ``mode="commit"`` — may demote only
    a precedent whose own ``precedent.promote`` event was itself
    automatic (:func:`_was_auto_promoted`); every other caller (a human,
    via CLI/MCP/API) may demote any precedent *that has a graph node*
    regardless of how it was promoted. Both the promotion and demotion
    policies still ship ``enabled=False``, so no auto-created precedent
    exists in a default deployment — but the check itself does not read
    that flag; it pins the contract for the moment an operator turns
    either policy's commit mode on. Both halves are pinned by tests with
    deliberately defective subjects: one that skips the check, or trusts
    a caller-supplied boolean instead of re-deriving it from the event
    log, must fail the "automatic caller is narrowed" test; one that
    refuses every human too must fail the "human may demote any
    precedent" test.

    **Only a learning-promoted precedent has a graph node.**
    ``PrecedentPromoteHandler`` serves two sources (see its own
    docstring): a trace- or miner-sourced precedent's
    ``PRECEDENT_PROMOTED`` event names a trace id and mints no entity at
    all, while ``submit_learning_promotion``'s learning-candidate path
    mints a real graph entity first and then emits the event against
    *that* id. Only the second kind can be demoted, because demotion is
    a stamp on a graph node. Demoting the first kind refuses with a
    message naming that gap (:func:`_is_promoted_precedent_event`)
    rather than the generic "Node not found" a typo'd or unrelated id
    gets.

    The check is always re-derived inside :meth:`handle` from the event
    log, never from a caller-supplied flag on ``command.args`` — the
    audit trail is authoritative and a caller cannot assert "trust me,
    this one's auto."

    **Idempotent re-demotion, and only ``current`` may be demoted.** The
    handler reads the target's current ``Lifecycle.state`` before
    writing. A target already ``"demoted"`` is a no-op: no new SCD-2
    version, no new ``PRECEDENT_DEMOTED`` event, the stored
    ``deprecation_reason`` unchanged — the result still carries a
    distinct message so every caller surface can say "already demoted"
    rather than imply a hollow repeat write. A target in any other
    non-``current`` state (``superseded``, ``archived``, ``draft``,
    ``deprecated``) is refused, naming the actual state, because demotion
    is defined only as a transition *out of* currency — overwriting
    someone else's archival or supersession record would destroy
    provenance a different operation is responsible for.

    SCD-2: the write closes the current node version and opens a new one
    (:func:`_rewrite_current_node`), so the pre-demotion state stays
    readable via ``get_node_history`` and ``as_of``. The reversal verb is
    :data:`Operation.PRECEDENT_REINSTATE` (:class:`PrecedentReinstateHandler`)
    — ``entity.update`` refuses a write to ``properties.lifecycle`` on a
    precedent node, so it is no longer a way to undo this.

    **Vector sync.** The graph write alone leaves a semantic hit serving
    the pre-demotion snapshot (a vector row's metadata is an embed-time
    copy — see :func:`_sync_vector_lifecycle`), so the handler re-stamps
    the precedent's vector row with the same ``lifecycle`` dict it just
    wrote to the graph, in the same call.

    **Document row.** A precedent's document row (see
    :class:`PrecedentPromoteHandler`) is stamped first, so a keyword hit and,
    through the mirror, a semantic hit stop serving it; a failure raises before
    the node changes. A precedent promoted before that row existed has none, and
    none is created.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "precedent.demote requires a non-empty reason for the audit trail"
            raise ValidationError(msg, errors=[msg], code="demotion_reason_required")
        if len(reason) > MAX_DEMOTION_REASON_CHARS:
            msg = (
                f"precedent.demote reason exceeds {MAX_DEMOTION_REASON_CHARS} "
                "chars; it is written verbatim to the append-only audit log — "
                "keep it short and content-free"
            )
            raise ValidationError(msg, errors=[msg], code="demotion_reason_too_long")

        graph = self._registry.knowledge.graph_store
        event_log = self._registry.operational.event_log
        node = graph.get_node(target_id)
        if node is None:
            if _is_promoted_precedent_event(event_log, target_id):
                # A real precedent — it has a non-miner PRECEDENT_PROMOTED event — but
                # the trace-sourced path never mints a graph entity
                # for it (see PrecedentPromoteHandler's docstring), so there
                # is no node to stamp. Distinct from "nothing by this id
                # exists at all" below.
                msg = (
                    f"Precedent {target_id} cannot be demoted: only promoted "
                    "learning precedents can be demoted; this id has no "
                    "graph node"
                )
                raise ValidationError(msg, errors=[msg], code="target_not_graph_backed")
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")
        if node["node_type"] != "precedent":
            msg = (
                f"Node {target_id} is not a precedent (node_type={node['node_type']!r})"
            )
            raise ValidationError(msg, errors=[msg], code="target_not_precedent")

        if command.requested_by == AUTO_DEMOTE_REQUESTED_BY and not _was_auto_promoted(
            event_log, target_id
        ):
            msg = (
                f"Precedent {target_id} was not promoted by the automatic "
                "policy; the automatic demotion path may only demote "
                "precedents it (or an earlier run of it) promoted"
            )
            raise ValidationError(msg, errors=[msg], code="target_not_auto_promoted")

        # Read current state before writing, so re-demotion and a
        # non-current target are both handled below. Absent or malformed
        # LIFECYCLE_KEY reads as CURRENT_STATE — submit_learning_promotion
        # never stamps one at all, and retrieve.lifecycle's own fail-open
        # convention treats a bad record the same way: never a reason to
        # refuse on its own.
        current_state = _current_lifecycle_state(node)
        if current_state == "demoted":
            # Idempotent no-op: no new SCD-2 version, no new
            # PRECEDENT_DEMOTED event, the stored reason untouched. The
            # message is the only channel a handler has to say "nothing
            # changed" (CommandResult carries no handler-settable metadata
            # on the SUCCESS path) — surfaces that need a structured
            # signal match against precedent_already_demoted_message(...).
            return target_id, precedent_already_demoted_message(target_id)
        if current_state != CURRENT_STATE:
            msg = (
                f"Precedent {target_id} cannot be demoted: its lifecycle "
                f"state is {current_state!r}, not {CURRENT_STATE!r}"
            )
            raise ValidationError(msg, errors=[msg], code="target_not_current")

        # Field-by-field, matching RetentionPruneHandler._archive's
        # convention of a fresh Lifecycle per transition rather than
        # reading-and-merging the prior record (RetentionRestoreHandler's
        # _restore does the same for its own transition):
        #   - state: "demoted" is this transition's entire point.
        #   - valid_until: stamps when currency ended, same as _archive.
        #   - deprecation_reason: the caller's audited reason, required
        #     non-empty above, same field _archive writes its reason to.
        #   - valid_from, superseded_by: never set by any writer in this
        #     codebase today (Lifecycle's own docstring: "no classifier
        #     populates it ... yet"), so a "current" record never holds
        #     one to carry forward.
        #   - classification_version: a schema-version marker no
        #     transition bumps; stays at Lifecycle's own default.
        lifecycle = Lifecycle(
            state="demoted",
            valid_until=datetime.now(UTC),
            deprecation_reason=reason,
        ).model_dump(mode="json")

        def demote(current: dict[str, Any]) -> dict[str, Any]:
            props = dict(current["properties"])
            props[LIFECYCLE_KEY] = lifecycle
            return {"properties": props}

        # Before the node, so a failure leaves a precedent no axis withholds
        # rather than one the graph withholds and the keyword axis serves.
        _stamp_precedent_document(self._registry, target_id, lifecycle)

        if _rewrite_current_node(graph, target_id, demote) is None:
            # Vanished between the read above and the write (purged
            # mid-command) — refused the same way as a target that was
            # never there.
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        # Keep the semantic path from continuing to serve the
        # pre-demotion snapshot: a vector row's metadata is an embed-time
        # copy, not a read of the graph store (#338-style gap).
        vector_sync_outcome = _sync_vector_lifecycle(
            self._registry,
            target_id,
            lifecycle,
            command=command,
            log_event="precedent_demote_vector_lifecycle_sync_failed",
        )

        event_payload: dict[str, Any] = {
            "target_id": target_id,
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
        }
        if vector_sync_outcome == "failed":
            # Same fact ``command.metadata["handler_warnings"]`` already
            # carries into ``CommandResult.warnings`` -- a consumer of the
            # event log (rather than the synchronous CommandResult) needs
            # its own way to see the semantic path went stale.
            event_payload["vector_sync_failed"] = True
        self._registry.operational.event_log.emit(
            EventType.PRECEDENT_DEMOTED,
            source="mutation_executor",
            entity_id=target_id,
            entity_type="precedent",
            payload=event_payload,
        )
        return target_id, f"Precedent demoted: {target_id}"


class PrecedentReinstateHandler:
    """Stamp a demoted precedent node back to ``Lifecycle.state="current"``.

    Wires :data:`Operation.PRECEDENT_REINSTATE` — the reversal verb
    :class:`PrecedentDemoteHandler` shipped without. Modelled closely on
    that handler: same reason validation
    (:data:`MAX_DEMOTION_REASON_CHARS`), same not-found/not-graph-backed/
    not-a-precedent checks, same ``_rewrite_current_node`` SCD-2 write and
    :func:`_sync_vector_lifecycle` call.

    **Human-only.** Unlike demotion (where the automatic policy may demote
    only what it itself promoted), reinstatement has no automatic path at
    all: a caller whose ``Command.requested_by`` equals
    :data:`AUTO_DEMOTE_REQUESTED_BY` is refused outright, narrowing or not.
    A policy that can demote only its own promotions reinstating one would
    be a silent self-reversal loop with no human in it.

    **Only a currently-demoted target may be reinstated.** There is no
    idempotent-no-op branch here the way ``PrecedentDemoteHandler`` has
    one for re-demotion: reinstating a target that is already ``current``,
    ``archived``, ``superseded``, or anything else is refused, legibly
    naming the actual state — which also covers "reinstating twice"
    without a special case, since the first reinstate already moved the
    state to ``current``.

    SCD-2: the write closes the demoted version and opens a fresh
    ``current`` one, so the demoted version stays readable via
    ``get_node_history``/``as_of``. The vector row is re-synced in the
    same call, so a semantic hit stops carrying the demoted snapshot. The
    precedent's document row, when it has one, is re-stamped after the node: a
    failure there leaves the row withheld, and is reported on the result's
    warnings and as ``document_sync_failed`` on the event. Re-issuing the
    reinstate then repairs it: a target whose node is already ``current`` but
    whose document row is still ``demoted`` is accepted, re-stamps the row and
    nothing else, and marks the event ``document_resync_only``.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def _restamp_document(
        self,
        command: Command,
        target_id: str,
        lifecycle: dict[str, Any],
        *,
        strict: bool,
    ) -> bool:
        """Re-stamp the precedent's document row; ``True`` when that failed.

        After the node write a failure is fail-soft: the reinstatement stands,
        the row stays withheld, and the warning names the repair (re-issue the
        command). ``strict`` is the repair itself, where the node is already
        current and this stamp is the whole job, so a failure raises rather
        than reporting a repair that did not happen.
        """
        try:
            _stamp_precedent_document(self._registry, target_id, lifecycle)
        except Exception:
            if strict:
                raise
            logger.warning(
                "precedent_reinstate_document_sync_failed",
                item_id=target_id,
                exc_info=True,
            )
            command.metadata.setdefault("handler_warnings", []).append(
                f"precedent_reinstate_document_sync_failed: document row for "
                f"{target_id} was not re-stamped; keyword retrieval keeps "
                "withholding it until it is. Re-issue precedent.reinstate "
                "for this id to re-stamp the row"
            )
            return True
        return False

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "precedent.reinstate requires a non-empty reason for the audit trail"
            raise ValidationError(msg, errors=[msg], code="reinstate_reason_required")
        if len(reason) > MAX_DEMOTION_REASON_CHARS:
            msg = (
                f"precedent.reinstate reason exceeds {MAX_DEMOTION_REASON_CHARS} "
                "chars; it is written verbatim to the append-only audit log — "
                "keep it short and content-free"
            )
            raise ValidationError(msg, errors=[msg], code="reinstate_reason_too_long")

        if command.requested_by == AUTO_DEMOTE_REQUESTED_BY:
            # Defence in depth: the executor's unattended-writer roster
            # (trellis.mutate.immutable_core.UNATTENDED_WRITERS) already
            # permits this identity only Operation.PRECEDENT_DEMOTE, so a
            # command naming it here is refused at Stage 1, before this
            # handler ever runs — this branch is currently unreachable
            # through that path. Kept rather than removed in case a
            # future roster change ever widens that identity's allowed
            # operations.
            msg = (
                "precedent.reinstate is human-only; the automatic demotion "
                "policy's identity may not reinstate any precedent"
            )
            raise ValidationError(msg, errors=[msg], code="reinstate_requires_human")

        graph = self._registry.knowledge.graph_store
        event_log = self._registry.operational.event_log
        node = graph.get_node(target_id)
        if node is None:
            if _is_promoted_precedent_event(event_log, target_id):
                msg = (
                    f"Precedent {target_id} cannot be reinstated: only promoted "
                    "learning precedents can be reinstated; this id has no "
                    "graph node"
                )
                raise ValidationError(msg, errors=[msg], code="target_not_graph_backed")
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")
        if node["node_type"] != "precedent":
            msg = (
                f"Node {target_id} is not a precedent (node_type={node['node_type']!r})"
            )
            raise ValidationError(msg, errors=[msg], code="target_not_precedent")

        current_state = _current_lifecycle_state(node)
        # A reinstate whose document stamp failed leaves the node ``current``
        # and the row ``demoted``. That is the one case a ``current`` target
        # is accepted: the retry re-stamps the row and touches nothing else.
        document_resync_only = (
            current_state == CURRENT_STATE
            and _precedent_document_state(self._registry, target_id) == "demoted"
        )
        if current_state != "demoted" and not document_resync_only:
            msg = (
                f"Precedent {target_id} cannot be reinstated: its lifecycle "
                f"state is {current_state!r}, not 'demoted'"
            )
            raise ValidationError(msg, errors=[msg], code="target_not_demoted")

        # A fresh Lifecycle for the transition, matching
        # PrecedentDemoteHandler's own convention rather than reading and
        # merging the demoted record forward: the demotion's valid_until /
        # deprecation_reason belong to that transition, not this one.
        lifecycle = Lifecycle(state=CURRENT_STATE).model_dump(mode="json")

        def reinstate(current: dict[str, Any]) -> dict[str, Any]:
            props = dict(current["properties"])
            props[LIFECYCLE_KEY] = lifecycle
            return {"properties": props}

        if (
            not document_resync_only
            and _rewrite_current_node(graph, target_id, reinstate) is None
        ):
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        document_sync_failed = self._restamp_document(
            command, target_id, lifecycle, strict=document_resync_only
        )

        vector_sync_outcome = _sync_vector_lifecycle(
            self._registry,
            target_id,
            lifecycle,
            command=command,
            log_event="precedent_reinstate_vector_lifecycle_sync_failed",
        )

        event_payload: dict[str, Any] = {
            "target_id": target_id,
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
        }
        if vector_sync_outcome == "failed":
            event_payload["vector_sync_failed"] = True
        if document_sync_failed:
            event_payload["document_sync_failed"] = True
        if document_resync_only:
            event_payload["document_resync_only"] = True
        self._registry.operational.event_log.emit(
            EventType.PRECEDENT_REINSTATED,
            source="mutation_executor",
            entity_id=target_id,
            entity_type="precedent",
            payload=event_payload,
        )
        return target_id, f"Precedent reinstated: {target_id}"


class LabelAddHandler:
    """Read node from graph store, add label to properties, write a new version."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        label = command.args["label"]
        store = self._registry.knowledge.graph_store

        def add(node: dict[str, Any]) -> dict[str, Any]:
            props = dict(node["properties"])
            labels = props.get("labels", [])
            if label not in labels:
                labels.append(label)
            props["labels"] = labels
            return {"properties": props}

        if _rewrite_current_node(store, target_id, add) is None:
            # Raised, not returned: the executor audits any return as a write
            # that happened. ``code`` becomes the rejection's audit reason. A
            # node purged mid-command is refused the same way.
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        self._registry.operational.event_log.emit(
            EventType.LABEL_ADDED,
            source="mutation_executor",
            entity_id=target_id,
            payload={"label": label},
        )
        return target_id, f"Label '{label}' added to {target_id}"


class LabelRemoveHandler:
    """Read node, remove label from properties, write a new version."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        label = command.args["label"]
        store = self._registry.knowledge.graph_store

        def remove(node: dict[str, Any]) -> dict[str, Any]:
            props = dict(node["properties"])
            labels = props.get("labels", [])
            if label in labels:
                labels.remove(label)
            props["labels"] = labels
            return {"properties": props}

        if _rewrite_current_node(store, target_id, remove) is None:
            # Raised, as in LabelAddHandler.
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        self._registry.operational.event_log.emit(
            EventType.LABEL_REMOVED,
            source="mutation_executor",
            entity_id=target_id,
            payload={"label": label},
        )
        return target_id, f"Label '{label}' removed from {target_id}"


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


class SettingsUpdateHandler:
    """Validate and persist one settings-store override via SettingsStore.

    This is the governed write path the settings registry
    (:mod:`trellis.core.settings_registry`) was always meant to gate: a
    name/value pair is checked against the registry's declared
    ``type``/``minimum``/``maximum``/``enum`` before anything is written.

    ``args["value"] is None`` means **remove the override** — there is no
    separate ``SETTINGS_RESET`` operation. This keeps one verb for the
    knob's whole lifecycle (set, change, clear) instead of two operations
    that would need to agree on the same registry lookup and the same
    audit event shape; a caller already has to pass a literal ``null``
    to clear a JSON field everywhere else in this API, so this is not a
    new idiom.

    Every refusal — unknown name, wrong type, out of range, not in the
    declared enum, or the store itself degraded/stale — is raised as a
    :class:`~trellis.errors.ValidationError` so the executor's normal
    ``REJECTED`` path records the real reason. Nothing here clamps a bad
    value to the nearest bound or to the default: a bad write is refused,
    never silently corrected.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        name = command.args["name"]
        raw_value = command.args.get("value")

        # Defence in depth: this handler is only ever registered via
        # create_settings_handlers, reachable only through the admin
        # route/CLI, which both stamp target_type and
        # target_id correctly -- but an untargeted or mistargeted Command
        # reaching here by some future wiring mistake must still refuse
        # rather than write, exactly as an entity_type="setting" policy
        # expects to be able to gate this operation on those two fields.
        if command.target_type != "setting" or command.target_id != name:
            msg = (
                "settings.update requires target_type='setting' and "
                f"target_id={name!r}; got target_type={command.target_type!r}, "
                f"target_id={command.target_id!r}"
            )
            raise ValidationError(msg, errors=[msg], code="untargeted_command")

        spec = get_setting(name)
        if spec is None:
            msg = f"Unknown setting: {name!r}"
            raise ValidationError(msg, errors=[msg], code="unknown_setting")

        # settings_writable=False means no code path ever reads this
        # knob's stored value -- not even after a restart (that is
        # restart_required, which stays writable). Storing one anyway
        # would be accepted and echoed back by GET /settings as if it
        # mattered, with nothing that could ever make it real. Clearing
        # a stray legacy row (raw_value is None) is still allowed.
        if raw_value is not None and not spec.settings_writable:
            msg = (
                f"{name} is read from {spec.env_var} only, once at "
                "process start; the settings store is never consulted, "
                "so a stored value here would never take effect. Set "
                f"{spec.env_var} instead."
            )
            raise ValidationError(msg, errors=[msg], code="settings_write_refused")

        path = resolve_settings_path(self._registry.stores_dir)
        if path is None:
            # Same precedent as trellis_api.routes.policies._get_policy_store:
            # no stores_dir configured means no file-backed settings store
            # exists to write to. Not a caller-input problem (ValidationError
            # would mislabel it as one the caller can fix by retrying with a
            # different value), so this is left as an ordinary exception for
            # the executor's handler-panic path to turn into a FAILED result.
            msg = "stores_dir must be set on registry to use SettingsStore"
            raise ValueError(msg)
        store = SettingsStore(path)
        existing = store.get(name)
        old_value = existing.value if existing is not None else None

        if raw_value is None:
            try:
                removed = store.remove(name)
            except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
                raise ValidationError(
                    exc.message, errors=[exc.message], code=exc.code.lower()
                ) from exc
            new_value: bool | int | float | str | None = None
            action = "reset to default/env" if removed else "already unset"
        else:
            try:
                new_value = validate_value(spec, raw_value)
            except ValueError as exc:
                # validate_value always raises ValueError(msg) with a single
                # string argument composed by _coerce_*/_check_bounds, never
                # a wrapped driver/library exception, so args[0] is exactly
                # that message -- this is a structured-field read, not a
                # bare str(exc) stringification of the caught exception.
                msg = exc.args[0] if exc.args else "invalid value"
                raise ValidationError(msg, errors=[msg], code="invalid_value") from exc
            try:
                store.set(name, new_value)
            except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
                raise ValidationError(
                    exc.message, errors=[exc.message], code=exc.code.lower()
                ) from exc
            action = f"set to {new_value!r}"

        overridden_by = resolve_source(spec, store.as_values())

        payload: dict[str, Any] = {
            "name": name,
            "old_value": old_value,
            "new_value": new_value,
            "overridden_by_at_write": overridden_by,
            "changed_by": command.requested_by,
            "settings_live": spec.settings_live,
            "restart_required": spec.restart_required,
        }
        requested_by_claim = command.args.get("requested_by_claim")
        if requested_by_claim is not None:
            payload["requested_by_claim"] = requested_by_claim
        self._registry.operational.event_log.emit(
            EventType.SETTINGS_UPDATED,
            source="mutation_executor",
            entity_id=name,
            payload=payload,
        )
        return name, f"Setting {name!r} {action}"


def _validate_classify_rule_metrics(command_args: dict[str, Any]) -> None:
    """Refuse a ``classify_rule.apply`` whose carried metrics are unusable.

    ``support``/``precision``/``lift`` are optional, caller-supplied,
    copied verbatim onto the ``CLASSIFY_RULE_APPLIED`` audit event -- the
    REST DTO (:class:`~trellis_wire.dtos.ClassifyRuleApplyRequest`, which
    sets ``allow_inf_nan=False``) narrows what reaches here for that one
    surface, but it is not a governed boundary: the CLI has no finiteness
    check of its own, and a direct ``MutationExecutor.execute`` from
    Python skips the DTO too. This is the one check every caller passes
    through -- MCP cannot reach ``classify_rule.apply`` at all, since its
    handlers register only on ``build_classify_rules_executor``, which
    ``build_curate_executor`` (the executor MCP's ``execute_mutation``
    uses) does not.

    Without it, a ``NaN``/``Infinity`` metric wrote the overlay row (
    :meth:`~trellis.stores.classify_overrides_store.ClassifyOverridesStore.put`
    has no opinion on these fields) and only then failed -- when the
    *audit event* was serialized, not the overlay write -- leaving an
    applied rule with no ``CLASSIFY_RULE_APPLIED`` event to show for it.
    Called before any write, so a rejected call leaves no trace at all.

    Ranges mirror what :mod:`trellis.learning.tag_evolution` computes
    each metric from: ``support`` is a document count (``>= 0``),
    ``precision`` is ``support / keyword_documents`` (``[0, 1]``), and
    ``lift`` is ``precision / base_rate`` (``>= 0``, unbounded above).

    Raises:
        ValidationError: a metric is present but not a plain, finite
            number in its valid range (``code="invalid_metric"``).
    """
    support = command_args.get("support")
    if support is not None and (
        isinstance(support, bool) or not isinstance(support, int) or support < 0
    ):
        msg = (
            "classify_rule.apply: 'support' must be a non-negative "
            f"integer or null, got {support!r}"
        )
        raise ValidationError(msg, errors=[msg], code="invalid_metric")

    for name, low, high in (("precision", 0.0, 1.0), ("lift", 0.0, None)):
        value = command_args.get(name)
        if value is None:
            continue
        # bool is an int subclass and NaN/inf are valid `float` instances,
        # so both need an explicit check beyond isinstance(value, float).
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            msg = (
                f"classify_rule.apply: {name!r} must be a finite number or "
                f"null, got {value!r}"
            )
            raise ValidationError(msg, errors=[msg], code="invalid_metric")
        if value < low or (high is not None and value > high):
            bound = f"[{low}, {high}]" if high is not None else f">= {low}"
            msg = f"classify_rule.apply: {name!r} must be {bound}, got {value!r}"
            raise ValidationError(msg, errors=[msg], code="invalid_metric")


_MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH = 200


def _validate_classify_rule_identifier(value: str, *, field: str, code: str) -> None:
    """Refuse a classify-rule identifier that is not already normal.

    Shared by :class:`ClassifyRuleApplyHandler` (``key``, ``target``, and
    ``source_event_id``) and :class:`ClassifyRuleRevokeHandler` (``key``),
    called before either touches the overlay store, so a refusal here
    leaves no row and no event -- the same posture as
    ``_validate_classify_rule_metrics`` above.

    Refuses rather than normalising:
    :class:`~trellis.schemas.classify_overrides.ClassifyOverrideRow` strips
    whitespace on assignment (``str_strip_whitespace=True``), so a key or
    target with leading/trailing whitespace would otherwise be stored
    stripped while ``row_id``/``entity_id``/the audit payload -- all built
    from the caller's original string -- stayed un-stripped, splitting one
    logical identifier across two different strings. A
    control character (:func:`unicodedata.category` category ``"Cc"``,
    which includes NUL and ESC) or a lone UTF-16 surrogate (category
    ``"Cs"``, checked with
    :func:`trellis.core.text_safety.contains_nul_or_lone_surrogate` rather
    than re-deriving the same surrogate-range test) can break a
    downstream consumer outright: a NUL embedded in a Postgres ``jsonb``
    audit payload fails to write at all, and a lone surrogate cannot be
    encoded to UTF-8 at all -- pydantic-core refuses one even earlier,
    raising its own ``pydantic_core.ValidationError`` the moment
    ``ClassifyOverrideRow`` is built, which this handler's callers do not
    catch, so an unchecked surrogate reaches the caller as a handler
    panic (``FAILED``) rather than a clean refusal. A key, target, or
    source_event_id over the length limit risks the same failure on some
    backends' index-row limits, writing the overlay row and then failing
    only when the audit event is emitted.

    Args:
        value: the string to check.
        field: which field this is, used only in the error message.
        code: the :class:`~trellis.errors.ValidationError` code to raise.
            Each caller passes its own (``"invalid_key"``,
            ``"invalid_target"``, ``"invalid_source_event_id"``) rather
            than this function deriving one from ``field``, since the
            type-check each caller already raises for the same field uses
            that same code and the two must not disagree.

    Raises:
        ValidationError: ``value`` has leading/trailing whitespace,
            contains a control character or an unpaired UTF-16 surrogate,
            or exceeds ``_MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH`` characters
            (raised with the caller-supplied ``code``).
    """
    if value != value.strip():
        msg = (
            f"classify_rule: {field} {value!r} has leading or trailing "
            "whitespace -- refused rather than normalised, since the "
            "stored row strips whitespace but the audit trail would not"
        )
        raise ValidationError(msg, errors=[msg], code=code)
    if len(value) > _MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH:
        msg = (
            f"classify_rule: {field} is {len(value)} characters, over the "
            f"{_MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH}-character limit"
        )
        raise ValidationError(msg, errors=[msg], code=code)
    if contains_nul_or_lone_surrogate(value):
        msg = (
            f"classify_rule: {field} {value!r} contains a NUL byte or an "
            "unpaired UTF-16 surrogate"
        )
        raise ValidationError(msg, errors=[msg], code=code)
    for ch in value:
        if unicodedata.category(ch) == "Cc":
            msg = (
                f"classify_rule: {field} {value!r} contains a control "
                f"character ({unicodedata.category(ch)!r})"
            )
            raise ValidationError(msg, errors=[msg], code=code)


def _find_unsafe_jsonb_string(
    obj: Any, path: str = "payload"
) -> tuple[str, str] | None:
    """Walk ``obj`` for the first string a jsonb column cannot store.

    Recurses into ``dict``/``list``/``tuple`` nesting at any depth,
    checking dict *keys* as well as dict and sequence values -- a str
    key is exactly as storable (or not) as a str value, so a key is
    checked the same way rather than assumed safe for being a key; every
    other leaf type (``int``, ``float``, ``bool``, ``None``, a datetime
    already rendered to ``str`` by the caller) is skipped, since a NUL
    or lone surrogate can only ever occur inside a Python ``str``.

    Returns:
        ``(path, value)`` for the first unsafe string found, walking
        each dict item's key before its value, in insertion order, and
        list/tuple items in index order, or ``None`` if no string in the
        structure is unsafe.
    """
    if isinstance(obj, str):
        return (path, obj) if contains_nul_or_lone_surrogate(obj) else None
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and contains_nul_or_lone_surrogate(key):
                return (f"{path} key", key)
            found = _find_unsafe_jsonb_string(value, f"{path}[{key!r}]")
            if found is not None:
                return found
        return None
    if isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            found = _find_unsafe_jsonb_string(value, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _validate_jsonb_safe_payload(payload: dict[str, Any], *, operation: str) -> None:
    """Refuse an audit ``payload`` a Postgres ``jsonb`` column cannot store.

    ``_validate_classify_rule_identifier`` already refuses a NUL or
    control character in ``key``/``target``/``source_event_id`` directly,
    but the payload also carries fields that validator never sees --
    caller identity (``command.metadata``'s ``key_id``/``key_name``) and
    whatever a future field adds -- so this is the backstop that checks
    every string in the payload, dict keys included, at any nesting
    depth, rather than one named field at a time. (A prior row's own
    ``applied_by``/``source_event_id``, which may predate that validator
    or have been written by hand-editing the overlay file, is scrubbed
    by :func:`_scrub_stranded_legacy_field` before it ever reaches this
    probe -- issue #869 -- so this backstop no longer refuses on a value
    this call cannot fix by resubmitting different input.) Called before
    the write it would otherwise follow (``store.put`` in Apply,
    ``store.remove`` in Revoke), so a refusal here leaves the prior state
    -- row present or absent -- exactly as it was, with no row written
    and no event emitted.

    Raises:
        ValidationError: some string value or dict key, at any nesting
            depth, contains a NUL byte or an unpaired UTF-16 surrogate
            (``code="unsafe_audit_payload"``).
    """
    found = _find_unsafe_jsonb_string(payload)
    if found is not None:
        path, value = found
        msg = (
            f"classify_rule.{operation}: audit payload {path} contains a "
            "NUL byte or an unpaired surrogate that a jsonb column cannot "
            f"store: {value!r}"
        )
        raise ValidationError(msg, errors=[msg], code="unsafe_audit_payload")


def _scrub_stranded_legacy_field(value: str) -> str:
    """Replace a NUL byte or lone surrogate in a value read off an existing
    overlay row, rather than refusing the payload it rides in on.

    ``existing.applied_by``/``existing.source_event_id`` may predate the
    identifier validator or come from a hand-edited overlay file. The
    caller cannot fix such a value by resubmitting, so refusing the audit
    payload over it would leave the row impossible to revoke or re-apply.
    """
    return replace_nul_and_lone_surrogates(value)


_CLASSIFY_RULE_IDENTITY_FIELD_CODES = (
    ("requested_by_claim", "invalid_requested_by_claim"),
    ("key_id", "invalid_key_id"),
    ("key_name", "invalid_key_name"),
)


def _validate_classify_rule_identity_fields(
    *, requested_by_claim: Any, key_id: Any, key_name: Any, operation: str
) -> None:
    """Refuse ``requested_by_claim``/``key_id``/``key_name`` unless str|None.

    Shared by :class:`ClassifyRuleApplyHandler` and
    :class:`ClassifyRuleRevokeHandler`, called before either builds or
    removes a row. No shipped surface sends anything else today --
    ``requested_by_claim`` comes from ``command.args``, which no surface
    sets, and REST/CLI only ever stamp ``metadata``'s ``key_id``/
    ``key_name`` as ``str | None`` -- but nothing upstream of this
    handler enforces that, and a non-str value here would otherwise
    reach the audit payload unchecked: a ``float("nan")`` claim passes
    straight through ``_validate_jsonb_safe_payload`` (it is not a
    ``str``) and only ``json.dumps(..., allow_nan=False)`` would catch
    it, while a ``set`` (or anything else ``json.dumps`` cannot encode
    at all) raises an uncaught ``TypeError`` that neither probe below
    catches, turning a bad claim into a handler panic rather than a
    clean refusal.

    Args:
        requested_by_claim: ``command.args.get("requested_by_claim")``.
        key_id: ``command.metadata.get("key_id")``.
        key_name: ``command.metadata.get("key_name")``.
        operation: ``"apply"`` or ``"revoke"``, for the error message.

    Raises:
        ValidationError: one of the three is neither ``str`` nor
            ``None`` (``code="invalid_requested_by_claim"``,
            ``"invalid_key_id"`` or ``"invalid_key_name"``, matching
            whichever field failed first).
    """
    values = (requested_by_claim, key_id, key_name)
    for value, (field, code) in zip(
        values, _CLASSIFY_RULE_IDENTITY_FIELD_CODES, strict=True
    ):
        if value is not None and not isinstance(value, str):
            msg = (
                f"classify_rule.{operation}: {field} must be a string or "
                f"null, got {type(value).__name__}"
            )
            raise ValidationError(msg, errors=[msg], code=code)


def _validate_json_serializable_payload(
    payload: dict[str, Any], *, operation: str
) -> None:
    """Refuse an audit ``payload`` ``json.dumps(..., allow_nan=False)`` rejects.

    Shared by :class:`ClassifyRuleApplyHandler` and
    :class:`ClassifyRuleRevokeHandler`, called before the write it would
    otherwise follow (``store.put`` in Apply, ``store.remove`` in
    Revoke). On Apply, ``_validate_classify_rule_metrics`` already
    refuses a non-finite support/precision/lift, so this should never
    fire there in practice -- it is the backstop for whatever the
    payload gains next that isn't a plain, finite number. Revoke carries
    no metrics of its own, but this is still the only probe that would
    catch one reaching its payload some other way (e.g. a legacy row's
    own fields). The actual event log backend serialises with
    ``json.dumps(..., allow_nan=False)`` regardless of which store backs
    it (SQLite and Postgres both), and a failure there, discovered only
    after the write already ran, would leave a rule applied or revoked
    with no event to show for it.

    Raises:
        ValidationError: ``json.dumps(payload, allow_nan=False)`` raises
            ``ValueError`` -- always for a non-finite float, which is
            the only shape reaching this point that it refuses
            (``code="invalid_metric"``).
    """
    try:
        json.dumps(payload, allow_nan=False)
    except ValueError as exc:
        # json.dumps(..., allow_nan=False) always raises ValueError(msg)
        # with a single string argument ("Out of range float values are
        # not JSON compliant: nan"), never a wrapped driver/library
        # exception, so args[0] is exactly that message -- a
        # structured-field read, not a bare str(exc) stringification of
        # the caught exception.
        detail = exc.args[0] if exc.args else "not serializable"
        msg = f"classify_rule.{operation}: audit payload is not serializable: {detail}"
        raise ValidationError(msg, errors=[msg], code="invalid_metric") from exc


class ClassifyRuleApplyHandler:
    """Apply one mined tag/domain candidate into the operator overlay.

    Persists a :class:`~trellis.schemas.classify_overrides.ClassifyOverrideRow`
    via :class:`~trellis.stores.classify_overrides_store.ClassifyOverridesStore`
    -- the governed, audited analogue of hand-pasting YAML into
    ``classify.domain_keywords`` / ``classify.domain_aliases`` in
    ``config.yaml``. Every real reader
    (:meth:`~trellis.stores.registry.StoreRegistry.build_ingestion_pipeline`,
    :meth:`~trellis.stores.registry.StoreRegistry.domain_keyword_map`,
    :meth:`~trellis.stores.registry.StoreRegistry.domain_alias_map`) builds
    a fresh overlay-aware map on every call, so an applied rule needs no
    restart to take effect.

    Re-applying an already-applied ``(kind, key)`` with a *different*
    ``target`` overwrites the row in place -- CLAUDE.md: "a wrong rule
    hides content" -- which is why this operation is classified
    ``DESTRUCTIVE`` in ``immutable_core.py``, the same reasoning as
    ``settings.update``.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(  # noqa: PLR0912, PLR0915
        self, command: Command
    ) -> tuple[str | None, str]:
        from trellis.classify.factory import (  # noqa: PLC0415
            config_sourced_alias_target,
            effective_domain_aliases,
            reject_reserved_rule_target,
        )
        from trellis.schemas.classify_overrides import (  # noqa: PLC0415
            ClassifyOverrideRow,
        )
        from trellis.stores.classify_overrides_store import (  # noqa: PLC0415
            ClassifyOverridesStore,
            resolve_classify_overrides_path,
        )

        kind = command.args["kind"]
        key = command.args["key"]
        target = command.args["target"]
        source_event_id = command.args["source_event_id"]

        if kind not in ("tag_keyword", "domain_alias"):
            msg = f"Unknown classify-rule kind: {kind!r}"
            raise ValidationError(msg, errors=[msg], code="unknown_kind")
        if not key or not isinstance(key, str):
            msg = "classify_rule.apply requires a non-empty string 'key'"
            raise ValidationError(msg, errors=[msg], code="invalid_key")
        _validate_classify_rule_identifier(key, field="key", code="invalid_key")
        if kind == "tag_keyword" and len(key) < MIN_TAG_KEYWORD_LENGTH:
            # The miner itself never proposes a token shorter than this
            # (``_TOKEN_RE`` in tag_evolution.py), because a short keyword
            # matches far too much under the classifier's substring
            # semantics and defeats the two-distinct-keyword minimum that
            # otherwise keeps one bad promotion from assigning a domain on
            # its own. apply-rule does not route through that regex, so
            # nothing else stops a one-letter key submitted by hand.
            msg = (
                f"classify_rule.apply: tag_keyword key {key!r} is shorter "
                f"than the {MIN_TAG_KEYWORD_LENGTH}-character minimum "
                "-- a short keyword matches far too much under the "
                "classifier's substring semantics and would defeat the "
                "two-hit minimum on its own."
            )
            raise ValidationError(msg, errors=[msg], code="tag_keyword_too_short")
        if not target or not isinstance(target, str):
            msg = "classify_rule.apply requires a non-empty string 'target'"
            raise ValidationError(msg, errors=[msg], code="invalid_target")
        _validate_classify_rule_identifier(
            target, field="target", code="invalid_target"
        )
        if not source_event_id or not isinstance(source_event_id, str):
            msg = (
                "classify_rule.apply requires a non-empty string "
                "'source_event_id' so the applied rule can be traced back "
                "to the candidate it was mined from"
            )
            raise ValidationError(msg, errors=[msg], code="invalid_source_event_id")
        _validate_classify_rule_identifier(
            source_event_id, field="source_event_id", code="invalid_source_event_id"
        )
        _validate_classify_rule_metrics(command.args)
        # Before any row is built: a non-str/non-None requested_by_claim
        # or metadata key_id/key_name would otherwise reach the audit
        # payload unchecked (see _validate_classify_rule_identity_fields's
        # own docstring for exactly what the two probes further down
        # would and would not have caught on their own).
        _validate_classify_rule_identity_fields(
            requested_by_claim=command.args.get("requested_by_claim"),
            key_id=command.metadata.get("key_id"),
            key_name=command.metadata.get("key_name"),
            operation="apply",
        )

        row_id = f"{kind}:{key}"
        # Defence in depth, same posture as SettingsUpdateHandler: this
        # handler is only ever registered via create_classify_handlers,
        # reachable only through the admin route/CLI, which both stamp
        # target_type/target_id correctly -- but a mistargeted Command
        # reaching here by some future wiring mistake must still refuse
        # rather than write.
        if command.target_type != "classify_rule" or command.target_id != row_id:
            msg = (
                "classify_rule.apply requires target_type='classify_rule' "
                f"and target_id={row_id!r}; got "
                f"target_type={command.target_type!r}, "
                f"target_id={command.target_id!r}"
            )
            raise ValidationError(msg, errors=[msg], code="untargeted_command")

        # A reserved policy namespace (sensitivity, lifecycle, ...) is never
        # a legitimate domain value -- checked before the row ever reaches
        # the overlay file, not discovered only once a later reader tries
        # to build a merged domain map from it and fails. For a
        # domain_alias row, the key is itself a domain name once merged
        # (the alias side of ``alias -> canonical``), so it is checked
        # too -- without this, ``sensitivity -> privacy`` was accepted
        # because only the clean target was ever looked at.
        try:
            reject_reserved_rule_target(target)
            if kind == "domain_alias":
                reject_reserved_rule_target(key, what="key", casefold=True)
        except ValueError as exc:
            # reject_reserved_rule_target always raises ValueError(msg) with
            # a single string it composed itself, never a wrapped
            # driver/library exception, so args[0] is exactly that message
            # -- a structured-field read, not a bare str(exc) stringification
            # of the caught exception (same convention as validate_value
            # above).
            msg = exc.args[0] if exc.args else "reserved namespace target"
            raise ValidationError(
                msg, errors=[msg], code="reserved_namespace_target"
            ) from exc

        path = resolve_classify_overrides_path(self._registry.stores_dir)
        if path is None:
            # Same precedent as SettingsUpdateHandler: no stores_dir means
            # no file-backed overlay store exists to write to -- not a
            # caller-input problem, so this is left for the executor's
            # handler-panic path rather than raised as ValidationError.
            msg = "stores_dir must be set on registry to use ClassifyOverridesStore"
            raise ValueError(msg)
        store = ClassifyOverridesStore(path)

        classify_config = self._registry.classify_config
        if kind == "domain_alias":
            # Validate the *prospective* merged alias map -- this key's
            # new target folded into every other already-applied alias
            # row -- before it ever reaches the overlay file. A self-map
            # (``a -> a``) or a chain this apply would introduce (this
            # key plus an existing row forming ``a -> b -> c``) must be
            # refused here; by the time a later reader calls
            # ``effective_domain_aliases`` on the written file, the bad
            # row is already live.
            prospective = dict(store.domain_aliases_map())
            prospective[key] = target
            try:
                effective_domain_aliases(classify_config, overlay_aliases=prospective)
            except ValueError as exc:
                # effective_domain_aliases always raises ValueError(msg) with
                # a single string it composed itself, never a wrapped
                # driver/library exception, so args[0] is exactly that
                # message -- a structured-field read, not a bare str(exc)
                # stringification of the caught exception.
                msg = exc.args[0] if exc.args else "invalid alias rule"
                raise ValidationError(
                    msg, errors=[msg], code="invalid_alias_rule"
                ) from exc

        # Captured before the write so the audit event can record exactly
        # what this apply replaces -- both the overlay row it overwrites
        # (re-applying an already-applied key) and, for a domain_alias,
        # the config.yaml value this key's overlay precedence now shadows.
        existing = store.get(kind, key)
        config_target = (
            config_sourced_alias_target(classify_config, key)
            if kind == "domain_alias"
            else None
        )

        # Probed before use, not just before the write: ``applied_by``
        # below can resolve to a caller-supplied metadata string that
        # ``ClassifyOverrideRow`` itself would reject with its own
        # ``pydantic_core.ValidationError`` (unable to encode a lone
        # surrogate) rather than this handler's ``ValidationError`` --
        # the row construction a few lines down happens before the
        # payload-level ``_validate_jsonb_safe_payload`` call ever runs,
        # so a NUL/surrogate confined to ``metadata`` needs its own,
        # earlier check to get the same ``unsafe_audit_payload`` code and
        # leave no row written.
        _validate_jsonb_safe_payload(
            {
                "key_id": command.metadata.get("key_id"),
                "key_name": command.metadata.get("key_name"),
            },
            operation="apply",
        )

        # Structured caller identity, when the surface supplies one
        # (REST: the authenticated key's id/name; CLI: the OS user),
        # beats the constant surface label in ``command.requested_by``
        # (e.g. ``"api:classify.apply-rule"``) for a field meant to
        # answer "who applied this", not "through what entry point".
        applied_by = (
            command.metadata.get("key_id")
            or command.metadata.get("key_name")
            or command.requested_by
        )
        row = ClassifyOverrideRow(
            kind=kind,
            key=key,
            target=target,
            applied_at=datetime.now(UTC),
            applied_by=applied_by,
            source_event_id=source_event_id,
        )
        payload: dict[str, Any] = {
            "kind": kind,
            "key": key,
            "target": target,
            "support": command.args.get("support"),
            "precision": command.args.get("precision"),
            "lift": command.args.get("lift"),
            "applied_by": applied_by,
            "key_id": command.metadata.get("key_id"),
            "key_name": command.metadata.get("key_name"),
            "source_event_id": source_event_id,
            "prior_target": existing.target if existing is not None else None,
            "prior_applied_by": (
                _scrub_stranded_legacy_field(existing.applied_by)
                if existing is not None
                else None
            ),
            "prior_applied_at": (
                existing.applied_at.isoformat() if existing is not None else None
            ),
            "prior_source_event_id": (
                _scrub_stranded_legacy_field(existing.source_event_id)
                if existing is not None
                else None
            ),
            "config_target": config_target,
        }
        requested_by_claim = command.args.get("requested_by_claim")
        if requested_by_claim is not None:
            payload["requested_by_claim"] = requested_by_claim

        # Built and probed BEFORE the overlay write, in two independent
        # passes over the same payload (shared with Revoke -- see each
        # helper's own docstring): the first catches a non-finite float
        # that ``_validate_classify_rule_metrics`` should already have
        # refused, so it should never fire in practice, but it is the
        # backstop for whatever this payload gains next that isn't a
        # plain, finite number. Discovered only after ``store.put``
        # already wrote the row, either failure would apply a rule with
        # no ``CLASSIFY_RULE_APPLIED`` event to show for it.
        _validate_json_serializable_payload(payload, operation="apply")

        # A second, independent probe on the same built payload: the
        # json-serializable check above catches a non-finite float, but
        # a NUL byte or lone surrogate round-trips through json.dumps
        # just fine and only fails once a Postgres jsonb column refuses
        # to store it.
        _validate_jsonb_safe_payload(payload, operation="apply")

        try:
            store.put(row)
        except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
            raise ValidationError(
                exc.message, errors=[exc.message], code=exc.code.lower()
            ) from exc

        self._registry.operational.event_log.emit(
            EventType.CLASSIFY_RULE_APPLIED,
            source="mutation_executor",
            entity_id=row_id,
            payload=payload,
        )
        return row_id, f"Applied {kind} rule {key!r} -> {target!r}"


class ClassifyRuleRevokeHandler:
    """Revoke one applied rule from the operator overlay.

    Refuses -- as a :class:`~trellis.errors.ValidationError`, which the
    executor turns into a plain ``REJECTED`` result (REST: 400) rather
    than a panic -- when ``(kind, key)`` has no overlay row. The message
    names ``config.yaml`` specifically when the rule exists there instead:
    such a rule was never written by this store and cannot be un-applied
    through it. A genuinely unknown ``(kind, key)`` gets a different,
    equally refused message, so a typo is never told "edit config.yaml".
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(  # noqa: PLR0912, PLR0915
        self, command: Command
    ) -> tuple[str | None, str]:
        from trellis.classify.factory import (  # noqa: PLC0415
            config_sourced_alias_target,
            config_sourced_tag_keyword_domains,
            effective_domain_aliases,
            is_config_sourced_alias,
        )
        from trellis.stores.classify_overrides_store import (  # noqa: PLC0415
            ClassifyOverridesStore,
            resolve_classify_overrides_path,
        )

        kind = command.args["kind"]
        key = command.args["key"]

        if kind not in ("tag_keyword", "domain_alias"):
            msg = f"Unknown classify-rule kind: {kind!r}"
            raise ValidationError(msg, errors=[msg], code="unknown_kind")
        if not key or not isinstance(key, str):
            msg = "classify_rule.revoke requires a non-empty string 'key'"
            raise ValidationError(msg, errors=[msg], code="invalid_key")
        _validate_classify_rule_identifier(key, field="key", code="invalid_key")
        # Before any row is removed: the same type guard Apply runs,
        # same reason -- see _validate_classify_rule_identity_fields's
        # own docstring.
        _validate_classify_rule_identity_fields(
            requested_by_claim=command.args.get("requested_by_claim"),
            key_id=command.metadata.get("key_id"),
            key_name=command.metadata.get("key_name"),
            operation="revoke",
        )

        row_id = f"{kind}:{key}"
        if command.target_type != "classify_rule" or command.target_id != row_id:
            msg = (
                "classify_rule.revoke requires target_type='classify_rule' "
                f"and target_id={row_id!r}; got "
                f"target_type={command.target_type!r}, "
                f"target_id={command.target_id!r}"
            )
            raise ValidationError(msg, errors=[msg], code="untargeted_command")

        path = resolve_classify_overrides_path(self._registry.stores_dir)
        if path is None:
            msg = "stores_dir must be set on registry to use ClassifyOverridesStore"
            raise ValueError(msg)
        store = ClassifyOverridesStore(path)

        existing = store.get(kind, key)
        if existing is None:
            if store.is_degraded:
                # A degraded read cannot tell "never applied" apart from
                # "the row that WAS applied failed to parse" -- defer to
                # the write path's own refusal (which answers 409 with
                # the real ``store_degradation``) rather than guessing a
                # 400 "no such rule" from a read that might be wrong.
                try:
                    store.remove(kind, key)
                except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
                    raise ValidationError(
                        exc.message, errors=[exc.message], code=exc.code.lower()
                    ) from exc
                # remove() refuses before ever reaching a return on a
                # degraded store, so this is unreachable in practice --
                # kept only so a future change to that refusal order
                # fails loudly here instead of silently answering 400.
                msg = (
                    f"No applied {kind} rule found for key {key!r}, but the "
                    "classify-overrides store did not refuse the write."
                )
                raise ValueError(msg)

            classify_config = self._registry.classify_config
            if kind == "tag_keyword":
                config_domains = config_sourced_tag_keyword_domains(
                    classify_config, key
                )
                config_sourced = bool(config_domains)
                where = (
                    f"config.yaml (domain(s): {', '.join(config_domains)})"
                    if config_sourced
                    else None
                )
            else:
                config_sourced = is_config_sourced_alias(classify_config, key)
                where = "config.yaml" if config_sourced else None

            if config_sourced:
                msg = (
                    f"{kind} rule {key!r} comes from {where}, not the "
                    "operator overlay. Nothing to revoke here -- edit "
                    "config.yaml directly to change or remove it."
                )
                raise ValidationError(msg, errors=[msg], code="not_overlay_sourced")
            msg = f"No applied {kind} rule found for key {key!r}."
            raise ValidationError(msg, errors=[msg], code="no_such_rule")

        if kind == "domain_alias":
            # Revoking a row can re-expose a chain or self-map that was
            # only hidden because this overlay entry shadowed a
            # config.yaml alias -- e.g. config.yaml has ``fin -> finance``
            # and ``finance -> treasury`` (already a chain on its own,
            # but never merged because nothing read it that way until an
            # overlay row pointed at one end of it), or an overlay applies
            # ``fin -> money`` then ``finance -> treasury``, each fine
            # alone, and revoking ``fin`` exposes ``finance -> treasury``
            # chained with config's own ``fin -> finance``. Validate the
            # prospective merged map -- this store's rows minus the key
            # being revoked -- the same check Apply makes before it
            # writes, so the chain is refused here rather than only
            # discovered the next time a reader calls
            # ``effective_domain_aliases``.
            prospective = {
                k: v for k, v in store.domain_aliases_map().items() if k != key
            }
            try:
                effective_domain_aliases(
                    self._registry.classify_config, overlay_aliases=prospective
                )
            except ValueError as exc:
                # effective_domain_aliases always raises ValueError(msg)
                # with a single string it composed itself, never a
                # wrapped driver/library exception, so args[0] is exactly
                # that message -- same structured-field read Apply uses.
                msg = exc.args[0] if exc.args else "invalid alias rule"
                raise ValidationError(
                    msg, errors=[msg], code="invalid_alias_rule"
                ) from exc

        config_target = (
            config_sourced_alias_target(self._registry.classify_config, key)
            if kind == "domain_alias"
            else None
        )
        payload: dict[str, Any] = {
            "kind": kind,
            "key": key,
            "target": existing.target,
            "revoked_by": command.requested_by,
            "key_id": command.metadata.get("key_id"),
            "key_name": command.metadata.get("key_name"),
            "removed_applied_by": _scrub_stranded_legacy_field(existing.applied_by),
            "removed_applied_at": existing.applied_at.isoformat(),
            "removed_source_event_id": _scrub_stranded_legacy_field(
                existing.source_event_id
            ),
            "config_target": config_target,
        }
        requested_by_claim = command.args.get("requested_by_claim")
        if requested_by_claim is not None:
            payload["requested_by_claim"] = requested_by_claim

        # Built and probed BEFORE the store write, with the same two
        # probes Apply runs, in the same order. A NUL byte or lone
        # surrogate can still reach this payload inside a str the type
        # guard above lets through (``command.metadata``'s identity
        # fields) -- ``existing``'s own ``source_event_id``/``applied_by``
        # are pre-scrubbed above instead, since those may have been
        # written by an earlier build or by hand-editing the overlay
        # file, a value this call cannot fix by resubmitting different
        # input (issue #869). No current field can carry a
        # non-finite float here; the serializability probe is a
        # backstop for whatever this payload gains next. Discovering
        # either failure only after
        # ``store.remove`` already ran would revoke the rule with no
        # ``CLASSIFY_RULE_REVOKED`` event to show for it; probing first
        # leaves the row in place on a refusal.
        _validate_json_serializable_payload(payload, operation="revoke")
        _validate_jsonb_safe_payload(payload, operation="revoke")

        try:
            # ``existing`` above is non-None, so this row is live in a
            # non-degraded store -- the only ways to reach this point --
            # and ``remove`` refuses rather than returning ``False`` on a
            # degraded/stale store (caught below). ``removed`` is
            # therefore always ``True`` here; no variable is kept for it.
            store.remove(kind, key)
        except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
            raise ValidationError(
                exc.message, errors=[exc.message], code=exc.code.lower()
            ) from exc

        self._registry.operational.event_log.emit(
            EventType.CLASSIFY_RULE_REVOKED,
            source="mutation_executor",
            entity_id=row_id,
            payload=payload,
        )
        return row_id, f"{kind} rule {key!r} revoked"


class EntityCreateHandler:
    """Create entity node via graph store, return node_id.

    Supports optional ``node_role`` and ``generation_spec`` command args to
    create structural or curated nodes. Defaults to a semantic node when
    omitted. The graph store rejects invalid combinations (e.g., curated
    without a generation_spec) via ``validate_node_role_args``.

    ``document_ids`` follows the same omission semantics as
    :class:`EntityUpdateHandler`: an omitted field carries the stored link
    forward, an explicit value replaces it.

    **An existing precedent's id is refused unless it is current.**
    ENTITY_CREATE on any other existing id is a legitimate upsert (it
    opens a new SCD-2 version, e.g. re-extracting an entity), and
    re-approving a learning candidate whose target is already a *current*
    precedent is equally legitimate (``submit_learning_promotion`` writes
    it this way; ``_precedent_reapproval_refusal`` already lets a current
    precedent's re-approval through before this handler ever runs). What
    is refused is an unconditional property replace that would drop a
    *non-current* ``lifecycle`` — a demoted precedent would come back
    ``"current"`` with no reason recorded and no ``PRECEDENT_REINSTATED``
    event, the same silent-reinstate shape :class:`EntityUpdateHandler`'s
    guards close on the update path. A ``entity_type`` other than
    ``"precedent"`` over an existing precedent is refused outright,
    whatever its lifecycle state, mirroring
    :class:`EntityUpdateHandler`'s retype guard: it would otherwise
    retype the node away from precedent in the same call that replaces
    its properties, reaching the same end state as the two-step bypass
    that guard closes.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        props: dict[str, Any] = dict(command.args.get("properties", {}))
        props["name"] = command.args["name"]

        # Use caller-supplied entity_id if provided, otherwise auto-generate ULID
        caller_id = command.args.get("entity_id")
        node_role = command.args.get("node_role", "semantic")
        generation_spec = command.args.get("generation_spec")

        # One read serves both the precedent guard below and
        # ``_resolve_document_ids``'s stored-link lookup, so a caller_id
        # that already exists costs a single extra read either way.
        existing_node = (
            self._registry.knowledge.graph_store.get_node(caller_id)
            if caller_id is not None
            else None
        )
        if existing_node is not None and existing_node["node_type"] == "precedent":
            supplied_entity_type = command.args["entity_type"]
            if supplied_entity_type != "precedent":
                msg = (
                    f"entity.create refuses to retype existing precedent "
                    f"{caller_id} to {supplied_entity_type!r}: a later "
                    "entity.update naming properties.lifecycle under that "
                    "non-precedent node_type would then slip past the guard "
                    "that refuses it on a precedent; use entity.update for "
                    "a non-retyping property edit"
                )
                raise ValidationError(
                    msg,
                    errors=[msg],
                    code="entity_create_retypes_existing_precedent",
                )
            if _current_lifecycle_state(existing_node) != CURRENT_STATE:
                msg = (
                    f"entity.create refuses to upsert over existing "
                    f"precedent {caller_id}: an unconditional property "
                    "replace would drop its lifecycle (a demoted precedent "
                    "would come back 'current' with no reason recorded and "
                    "no PRECEDENT_REINSTATED event); use "
                    "precedent.reinstate for that, or entity.update for a "
                    "non-lifecycle property edit"
                )
                raise ValidationError(
                    msg, errors=[msg], code="entity_create_over_existing_precedent"
                )

        # ``document_ids`` is the first-class graph↔document link (Phase 4 of
        # ADR planes-and-substrates). Threading it here gives the pointer a
        # governed write path — the pointer-not-prose invariant depends on a
        # created entity being able to carry an ``evidence_ref`` document
        # pointer without smuggling it through metadata.
        #
        # ENTITY_CREATE on an existing id is an upsert (it opens a new SCD-2
        # version), so an *omitted* ``document_ids`` has to carry the stored
        # link forward the way EntityUpdateHandler does. Passing ``None``
        # straight through would write NULL and silently destroy a link some
        # other writer established — e.g. re-extracting an entity that
        # ``save_knowledge`` had linked to its document. Omission means
        # "leave it alone"; an explicit value still replaces.
        document_ids = self._resolve_document_ids(command, existing_node)
        node_id = self._registry.knowledge.graph_store.upsert_node(
            node_id=caller_id,
            node_type=command.args["entity_type"],
            properties=props,
            node_role=node_role,
            generation_spec=generation_spec,
            document_ids=document_ids,
        )
        _bind_entity_name_alias(
            self._registry.knowledge.graph_store,
            entity_id=node_id,
            name=command.args["name"],
        )

        self._registry.operational.event_log.emit(
            EventType.ENTITY_CREATED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=command.args["entity_type"],
            payload={
                "name": command.args["name"],
                "node_role": node_role,
                "document_ids": document_ids or [],
            },
        )
        return node_id, f"Entity created: {command.args['name']}"

    def _resolve_document_ids(
        self, command: Command, existing_node: dict[str, Any] | None
    ) -> list[str] | None:
        """Command's ``document_ids``, else the stored link, else ``None``.

        ``existing_node`` is whatever ``handle`` already read for the
        precedent guard (``None`` when ``entity_id`` was omitted or named
        no existing node) — reused here rather than re-read, so a caller_id
        that already exists still costs one store read, not two.
        """
        if "document_ids" in command.args:
            supplied = command.args["document_ids"]
            return cast("list[str] | None", supplied)
        if existing_node is None:
            return None
        # ``get_node`` returns a (possibly empty) list; normalise empty to
        # ``None`` so ``validate_document_ids`` sees a clean value.
        return cast("list[str] | None", existing_node.get("document_ids") or None)


class EntityUpdateHandler:
    """Update an existing entity node, emitting ``ENTITY_UPDATED``.

    Wires :data:`Operation.ENTITY_UPDATE` into the governed pipeline. The
    enum verb shipped with no handler, so the executor rejected every
    ``entity.update`` command with "No handler registered for:
    entity.update" — this closes that gap.

    **SCD-2 discipline.** The update is never an in-place edit. The new
    version is written by a compare-and-set on the version read
    (``_rewrite_current_node``), which closes the old version (``valid_to``
    is set) and inserts a fresh version row; ``get_node_history`` is the
    audit trail. A node purged between the read and the write is refused
    like a missing one, not re-created; a version replaced by another
    writer in between is re-read and the update re-applied. ``node_role``
    and ``generation_spec`` are immutable across versions (the graph store
    enforces role immutability via ``check_node_role_immutable``), so they
    are carried forward from the existing version — the same discipline
    :class:`LabelAddHandler` uses.

    **Partial update.** ``properties`` is *merged* into the existing bag
    (not replaced) so a caller can attach a single field — e.g. an
    ``evidence_ref`` pointer — without resending the whole node. Likewise
    ``document_ids`` is carried forward when the caller omits it: absent
    means "leave the graph↔document link untouched", present means "replace
    it". This keeps a version bump from silently dropping the
    pointer-not-prose link.

    Idempotency: a same-payload re-submission still creates a new version
    row (SCD-2 has no same-value short-circuit at this layer); use
    ``Command.idempotency_key`` when at-most-once semantics are required.

    **Precedents are the one exception to the property merge.** A write
    naming ``properties.lifecycle`` on a node whose current ``node_type``
    is ``"precedent"``, *or* whose ``entity_type`` arg names ``"precedent"``
    in the same call, is refused rather than merged: this used to be the
    only way to undo a ``precedent.demote`` (silently, with no reason
    recorded and no vector re-sync), which is exactly the gap
    :class:`PrecedentReinstateHandler` closes. Every other node's
    ``lifecycle`` key is an ordinary property this handler merges like any
    other — the save-surfaces' supersession stamp (``mcp.supersession``)
    still writes it this way.

    **A precedent's ``node_type`` may not be changed away from
    ``"precedent"`` at all**, lifecycle write or not: a two-step bypass
    (update 1: ``entity_type="concept"`` with no lifecycle payload, so the
    guard above never fires because nothing names ``lifecycle``; update 2:
    ``entity_type="precedent"`` plus ``properties.lifecycle.state="current"``,
    read against the *now-"concept"* stored node) would otherwise reinstate
    a demoted precedent with no ``PRECEDENT_REINSTATED`` event and no
    reason recorded. Refusing step 1 outright closes it before step 2 is
    ever reached; the ``entity_type == "precedent"`` keying above is the
    second, independent guard for the same end state.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        entity_id = command.args["entity_id"]
        store = self._registry.knowledge.graph_store
        raw_properties = command.args.get("properties") or {}
        # ``props.update(...)`` below accepts a dict OR an iterable of
        # ``(key, value)`` pairs, and callers use both (MCP
        # ``execute_mutation`` and REST ``/commands/batch`` both carry
        # ``args`` as loosely-typed JSON, so a list-of-pairs payload reaches
        # here unchanged). Normalising to a dict *here*, before either
        # precedent guard reads it, means ``touches_lifecycle`` below sees
        # the same key regardless of which shape the caller sent --
        # otherwise a pairs-form ``[["lifecycle", {...}]]`` skipped the
        # ``isinstance(..., dict)`` check entirely and reinstated a demoted
        # precedent with no ``PRECEDENT_REINSTATED`` event.
        supplied_properties: dict[str, Any] = dict(raw_properties)
        supplied_entity_type = command.args.get("entity_type")
        touches_lifecycle = LIFECYCLE_KEY in supplied_properties

        # Read the existing node only when one of the two precedent guards
        # below could actually need it, so the common case -- a property
        # merge that names neither ``entity_type`` nor ``lifecycle`` --
        # stays the single read ``_rewrite_current_node`` does internally.
        existing_node = (
            store.get_node(entity_id)
            if (supplied_entity_type or touches_lifecycle)
            else None
        )
        is_existing_precedent = (
            existing_node is not None and existing_node["node_type"] == "precedent"
        )

        if (
            is_existing_precedent
            and supplied_entity_type
            and supplied_entity_type != "precedent"
        ):
            msg = (
                f"entity.update refuses to change {entity_id}'s entity_type "
                f"away from 'precedent' (to {supplied_entity_type!r}): a "
                "later update could then write properties.lifecycle under "
                "that non-precedent node_type and slip past the guard that "
                "refuses it on a precedent"
            )
            raise ValidationError(
                msg, errors=[msg], code="precedent_entity_type_change_refused"
            )

        if touches_lifecycle and (
            is_existing_precedent or supplied_entity_type == "precedent"
        ):
            msg = (
                f"entity.update refuses a write to properties.{LIFECYCLE_KEY} "
                f"on precedent {entity_id}: use precedent.demote or "
                "precedent.reinstate instead, which keep the audit trail "
                "and the vector row in sync"
            )
            raise ValidationError(
                msg, errors=[msg], code="precedent_lifecycle_via_entity_update"
            )

        def update(existing: dict[str, Any]) -> dict[str, Any]:
            # Partial update: merge caller-supplied properties onto the
            # existing bag, then let an explicit ``name`` win.
            props: dict[str, Any] = dict(existing["properties"])
            props.update(supplied_properties)
            if "name" in command.args:
                props["name"] = command.args["name"]
            changes: dict[str, Any] = {"properties": props}
            # ``node_type`` is mutable across versions but carries forward
            # when the caller omits ``entity_type``.
            if command.args.get("entity_type"):
                changes["node_type"] = command.args["entity_type"]
            # The document link carries forward unless the caller supplies a
            # replacement.
            if "document_ids" in command.args:
                changes["document_ids"] = command.args["document_ids"]
            return changes

        written = _rewrite_current_node(store, entity_id, update)
        if written is None:
            # Input to fix, not a store failure: refused like a label on a
            # missing node (REJECTED, ``code`` the audit reason).
            msg = f"Node not found: {entity_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        # Bound and emitted only after the write succeeded, so a refused
        # update leaves neither an alias nor an event.
        final_name = written["properties"].get("name")
        name_was_supplied = "name" in command.args or "name" in supplied_properties
        if name_was_supplied:
            _bind_entity_name_alias(
                store,
                entity_id=entity_id,
                name=final_name,
            )

        self._registry.operational.event_log.emit(
            EventType.ENTITY_UPDATED,
            source="mutation_executor",
            entity_id=entity_id,
            entity_type=written["node_type"],
            payload={
                "name": final_name,
                "node_role": written["node_role"],
                "document_ids": written["document_ids"] or [],
            },
        )
        return entity_id, f"Entity updated: {entity_id}"


class AliasUpsertHandler:
    """Bind or explicitly rebind an alias through the governed pipeline."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        store = self._registry.knowledge.graph_store
        kwargs = {
            "raw_name": command.args.get("raw_name"),
            "match_confidence": command.args.get("match_confidence", 1.0),
            "is_primary": command.args.get("is_primary", False),
        }
        try:
            if command.args.get("if_absent", False):
                result = store.bind_alias_if_absent(
                    command.args["entity_id"],
                    command.args["source_system"],
                    command.args["raw_id"],
                    stale_owner_name_key=command.args.get("stale_owner_name_key"),
                    **kwargs,
                )
                return result.alias_id, f"Alias bind outcome: {result.status.value}"

            alias_id = store.upsert_alias(
                command.args["entity_id"],
                command.args["source_system"],
                command.args["raw_id"],
                **kwargs,
            )
        except Exception as exc:
            msg = "Alias store operation failed"
            raise StoreError(msg, store="graph") from exc
        return alias_id, "Alias upserted"


class LinkCreateHandler:
    """Validate both endpoints exist, then create edge via graph store.

    Pre-flight FK validation runs at the start of :meth:`handle` (before any
    side effect) so orphan edges can't be created in the first place. The
    legacy CLI ``graph-health`` command surfaces orphans post-hoc as a
    safety net; this handler closes the door at ingest time.

    The check resolves each endpoint via :meth:`_resolve_node` (direct
    ``get_node`` lookup, then property-based fallback on ``entity_id``).
    On miss, raises :class:`trellis.errors.ValidationError` with a message
    that names which side (source / target / both) failed and which IDs
    were attempted — the executor turns that into a ``MUTATION_REJECTED``
    event and a ``CommandStatus.FAILED`` result, so ``LINK_CREATED`` is
    never emitted for a dangling edge.

    Escape hatch: pass ``allow_dangling=True`` in ``command.args`` to skip
    FK validation. This is for bootstrap / edge-before-node ingest paths
    (e.g. extractors that emit edges in dependency order before their
    referenced nodes exist). Default is ``False`` — strict.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def _resolve_node(self, node_id: str) -> str | None:
        """Resolve a node ID, falling back to property-based lookup.

        Tries:
          1. Direct ``get_node(node_id)`` (exact match on node_id column)
          2. Property lookup: ``properties->>'entity_id' = node_id``
        """
        store = self._registry.knowledge.graph_store
        if store.get_node(node_id) is not None:
            return node_id
        # Fallback: search by entity_id stored in properties
        results = store.query(properties={"entity_id": node_id}, limit=1)
        if results:
            node_id_val: str | None = results[0]["node_id"]
            return node_id_val
        return None

    def _resolve_endpoints(self, source_id: str, target_id: str) -> tuple[str, str]:
        """Resolve both edge endpoints or raise :class:`ValidationError`.

        Centralises the FK-validation block so the happy-path of
        :meth:`handle` doesn't carry the per-side error wiring. Both
        endpoints are checked even on a single miss so callers see all
        root causes in one round trip.
        """
        resolved_source = self._resolve_node(source_id)
        resolved_target = self._resolve_node(target_id)
        missing: list[str] = []
        if resolved_source is None:
            missing.append(
                f"source_id={source_id!r} does not reference an existing entity"
            )
        if resolved_target is None:
            missing.append(
                f"target_id={target_id!r} does not reference an existing entity"
            )
        if missing:
            msg = f"LINK_CREATE FK check failed: {'; '.join(missing)}"
            # ``code`` becomes the ``reason`` field on the MUTATION_REJECTED
            # event the executor emits — see Variant A' in
            # docs/design/adr-extraction-validation.md.
            raise ValidationError(msg, errors=missing, code="orphan_edge")
        # Both checks passed → both resolved values are non-None.
        return resolved_source, resolved_target  # type: ignore[return-value]

    def handle(self, command: Command) -> tuple[str | None, str]:
        source_id = command.args["source_id"]
        target_id = command.args["target_id"]
        edge_kind = command.args["edge_kind"]
        allow_dangling = bool(command.args.get("allow_dangling", False))
        store = self._registry.knowledge.graph_store

        if not allow_dangling:
            source_id, target_id = self._resolve_endpoints(source_id, target_id)

        edge_id = store.upsert_edge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_kind,
            properties=command.args.get("properties"),
        )

        self._registry.operational.event_log.emit(
            EventType.LINK_CREATED,
            source="mutation_executor",
            entity_id=edge_id,
            payload={
                "source_id": source_id,
                "target_id": target_id,
                "edge_kind": edge_kind,
            },
        )
        return edge_id, f"Link created: {source_id} --[{edge_kind}]--> {target_id}"


class ObservationRecordHandler:
    """Persist an Observation as a graph node + ``hasObservation`` edge.

    Wires :data:`Operation.OBSERVATION_RECORD` into the governed mutation
    pipeline so empirical-observation ingestion follows the same audit /
    idempotency / policy contract as every other mutation. ``args``
    expects an ``observation`` key holding either an :class:`Observation`
    instance or a dict; dicts are routed through
    ``Observation.model_validate`` so any missing required field raises
    a :class:`pydantic.ValidationError` *inside* the handler — surfaced
    as ``CommandStatus.FAILED`` to the caller, never silently defaulted.

    Idempotency semantics: re-recording with the same id REPLACES the
    existing observation and re-emits the ``OBSERVATION_RECORDED`` event.
    This diverges from :class:`TraceIngestHandler`'s
    short-circuit-on-repeat behavior because Observations are mutable
    signals over time — an agent may revise its confidence as new
    evidence arrives. Use a new ``observation_id`` to record a distinct
    signal. See ``docs/design/adr-observation-entity-type.md`` §2.1;
    ``hasObservation`` is the canonical edge from the subject entity to
    the observation node.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["observation"]
        try:
            obs = (
                raw if isinstance(raw, Observation) else Observation.model_validate(raw)
            )
        except Exception as exc:
            # Loud-on-missing-required-field discipline. The executor
            # turns ValidationError into a structured rejection event.
            # describe_validation_error, not str(exc): pydantic's own text
            # embeds input_value=<the caller's field value>, and this
            # message becomes the immutable mutation.rejected audit event.
            msg = f"Observation validation failed: {describe_validation_error(exc)}"
            raise ValidationError(msg, code="observation_validation") from exc

        store = self._registry.knowledge.graph_store

        # Idempotent upsert — repeat submissions of the same observation_id
        # collapse onto the existing node. The graph store's SCD-2 layer
        # treats a same-payload upsert as a no-op version close + reopen.
        props: dict[str, Any] = {
            "observation_id": obs.observation_id,
            "subject_entity_id": obs.subject_entity_id,
            "subject_entity_type": obs.subject_entity_type,
            "observer_agent_id": obs.observer_agent_id,
            "content": obs.content,
            "confidence": obs.confidence,
            "observed_at": obs.observed_at.isoformat(),
        }
        if obs.evidence_ref is not None:
            props["evidence_ref"] = obs.evidence_ref
        if obs.metadata is not None:
            props["metadata"] = obs.metadata

        node_id = store.upsert_node(
            node_id=obs.observation_id,
            node_type=OBSERVATION,
            properties=props,
        )

        # Best-effort hasObservation edge from the subject entity to the
        # new observation node. The subject may not be a graph node in
        # all deployments (e.g., raw trace IDs) — when the FK fails we
        # still keep the observation row, but the edge is skipped. This
        # matches the open-string entity-type rule (CLAUDE.md
        # type-extensibility).
        #
        # Narrow the catch to ``StoreError`` (which ``NotFoundError``
        # subclasses): unexpected exceptions (DB driver bugs, schema
        # errors) are escalated rather than silently swallowed — see
        # PR #122 (C2 Phase 5) for the silent-fallback discipline.
        try:
            store.upsert_edge(
                source_id=obs.subject_entity_id,
                target_id=node_id,
                edge_type=HAS_OBSERVATION,
            )
        except (NotFoundError, StoreError) as exc:
            logger.info(
                "observation_edge_skipped",
                subject_entity_id=obs.subject_entity_id,
                observation_id=node_id,
                reason=str(exc),
            )

        self._registry.operational.event_log.emit(
            EventType.OBSERVATION_RECORDED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=OBSERVATION,
            payload={
                "observation_id": node_id,
                "subject_entity_id": obs.subject_entity_id,
                "subject_entity_type": obs.subject_entity_type,
                "observer_agent_id": obs.observer_agent_id,
                "confidence": obs.confidence,
            },
        )
        return node_id, f"Observation recorded: {node_id}"


class MeasurementRecordHandler:
    """Persist a Measurement as an append-only graph node.

    Sibling of :class:`ObservationRecordHandler` keyed on
    ``measurement_id``. See ``docs/design/adr-observation-entity-type.md``
    §2.1 — Measurement rows are *append-only by convention* so the SCD-2
    cost of high-frequency metric streams stays bounded.

    Idempotency semantics: re-recording with the same id REPLACES the
    existing measurement and re-emits the ``MEASUREMENT_RECORDED`` event.
    This diverges from :class:`TraceIngestHandler`'s
    short-circuit-on-repeat behavior because Measurements are mutable
    signals over time — a metric may be re-measured with a corrected
    value. Use a new ``measurement_id`` to record a distinct signal.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["measurement"]
        try:
            meas = (
                raw if isinstance(raw, Measurement) else Measurement.model_validate(raw)
            )
        except Exception as exc:
            # describe_validation_error, not str(exc): see the matching
            # comment in ObservationRecordHandler.handle above.
            msg = f"Measurement validation failed: {describe_validation_error(exc)}"
            raise ValidationError(msg, code="measurement_validation") from exc

        store = self._registry.knowledge.graph_store

        props: dict[str, Any] = {
            "measurement_id": meas.measurement_id,
            "subject_entity_id": meas.subject_entity_id,
            "subject_entity_type": meas.subject_entity_type,
            "metric_name": meas.metric_name,
            "metric_value": meas.metric_value,
            "observer_agent_id": meas.observer_agent_id,
            "measured_at": meas.measured_at.isoformat(),
        }
        if meas.unit is not None:
            props["unit"] = meas.unit
        if meas.metadata is not None:
            props["metadata"] = meas.metadata

        node_id = store.upsert_node(
            node_id=meas.measurement_id,
            node_type=MEASUREMENT,
            properties=props,
        )

        # Same narrow-catch discipline as ObservationRecordHandler —
        # silent-fallback hygiene per PR #122 (C2 Phase 5). Measurement
        # gets its own ``hasMeasurement`` edge kind so consumers can
        # route on edge kind alone without inspecting the target node's
        # type (adr-observation-entity-type.md §2.2).
        try:
            store.upsert_edge(
                source_id=meas.subject_entity_id,
                target_id=node_id,
                edge_type=HAS_MEASUREMENT,
            )
        except (NotFoundError, StoreError) as exc:
            logger.info(
                "measurement_edge_skipped",
                subject_entity_id=meas.subject_entity_id,
                measurement_id=node_id,
                reason=str(exc),
            )

        self._registry.operational.event_log.emit(
            EventType.MEASUREMENT_RECORDED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=MEASUREMENT,
            payload={
                "measurement_id": node_id,
                "subject_entity_id": meas.subject_entity_id,
                "subject_entity_type": meas.subject_entity_type,
                "metric_name": meas.metric_name,
                "metric_value": meas.metric_value,
                "observer_agent_id": meas.observer_agent_id,
            },
        )
        return node_id, f"Measurement recorded: {node_id}"


#: Upper bound on ``redaction.apply``'s ``reason`` arg. The reason is
#: written verbatim into the append-only audit log — the one payload
#: field that *could* re-contain the redacted content — so it is kept
#: short and rejected loudly rather than silently truncated.
MAX_REDACTION_REASON_CHARS = 2000

#: Cap on the linked observation / measurement id lists in the
#: ``REDACTION_APPLIED`` payload (they are follow-up pointers, not an
#: exhaustive index).
_LINKED_SIGNAL_LIMIT = 100


#: Page size of the precedent walk below. Not a cap: the walk continues
#: until it has read every precedent.
_PRECEDENT_PAGE_SIZE = 500


def _precedents_sourced_from(graph: GraphStore, target_id: str) -> list[dict[str, Any]]:
    """Current precedent nodes that copied text from ``target_id``.

    The graph API has no index on ``source_item_id`` and its property filter
    is equality only, which cannot express "a chunk of this document", so
    this reads every precedent-typed node, page by page through
    :meth:`~trellis.stores.base.graph.GraphStore.search_nodes` (the one node
    read every backend pages, with a stable order), and filters here. The
    walk finishes before anything is scrubbed, so a scrub's re-created node
    cannot move under it. A read that raises propagates before anything is
    purged: a partial scan never reaches success.

    A precedent's ``source_item_id`` names a document or one of its
    ``<parent>#chunk-N`` fragments. A parent target matches itself and its
    chunks (the chunk id must start with ``<target>#chunk-``, so a sibling
    document sharing a prefix does not match); a chunk target matches only
    that chunk. Returns the matching rows.
    """
    # Deferred: ``trellis.ingest_corpus`` pulls the sync stack.
    from trellis.ingest_corpus.models import (  # noqa: PLC0415
        CHUNK_ID_SEPARATOR,
        is_chunk_doc_id,
    )

    chunk_prefix = (
        None if is_chunk_doc_id(target_id) else f"{target_id}{CHUNK_ID_SEPARATOR}"
    )
    matched: list[dict[str, Any]] = []
    offset = 0
    first_total: int | None = None
    while True:
        rows, total = graph.search_nodes(
            node_type="precedent",
            sort="node_type",
            descending=False,
            limit=_PRECEDENT_PAGE_SIZE,
            offset=offset,
        )
        # Offset paging skips a row when an earlier one disappears between
        # pages, so a set that changed size mid-scan refuses rather than
        # risk leaving an excerpt behind. Nothing has been purged yet.
        if first_total is None:
            first_total = total
        elif total != first_total:
            msg = (
                "precedent set changed during the redaction scan "
                f"({first_total} -> {total} nodes); re-run the redaction"
            )
            raise StoreError(msg, store="graph")
        for row in rows:
            source = (row.get("properties") or {}).get("source_item_id")
            if not isinstance(source, str) or not source:
                continue
            if source == target_id or (
                chunk_prefix and source.startswith(chunk_prefix)
            ):
                matched.append(row)
        offset += len(rows)
        if not rows or offset >= total:
            return matched


def _scrub_precedent_excerpt(registry: StoreRegistry, row: dict[str, Any]) -> bool:
    """Remove a precedent's copied source text from every version it has.

    The text is the excerpt in its description and, when the name was derived
    from the source's first line (``name_source``, see
    :func:`~trellis.learning.scoring.name_may_hold_source_text`), the name:
    that becomes :func:`~trellis.learning.scoring.neutral_precedent_name`,
    marked ``neutral``. A name that came from a title or a reviewer stays.

    Matches how ``redaction.apply`` treats its own target: a purge, not an
    SCD-2 close. A new version would leave the excerpt readable through
    ``get_node_history`` and ``as_of``, and the graph API cannot rewrite a
    closed version, so the node is purged and re-created from its current
    version with the excerpt removed and ``source_excerpt_status`` set to
    :data:`~trellis.learning.scoring.SOURCE_EXCERPT_REDACTED`. Everything
    else on the current version survives: properties, role, document links,
    live edges (provenance included) and aliases. What does not survive is
    the precedent's own earlier versions (which is the point) and the ids
    and timestamps of its edge rows.

    Returns ``False`` when no version carries an excerpt or a source-derived
    name (nothing to scrub, and the history is left alone). The precedent's
    document row, when it has one, is rewritten from the scrubbed name and
    description before anything is purged (a failure there leaves the
    redaction re-runnable) and re-embedded after the node is back. The vector
    row is deleted first: it was embedded from the description. If the
    re-create fails after the purge, the precedent is gone, never
    half-redacted, and the error names its id.
    """
    from trellis.learning.scoring import (  # noqa: PLC0415
        NAME_SOURCE_NEUTRAL,
        SOURCE_EXCERPT_REDACTED,
        carries_source_excerpt,
        name_may_hold_source_text,
        neutral_precedent_name,
        strip_source_excerpt,
    )

    graph = registry.knowledge.graph_store
    precedent_id = row["node_id"]
    versions = graph.get_node_history(precedent_id) or [row]
    version_props = [v.get("properties") or {} for v in versions]
    has_excerpt = any(carries_source_excerpt(p) for p in version_props)
    has_source_name = any(
        name_may_hold_source_text(p, precedent_id) for p in version_props
    )
    if not (has_excerpt or has_source_name):
        return False

    properties = dict(row.get("properties") or {})
    if has_excerpt:
        description = properties.get("description")
        if isinstance(description, str):
            properties["description"] = strip_source_excerpt(
                description, str(properties.get("source_item_id") or "")
            )
        properties["source_excerpt_status"] = SOURCE_EXCERPT_REDACTED
    if name_may_hold_source_text(properties, precedent_id):
        properties["name"] = neutral_precedent_name(
            precedent_id, properties.get("intent_family")
        )
        properties["name_source"] = NAME_SOURCE_NEUTRAL

    edges = {e["edge_id"]: e for e in graph.get_edges(precedent_id, direction="both")}
    aliases = graph.get_aliases(precedent_id)

    scrubbed_document = _rewrite_precedent_document(registry, precedent_id, properties)
    registry.knowledge.vector_store.delete(precedent_id)
    if not graph.delete_node(precedent_id):
        return False  # a concurrent purge already removed it
    try:
        graph.upsert_node(
            precedent_id,
            row["node_type"],
            properties,
            node_role=row.get("node_role") or "semantic",
            generation_spec=row.get("generation_spec"),
            document_ids=row.get("document_ids") or None,
        )
    except Exception as exc:
        # The listing withholds the promoted excerpt for a precedent with no
        # node (see ``trellis.retrieve.precedents``), so the copy in
        # ``PRECEDENT_PROMOTED`` stays unserved; what is lost is the node and
        # its links, which the log names so they can be rebuilt.
        logger.warning(
            "redaction.precedent_recreate_failed",
            precedent_id=precedent_id,
            edge_ids=sorted(edges),
            alias_count=len(aliases),
        )
        msg = (
            f"precedent {precedent_id} was purged to scrub its source excerpt "
            f"but could not be re-created ({type(exc).__name__}); its node, "
            f"{len(edges)} edge(s) and {len(aliases)} alias(es) are gone and it "
            "must be re-promoted"
        )
        raise StoreError(msg, store="graph") from exc

    if scrubbed_document is not None:
        from trellis.retrieve.embed_ingest_hook import (  # noqa: PLC0415
            run_embed_on_ingest,
        )

        run_embed_on_ingest(
            registry,
            precedent_id,
            scrubbed_document[0],
            scrubbed_document[1],
            source="redaction.apply",
        )

    # Each link is restored on its own, so one failure does not strand the
    # rest. A re-run cannot finish the job (no version carries an excerpt
    # any more, so the precedent no longer matches), which is why every
    # unrestored link is named rather than summarised.
    unrestored_edges: list[str] = []
    for edge in edges.values():
        try:
            graph.upsert_edge(
                edge["source_id"],
                edge["target_id"],
                edge["edge_type"],
                edge.get("properties"),
                source_trace_id=edge.get("source_trace_id"),
                agent_id=edge.get("agent_id"),
                confidence=edge.get("confidence"),
                evidence_ref=edge.get("evidence_ref"),
                extractor_tier=edge.get("extractor_tier"),
            )
        except Exception:
            unrestored_edges.append(
                f"{edge['source_id']} -{edge['edge_type']}-> {edge['target_id']}"
            )
    unrestored_aliases: list[str] = []
    for alias in aliases:
        try:
            graph.upsert_alias(
                precedent_id,
                alias["source_system"],
                alias["raw_id"],
                raw_name=alias.get("raw_name"),
                match_confidence=alias.get("match_confidence", 1.0),
                is_primary=bool(alias.get("is_primary", False)),
            )
        except Exception:
            unrestored_aliases.append(f"{alias['source_system']}:{alias['raw_id']}")
    if unrestored_edges or unrestored_aliases:
        logger.warning(
            "redaction.precedent_links_not_restored",
            precedent_id=precedent_id,
            edges=unrestored_edges,
            aliases=unrestored_aliases,
        )
        msg = (
            f"precedent {precedent_id} was scrubbed and re-created, but "
            f"{len(unrestored_edges)} edge(s) and {len(unrestored_aliases)} "
            "alias(es) could not be restored: "
            f"{'; '.join(unrestored_edges + unrestored_aliases)}"
        )
        raise StoreError(msg, store="graph")
    return True


def _scrub_precedents(
    registry: StoreRegistry, command: Command, rows: list[dict[str, Any]]
) -> list[str]:
    """Scrub each precedent in ``rows``; the sorted ids of those scrubbed."""
    scrubbed: list[str] = []
    try:
        for row in rows:
            did_scrub = _scrub_precedent_excerpt(registry, row)
            if did_scrub:
                scrubbed.append(row["node_id"])
    except Exception:
        # A scrub purges history irreversibly and REDACTION_APPLIED is never
        # emitted on this path, so the earlier scrubs are recorded here or
        # nowhere.
        _log_scrubbed_before_failure(command, scrubbed)
        raise
    return sorted(scrubbed)


def _log_scrubbed_before_failure(command: Command, scrubbed: list[str]) -> None:
    """Record precedents a failed redaction had already scrubbed."""
    if scrubbed:
        logger.warning(
            "redaction.precedents_scrubbed_before_failure",
            command_id=command.command_id,
            scrubbed_precedent_ids=sorted(scrubbed),
        )


class RedactionApplyHandler:
    """Hard-purge a graph entity, emitting ``REDACTION_APPLIED``.

    Wires :data:`Operation.REDACTION_APPLY` into the governed pipeline. The
    enum verb shipped with no handler (the same gap ``entity.update`` had
    before :class:`EntityUpdateHandler`), so the executor rejected every
    ``redaction.apply`` command with "No handler registered" — defect
    cleanups had to fall back to *neutralizing* entities via
    ``entity.update`` because no governed deletion path existed.

    **Redaction is a purge, not an SCD-2 close.** ``delete_node`` physically
    removes *all* version rows, cascades to every edge version touching the
    node, and drops its alias rows (pinned by the graph-store contract
    tests). After redaction the entity is unreachable through ``get_node``,
    ``as_of`` time-travel, and ``get_node_history`` — a redaction that
    time-travel can resurrect would not be a redaction. The vector entry is
    deleted *before* the graph purge (``item_id == node_id`` is the
    shape-#2 contract on the bolt backends; the standalone stores key
    vectors by document id, so the delete is a recorded no-op there): a
    vector-backend failure therefore aborts before anything irreversible
    happens. The reverse failure — vector gone, graph purge raises — leaves
    the entity intact minus its re-derivable embedding, never the reverse.

    **The EventLog is the audit trail, and it never re-contains content.**
    The ``REDACTION_APPLIED`` payload carries the ``reason``, counts, and id
    pointers only — no name, no properties (see
    :attr:`~trellis.stores.base.event_log.EventType.REDACTION_APPLIED` for
    the schema). Because that event is the only record that survives the
    purge, the handler refuses to run against a ``NullEventLog``
    (``code="redaction_requires_event_log"``), and the emit itself is
    guarded: if it fails after the purge, the content-free payload is
    preserved in operator logs rather than reporting FAILED for a redaction
    that already happened. Scope is the Knowledge Plane: prior
    Operational-Plane events (e.g. the original ``ENTITY_CREATED`` payload)
    and traces are immutable by design and are not rewritten.

    **Precedents that copied the target's text are scrubbed (#927).** A
    promoted precedent's description opens with up to 600 chars of its
    source document (``source_item_id``), and a precedent with no readable
    name takes up to 96 chars of the source's first line as its name. When
    the target is that document or a ``<document>#chunk-N`` fragment of it,
    the excerpt is removed from the precedent (every version, see
    :func:`_scrub_precedent_excerpt`), ``source_excerpt_status`` becomes
    ``source_redacted``, a source-derived name becomes a neutral one, and
    the rest of the precedent stays. The lookup reads every precedent, page
    by page, so no precedent is missed; a read that fails refuses the
    redaction before anything is purged. The ids ride the payload as
    ``scrubbed_precedent_ids``. Only a *graph node* can be a target, so
    this runs for a document that has one (memory extraction mints a
    node whose id is the document id); a document with no node is refused
    like any other missing target. The ``PRECEDENT_PROMOTED`` event keeps
    its copy of the description and title by design — the log is immutable —
    so ``list_precedents`` serves the node's text, or a neutral title, for a
    scrubbed precedent instead.

    **A precedent's own document row is blanked, not deleted.** Redacting a
    promoted precedent keeps its document id but empties the text and title and
    archives it (:func:`_tombstone_precedent_document`), since the document plane
    has no governed delete. The payload is unchanged.

    **Scope: graph entities only; linked records become pointers.**
    Documents linked from any purged version are *not* cascaded — a
    document may back many entities — and their union across versions rides
    the payload so a future document-level redaction can locate them.
    Observations and Measurements *about* the subject are independent
    governed nodes and are likewise not cascaded, but they carry
    ``subject_entity_id`` as a property, so property-based queries keep
    serving them after the purge; their ids ride the payload so the
    operator can redact each one individually (they are graph nodes — this
    same verb applies). A ``target_id`` that is not a graph node is refused
    (``code="target_not_found"`` → ``CommandStatus.REJECTED``).

    Idempotency: re-redacting a purged id is refused the same way, and a
    concurrent-purge race is detected via ``delete_node``'s return value:
    the loser raises :class:`~trellis.errors.NotFoundError`
    (→ ``CommandStatus.FAILED``) and emits no second
    ``REDACTION_APPLIED``. Both succeed, each emitting one, only when
    ``delete_node`` returns ``True`` to both (see ``GraphStore.delete_node``).
    Use ``Command.idempotency_key`` when at-most-once semantics are
    required. A blank ``reason`` is rejected
    (``code="redaction_reason_required"``) and an over-long one too
    (``code="redaction_reason_too_long"``) — the recorded justification is
    the point of governed redaction, and it must stay short and
    content-free.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "redaction.apply requires a non-empty reason for the audit trail"
            raise ValidationError(msg, code="redaction_reason_required")
        if len(reason) > MAX_REDACTION_REASON_CHARS:
            msg = (
                f"redaction.apply reason exceeds {MAX_REDACTION_REASON_CHARS} "
                "chars; it is written verbatim to the append-only audit log — "
                "keep it short and content-free"
            )
            raise ValidationError(msg, code="redaction_reason_too_long")

        event_log = self._registry.operational.event_log
        if isinstance(event_log, NullEventLog):
            msg = (
                "redaction.apply requires a persisting event log: the "
                "REDACTION_APPLIED event is the only record that survives "
                "the purge, and this deployment's event_log backend is 'null'"
            )
            raise ValidationError(msg, code="redaction_requires_event_log")

        graph = self._registry.knowledge.graph_store
        node = graph.get_node(target_id)
        if node is None:
            # Input to fix, not a store failure: refused like a label on a
            # missing node (REJECTED, ``code`` the audit reason, CLI exit 2).
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        entity_type = node["node_type"]
        history = graph.get_node_history(target_id)
        node_versions = len(history)
        # Union across ALL versions: the purge takes every version's rows
        # with it, and the payload is the only place the graph→document
        # pointers survive — the current version alone under-reports when
        # a later version replaced the link.
        document_ids = sorted(
            {d for version in history for d in version.get("document_ids") or []}
        )
        edges = len(graph.get_edges(target_id, direction="both"))
        aliases = len(graph.get_aliases(target_id))
        linked_observation_ids = [
            row["node_id"]
            for row in graph.query(
                node_type=OBSERVATION,
                properties={"subject_entity_id": target_id},
                limit=_LINKED_SIGNAL_LIMIT,
            )
        ]
        linked_measurement_ids = [
            row["node_id"]
            for row in graph.query(
                node_type=MEASUREMENT,
                properties={"subject_entity_id": target_id},
                limit=_LINKED_SIGNAL_LIMIT,
            )
        ]

        # Precedents that copied this document's text into their
        # description are scrubbed *before* the target is purged: a failure
        # here leaves the target in place, so the redaction can simply be
        # re-run (a scrubbed precedent no longer matches), where the other
        # order would strand a scrub that has nothing left to be triggered
        # by. No precedent sourced from the target is a no-op.
        precedent_rows = _precedents_sourced_from(graph, target_id)
        scrubbed_precedent_ids = _scrub_precedents(
            self._registry, command, precedent_rows
        )

        # Vector entry first: if the vector backend raises, the command
        # fails with the graph untouched — nothing irreversible has
        # happened yet.
        vector_deleted = self._registry.knowledge.vector_store.delete(target_id)
        if entity_type == "precedent":
            # The precedent's own document row holds the same text as the node.
            _tombstone_precedent_document(self._registry, target_id)

        if not graph.delete_node(target_id):
            # Lost a race: another writer purged the node between our read
            # and the delete. Fail rather than emit a second
            # REDACTION_APPLIED carrying counts this command did not purge.
            _log_scrubbed_before_failure(command, scrubbed_precedent_ids)
            raise NotFoundError(entity_type="entity", entity_id=target_id)

        # ``target_id`` can carry the exact hazard this handler exists to
        # purge: ``redaction.apply`` is deliberately absent from
        # ID_BEARING_ARG_KEYS (see OperationRegistry.validate in
        # mutate/commands.py), so a raw NUL byte or lone surrogate reaches
        # here unscrubbed, and REDACTION_APPLIED is "the only record that
        # survives the purge" (class docstring above). ``EventLog.append``
        # scrubs ``entity_id`` and ``payload`` on every ``emit()`` call
        # before any backend sees them, so this record lands on Postgres
        # the same as any other -- no local scrub needed here.
        payload: dict[str, Any] = {
            "target_id": target_id,
            "target_kind": "entity",
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "node_versions_purged": node_versions,
            "edges_purged": edges,
            "aliases_purged": aliases,
            "vector_deleted": vector_deleted,
            "document_ids": document_ids,
            "linked_observation_ids": linked_observation_ids,
            "linked_measurement_ids": linked_measurement_ids,
        }
        message = (
            f"Entity redacted: {target_id} "
            f"({node_versions} version(s), {edges} edge(s), "
            f"{aliases} alias(es), vector_deleted={vector_deleted})"
        )
        # Keys only when there is something to say, so a redaction that
        # reaches no precedent emits the payload it always did.
        if scrubbed_precedent_ids:
            payload["scrubbed_precedent_ids"] = scrubbed_precedent_ids
            payload["scrubbed_precedent_count"] = len(scrubbed_precedent_ids)
            message += (
                f"; source excerpt scrubbed from {len(scrubbed_precedent_ids)} "
                "precedent(s)"
            )
        try:
            event_log.emit(
                EventType.REDACTION_APPLIED,
                source="mutation_executor",
                entity_id=target_id,
                entity_type=entity_type,
                payload=payload,
            )
        except Exception:
            # Same discipline as ops/write_health.py: the purge already
            # happened, so raising would report FAILED for a completed
            # redaction and (via the executor's own emit against the same
            # log) lose the record entirely. Preserve the content-free
            # payload in operator logs instead.
            logger.warning(
                "redaction_audit_emit_failed",
                target_id=target_id,
                entity_type=entity_type,
                redaction_payload=payload,
                exc_info=True,
            )
            message += (
                " (WARNING: REDACTION_APPLIED audit emit failed; "
                "payload preserved in operator logs)"
            )
        return target_id, message


MAX_RETENTION_REASON_CHARS = 2000


def _sync_vector_lifecycle(
    registry: StoreRegistry,
    item_id: str,
    lifecycle: dict[str, Any],
    *,
    command: Command | None = None,
    log_event: str = "retention_vector_lifecycle_sync_failed",
) -> VectorSyncOutcome:
    """Stamp a lifecycle record onto a vector row that has no document write.

    Four call sites share this: ``RetentionPruneHandler._resync_archived``
    (a row that diverged *before* the ``put_document`` mirror seam existed),
    ``RetentionRestoreHandler._restore``'s graph-node branch (a graph node
    has no document write to piggyback a mirror onto, unlike the document
    branches of ``_archive``/``_restore``, which go through
    :func:`~trellis.core.document_write.put_document` instead, so the
    mirror there is not a step a future edit could forget), and
    ``PrecedentDemoteHandler``/``PrecedentReinstateHandler``, neither of
    which is a retention operation at all — pass ``log_event`` so the log
    line names the actual caller rather than defaulting to "retention" for
    a demotion or reinstatement.

    **A vector row's metadata is a snapshot taken at embed time**, and the
    semantic strategy builds its :class:`~trellis.schemas.pack.PackItem`
    from that snapshot rather than from the document store. So an archival
    written only through ``document_store.put`` leaves the semantic path
    serving the item as though nothing happened —
    :func:`~trellis.retrieve.lifecycle.exclude_archived` reads
    ``item.metadata`` and simply never sees a lifecycle key.

    That is not hypothetical: the first production prune archived 35
    documents and every one of them kept a vector row whose metadata still
    read ``signal_quality="standard"``. The keyword path honoured the
    archival immediately (it reads the document store); the semantic path
    did not, and a pack-level test written against
    :class:`~trellis.retrieve.strategies.KeywordSearch` alone could not see
    the difference.

    Metadata-only update: the existing embedding is re-``upsert``-ed
    unchanged, so nothing is re-embedded and no cost is incurred. A missing
    vector row is normal (structural nodes, un-embedded documents) and is a
    no-op rather than an error — retention must not fail because an item was
    never embedded.

    **Visibility on failure.** The document/graph stamp is already written
    by the time this runs, so a vector backend outage must not roll back the
    transition — it is logged, not raised. Logging alone leaves a JSON
    caller with no way to see that the semantic path went stale, so when
    ``command`` is given the same condition is also appended to
    ``command.metadata["handler_warnings"]``, which ``MutationExecutor``
    folds into the returned ``CommandResult.warnings`` (see
    ``_handler_warnings`` in ``executor.py``). The :data:`VectorSyncOutcome`
    is also returned so a caller that emits its own domain event (
    :class:`PrecedentDemoteHandler`, :class:`PrecedentReinstateHandler`) can
    fold a ``"failed"`` outcome into that event's payload too — the
    ``CommandResult`` warning and the event are two independent readers of
    the same fact, and a JSON caller may see either one without replaying
    the log.

    The actual get/merge/upsert is delegated to
    :func:`~trellis.core.vector_metadata.sync_vector_metadata_outcome` —
    the same mirror the classify layer uses for ``content_tags`` and
    ``auto_importance`` — rather than repeated here: two writers maintaining
    the same "read the row, merge one key, re-upsert unchanged" sequence is
    exactly how ``MIRRORED_METADATA_KEYS`` split into two disjoint sets
    before (see that module's docstring). ``keys=(LIFECYCLE_KEY,)`` narrows
    the mirror to the one key this caller owns, so a row that also carries
    ``content_tags``/``auto_importance`` is left untouched. Resolving the
    store itself can still raise (a misconfigured backend) outside anything
    ``sync_vector_metadata_outcome`` catches, since that function takes an
    already-resolved store; it is folded into the same "failed" handling
    here rather than treated as the separate, silent ``"no_store"`` outcome
    :func:`~trellis.core.vector_metadata.resolve_vector_store` reports for a
    *deployment with no vector store at all* — this function has never had
    that quiet case, and keeps not having it.
    """
    outcome: VectorSyncOutcome
    try:
        store = registry.knowledge.vector_store
    except Exception:
        # Fail soft and loud: the document-store stamp is the authoritative
        # record and has already been written. A vector backend outage
        # must not roll back an archival, but it does leave the semantic
        # path stale, so it is logged rather than swallowed. ``exc_info``
        # is valid here because we are still inside the handler for the
        # exception it reports.
        logger.warning(log_event, item_id=item_id, exc_info=True)
        outcome = "failed"
    else:
        outcome = sync_vector_metadata_outcome(
            store, item_id, {LIFECYCLE_KEY: lifecycle}, keys=(LIFECYCLE_KEY,)
        )
        if outcome == "failed":
            # No ``exc_info`` here: unlike the branch above, there is no
            # exception live at this point to attach -- ``outcome`` is a
            # sentinel the callee returned after already logging the real
            # traceback itself (``vector_metadata_sync_failed`` in
            # ``sync_vector_metadata_outcome``). This call records the
            # verb-specific ``log_event`` the tests and
            # ``handler_warnings`` below key on, not a second copy of the
            # same traceback.
            logger.warning(log_event, item_id=item_id)
    if outcome != "failed":
        # "synced" (re-stamped), "unchanged" (already agreed) and "absent"
        # (no vector row -- structural nodes, never-embedded documents) are
        # all non-errors here, same as the missing-row branch before this
        # was delegated.
        return outcome
    if command is not None:
        command.metadata.setdefault("handler_warnings", []).append(
            f"{log_event}: vector row for {item_id} was not re-stamped; "
            "the semantic path may continue serving a stale snapshot "
            "until the next resync"
        )
    return outcome


def _prune_message(
    report: ResolutionReport,
    by_reason: dict[str, int],
    *,
    dry_run: bool,
    archived: int,
    skipped: int,
    resynced: int,
) -> str:
    """Render the operator-facing summary for one prune run.

    Every qualifier here reports something that would otherwise be silent:
    a capped scan (the candidate set is a prefix), a requested lifecycle
    state this phase cannot select at all, items that vanished mid-run,
    vector rows repaired, and items protected because a human restored them.
    """
    verb = "would archive" if dry_run else "archived"
    counts = ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())) or "none"
    message = (
        f"retention.prune ({'dry run' if dry_run else 'applied'}): "
        f"{verb} {len(report.candidates) if dry_run else archived} item(s) "
        f"[{counts}]"
    )
    if report.unselectable_lifecycle_states:
        # #419: without this the operator cannot tell "no rows are in that
        # state" from "this phase can never select that state".
        inert = ", ".join(report.unselectable_lifecycle_states)
        # Phrased as the rule, not the writer: "current items were explicitly
        # restored" would be the same provenance claim #419's dangerous half
        # was about. ``current`` is protected for what the value asserts,
        # whoever wrote it.
        message += (
            f"; WARNING lifecycle state(s) [{inert}] cannot be selected by "
            "phase one (archived is already archived; current asserts the "
            "item belongs in service) — they scanned nothing and their zero "
            "says nothing about the corpus"
        )
    if report.scan_truncated:
        message += (
            f"; WARNING scan capped at {MAX_DOCUMENTS_SCANNED} documents — "
            "candidate set is a prefix, re-run to continue"
        )
    if skipped:
        message += f"; {skipped} skipped (vanished between resolve and write)"
    if resynced:
        message += f"; {resynced} stale vector row(s) re-synced"
    if report.skipped_restored:
        # Not "(explicitly restored)": ``retention.restore`` is the writer
        # that motivated the guard, not the only one that can trip it — an
        # ``entity.update`` property merge or any bag-forwarding save writes
        # ``current`` too, and lands in this same counter (#419).
        message += (
            f"; {report.skipped_restored} protected (state=current — "
            "asserted in service)"
        )
    if report.skipped_precedent:
        message += (
            f"; {report.skipped_precedent} promoted-precedent item(s) (document "
            "rows and graph nodes) exempt (their lifecycle belongs to the "
            "precedent handlers)"
        )
    return message


class RetentionPruneHandler:
    """Resolve retention criteria and archive the resulting candidates.

    Wires :data:`Operation.RETENTION_PRUNE` into the governed pipeline. The
    enum verb shipped with an empty ``set()`` args schema and no handler, so
    the executor rejected every ``retention.prune`` command with "No handler
    registered" — the exact gap ``redaction.apply`` had before
    :class:`RedactionApplyHandler`, and the reason 24 noise-tagged captures
    had to be demoted-and-kept rather than removed. Decision record:
    ``docs/design/adr-retention-prune.md`` (Option A, phase one).

    **Phase one is archival, not purge.** The handler stamps
    :class:`~trellis.schemas.classification.Lifecycle`
    ``state="archived"`` and retrieval stops serving the item
    (:mod:`trellis.retrieve.lifecycle`). Physical removal is deliberately
    deferred until the archived population is real: retention is criteria-
    driven and a criteria bug is a *batch* mistake, so the version an
    operator can walk back is the one worth shipping first. This makes
    ``Lifecycle`` — schema-without-behaviour since it landed — both its
    first writer and its first enforcement point.

    **Dry-run by default.** ``dry_run`` defaults to ``True`` when the caller
    omits it. Destructive-by-default is right for a single named redaction
    target and wrong for a predicate that resolves to a set the caller has
    not seen; a dry run emits the same event flagged ``dry_run=True`` so the
    preview is as auditable as the real thing.

    **Criteria-driven batches tolerate a moving candidate set.** An item
    that vanishes between resolution and write is counted in ``skipped``,
    not raised — unlike redaction's single-target
    :class:`~trellis.errors.NotFoundError`, a batch whose population shifts
    underneath it has not failed. Items already archived are filtered during
    resolution, so a re-run is a no-op rather than a second version bump.
    Use ``Command.idempotency_key`` when at-most-once semantics are needed.

    **Never traces, never event rows.** Enforced by construction:
    :func:`~trellis.mutate.retention.resolve_candidates` reads the document
    and graph stores and nothing else. Confirmed entities are dropped on
    every criterion — confirmation is a human's judgment that the entity is
    real, and age is not evidence against it.

    ``PolicyType.RETENTION`` gets its first consumer here: a deny rule on
    ``retention.prune`` is evaluated by the executor's standard policy stage
    before this handler runs, which is the operator's kill switch.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw_criteria = command.args["criteria"]
        if not isinstance(raw_criteria, dict):
            msg = "retention.prune criteria must be an object"
            raise ValidationError(msg, code="retention_criteria_invalid")
        criteria = RetentionCriteria.model_validate(raw_criteria)

        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "retention.prune requires a non-empty reason for the audit trail"
            raise ValidationError(msg, code="retention_reason_required")
        if len(reason) > MAX_RETENTION_REASON_CHARS:
            msg = (
                f"retention.prune reason exceeds {MAX_RETENTION_REASON_CHARS} "
                "chars; it is written verbatim to the append-only audit log — "
                "keep it short and content-free"
            )
            raise ValidationError(msg, code="retention_reason_too_long")

        dry_run = bool(command.args.get("dry_run", True))

        event_log = self._registry.operational.event_log
        if not dry_run and isinstance(event_log, NullEventLog):
            msg = (
                "retention.prune requires a persisting event log for a "
                "non-dry run: the RETENTION_PRUNED event is the only record "
                "that the archival happened, and this deployment's event_log "
                "backend is 'null'"
            )
            raise ValidationError(msg, code="retention_requires_event_log")

        report = resolve_candidates(criteria, self._registry)

        archived = 0
        skipped = 0
        resynced = 0
        unfinished: Exception | None = None
        if not dry_run:
            try:
                for candidate in report.candidates:
                    if self._archive(candidate, reason):
                        archived += 1
                    else:
                        skipped += 1
                # Self-healing: an item archived before the vector sync
                # existed still has a stale snapshot, and the semantic path
                # serves from that snapshot. Re-running the prune repairs
                # them — no new verb for what is a one-shot consequence of
                # shipping the sync late.
                resynced = self._resync_archived(report.already_archived_ids)
            except Exception as exc:
                # Re-raised after the event: what was archived stays archived.
                unfinished = exc

        by_reason: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for candidate in report.candidates:
            code = candidate.reason_code
            by_reason[code] = by_reason.get(code, 0) + 1
            by_kind[candidate.kind] = by_kind.get(candidate.kind, 0) + 1

        payload: dict[str, Any] = {
            "criteria": criteria.model_dump(mode="json"),
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "dry_run": dry_run,
            "phase": "archival",
            "candidates": len(report.candidates),
            "archived": archived,
            "skipped": skipped,
            "by_reason": by_reason,
            "by_kind": by_kind,
            "documents_scanned": report.documents_scanned,
            "entities_scanned": report.entities_scanned,
            "scan_truncated": report.scan_truncated,
            "skipped_already_archived": report.skipped_already_archived,
            "skipped_confirmed": report.skipped_confirmed,
            "skipped_restored": report.skipped_restored,
            "skipped_precedent": report.skipped_precedent,
            # #419: which requested lifecycle states contributed a
            # guaranteed zero. Audited alongside the criteria that named
            # them so a reviewer reading this event later can tell an inert
            # criterion from an empty corpus.
            "unselectable_lifecycle_states": report.unselectable_lifecycle_states,
            "vector_rows_resynced": resynced,
            # Capped pointer, not an index — see _LINKED_SIGNAL_LIMIT.
            "item_ids": [c.item_id for c in report.candidates[:_LINKED_SIGNAL_LIMIT]],
        }

        message = _prune_message(
            report,
            by_reason,
            dry_run=dry_run,
            archived=archived,
            skipped=skipped,
            resynced=resynced,
        )

        try:
            event_log.emit(
                EventType.RETENTION_PRUNED,
                source="mutation_executor",
                entity_id=command.command_id,
                entity_type="retention_run",
                payload=payload,
            )
        except Exception:
            # Same discipline as RedactionApplyHandler: for a non-dry run the
            # archival already happened, so raising would report FAILED for
            # completed work. Preserve the payload in operator logs.
            logger.warning(
                "retention_audit_emit_failed",
                command_id=command.command_id,
                retention_payload=payload,
                exc_info=True,
            )
            message += (
                " (WARNING: RETENTION_PRUNED audit emit failed; "
                "payload preserved in operator logs)"
            )
        if unfinished is not None:
            raise unfinished
        return command.command_id, message

    def _resync_archived(self, item_ids: list[str]) -> int:
        """Re-stamp vector rows for already-archived items whose row is stale.

        Returns the number actually repaired — a row that already carries the
        archived stamp is left alone, so a steady-state re-run reports zero.
        """
        if not item_ids:
            return 0
        lifecycle = Lifecycle(state=ARCHIVED_STATE).model_dump(mode="json")
        vector_store = self._registry.knowledge.vector_store
        repaired = 0
        for item_id in item_ids:
            try:
                row = vector_store.get(item_id)
            except Exception:
                logger.warning(
                    "retention_vector_resync_read_failed",
                    item_id=item_id,
                    exc_info=True,
                )
                continue
            if row is None:
                continue
            metadata = row.get("metadata") or {}
            record = metadata.get(LIFECYCLE_KEY)
            if isinstance(record, dict) and record.get("state") == ARCHIVED_STATE:
                continue
            _sync_vector_lifecycle(self._registry, item_id, lifecycle)
            repaired += 1
        return repaired

    def _archive(self, candidate: RetentionCandidate, reason: str) -> bool:
        """Stamp one candidate archived. Returns False if it vanished."""
        lifecycle = Lifecycle(
            state=ARCHIVED_STATE,
            valid_until=datetime.now(UTC),
            deprecation_reason=reason,
        ).model_dump(mode="json")

        if candidate.kind == "document":
            store = self._registry.knowledge.document_store
            doc = store.get(candidate.item_id)
            if doc is None:
                return False
            metadata = dict(doc.get("metadata") or {})
            metadata[LIFECYCLE_KEY] = lifecycle
            # Metadata-only: the content written back is the row's own, and
            # only the lifecycle stamp changes.
            #
            # Masked on the *pack* surfaces — ``retrieve.lifecycle``'s
            # ``exclude_archived`` drops the row at the collect seam — and
            # ``_restore`` is the operation that hands it back to retrieval
            # still carrying whatever stamp this write left. But that is not
            # the same as latent (#406): ``retrieve.file_context`` reads the
            # column straight off ``list_documents``, with no lifecycle
            # predicate, so an archived document's bump moves the
            # ``newest_item_at`` its path reports to the read hook's staleness
            # gate immediately.
            put_document(
                store,
                resolve_vector_store(self._registry),
                candidate.item_id,
                doc["content"],
                metadata,
                preserve_updated_at=True,
            )
            return True

        graph = self._registry.knowledge.graph_store

        def archive(node: dict[str, Any]) -> dict[str, Any]:
            props = dict(node["properties"])
            props[LIFECYCLE_KEY] = lifecycle
            return {"properties": props}

        # SCD-2: the rewrite closes the current version and opens a new one,
        # so the pre-archival state stays readable via get_node_history and
        # ``as_of``. A node purged since the resolve is skipped, not re-created.
        return _rewrite_current_node(graph, candidate.item_id, archive) is not None


def _pre_archival_lifecycle(graph: GraphStore, item_id: str) -> dict[str, Any]:
    """Return the ``Lifecycle`` dict ``item_id`` held right before its most
    recent archival, read from SCD-2 history rather than assumed.

    A plain ``Lifecycle(state="current")`` here would restore a precedent
    that was *demoted*, then pruned, straight back to ``"current"`` --
    laundering the demotion through a prune/restore round-trip with no
    trace of it in the new version (the demoted one is still in history,
    but nothing about the restored version says to look). Walks back past
    every consecutive archived-state version -- an intervening write that
    never touches ``lifecycle`` (e.g. ``entity.update``) carries
    ``"archived"`` forward through the property merge, so more than one
    version can read as archived -- to the version immediately before the
    most recent transition into it, and returns that version's whole
    ``Lifecycle`` record, not just its ``state``, so a demotion's
    ``deprecation_reason`` and ``valid_until`` come back too.

    Refuses rather than guesses when history cannot answer the question:
    every version on record reads as archived, including the degenerate
    case of no history at all (an id purged mid-command) — either way,
    no pre-archival version exists to read.
    """
    history = graph.get_node_history(item_id)
    idx = 0
    total = len(history)
    while idx < total and _current_lifecycle_state(history[idx]) == ARCHIVED_STATE:
        idx += 1
    if idx >= total:
        msg = (
            f"retention.restore cannot determine the lifecycle state "
            f"{item_id} held before archival: its SCD-2 history has no "
            "version that is not itself archived"
        )
        raise ValidationError(
            msg, errors=[msg], code="retention_restore_history_unavailable"
        )
    record = history[idx]["properties"].get(LIFECYCLE_KEY)
    if isinstance(record, dict):
        return dict(record)
    # Absent/malformed on the pre-archival version itself fails open to
    # "current", matching _current_lifecycle_state's own convention.
    return Lifecycle(state=CURRENT_STATE).model_dump(mode="json")


def _validate_restore_args(command: Command) -> tuple[list[str], str]:
    """``item_ids`` and ``reason``, validated for ``retention.restore``.

    Split out of ``RetentionRestoreHandler.handle`` to keep that method's
    branch count under the linter's ceiling.
    """
    raw_ids = command.args["item_ids"]
    if not isinstance(raw_ids, list) or not raw_ids:
        msg = "retention.restore requires a non-empty item_ids list"
        raise ValidationError(msg, code="retention_restore_ids_required")
    item_ids = [str(i) for i in raw_ids]

    reason = str(command.args["reason"] or "").strip()
    if not reason:
        msg = "retention.restore requires a non-empty reason for the audit trail"
        raise ValidationError(msg, code="retention_reason_required")
    if len(reason) > MAX_RETENTION_REASON_CHARS:
        msg = f"retention.restore reason exceeds {MAX_RETENTION_REASON_CHARS} chars"
        raise ValidationError(msg, code="retention_reason_too_long")
    return item_ids, reason


def _raise_nothing_restored(refused: list[dict[str, str]], skipped: list[str]) -> None:
    """Escalate a wholly-unactioned ``retention.restore`` batch.

    "Skipped" alone is a legitimate success (a corrective batch may
    legitimately name items someone else already restored), but a batch
    that restored nothing *and* refused at least one id is not a no-op
    retry — it is reported to the caller as rejected, naming every
    refused id *and* every skipped one, not only the refused half, so an
    operator reading the rejection sees the whole batch rather than just
    the part that failed loudly. Split out of
    ``RetentionRestoreHandler.handle`` to keep that method's branch count
    under the linter's ceiling.
    """
    codes = {r["code"] for r in refused}
    code = (
        next(iter(codes)) if len(codes) == 1 else "retention_restore_nothing_restored"
    )
    detail = "; ".join(f"{r['item_id']} ({r['code']})" for r in refused)
    if skipped:
        detail += "; " + "; ".join(f"{item_id} (not archived)" for item_id in skipped)
    msg = f"retention.restore restored none of the requested items: {detail}"
    raise ValidationError(msg, errors=[r["message"] for r in refused], code=code)


class RetentionRestoreHandler:
    """Return archived items to ``Lifecycle.state="current"``.

    Wires :data:`Operation.RETENTION_RESTORE`. This is the half that makes
    :class:`RetentionPruneHandler`'s central claim true: phase one is
    archival rather than purge *because* "a wrong prune is walked back by
    re-stamping" — and re-stamping needs a governed path, or the claim is
    only rhetorical. Direct store writes are not an option
    (``CLAUDE.md``: all mutations go through the governed pipeline), so
    without this verb an operator who over-pruned had no sanctioned remedy
    at all.

    **Explicit ids, not a predicate.** ``retention.prune`` resolves criteria
    because it selects a population nobody has enumerated. Restore is the
    opposite situation: the operator knows exactly which items were wrong,
    because their ids ride the ``RETENTION_PRUNED`` payload. Re-deriving
    them from criteria would re-run the selection that was wrong the first
    time.

    Restoring a document sets ``state="current"`` and clears the
    archival's ``valid_until`` / ``deprecation_reason`` rather than
    deleting the lifecycle record — the item is current again, and the
    ``RETENTION_PRUNED`` → ``RETENTION_RESTORED`` event pair is the
    history. A graph node is restored to whatever state it held
    immediately *before* that archival instead — read from SCD-2 history
    via :func:`_pre_archival_lifecycle` rather than assumed to be
    ``"current"`` — so a precedent that was demoted and then pruned comes
    back demoted, not reinstated: prune-then-restore is not a second,
    unaudited way to undo a demotion.

    A vector row's metadata is an embed-time snapshot, not a live read of
    the graph store, so a graph-node restore also re-stamps the node's
    vector row with the lifecycle it was just given — the same
    ``_sync_vector_lifecycle`` call :class:`PrecedentDemoteHandler` and
    :class:`PrecedentReinstateHandler` already make. Without it, a
    restored precedent's row kept reading ``archived`` and stayed withheld
    on the semantic path even though the graph node itself was current
    again — wrong, though it failed closed rather than open. The document
    branch needs no equivalent call: ``put_document`` already mirrors the
    lifecycle onto the vector row for every document write. A sync failure
    does not undo the restore (the graph write already happened); it is
    logged and appended to ``CommandResult.warnings``, exactly like a
    demote/reinstate sync failure.

    An id that is not archived is counted in ``skipped``, not raised: a
    corrective batch assembled from an audit payload will legitimately name
    items someone else already restored.

    A precedent whose history cannot answer what it held before archival
    (:func:`_pre_archival_lifecycle`) is counted in ``refused``, not raised
    out of the loop: the ids in the same batch after it are independent
    restores and the ones already restored before it stay restored. Each
    refusal's id and code land in the ``RETENTION_RESTORED`` payload's
    ``refused_items`` and in ``CommandResult.warnings`` (via
    ``command.metadata["handler_warnings"]``, see ``_handler_warnings`` in
    ``executor.py``), so a caller sees exactly which ids need
    ``precedent.reinstate`` or a different remedy instead of a blanket
    retry. The command is rejected only when *nothing* in the batch was
    restored — some skipped and some refused, with nothing restored, still
    rejects, since "skipped" alone cannot turn a wholly-unactioned batch
    into a reported success. The rejection message names every skipped id
    alongside every refused one, not only the refused half, so an operator
    reading the rejection sees the whole batch rather than just the part
    that failed loudly. A genuinely unexpected exception (not this history
    refusal) still aborts the remainder of the batch, as before.

    ``RETENTION_RESTORED`` is emitted unconditionally, including for a
    batch that restored nothing at all (all refused, all skipped, or a
    mix of the two) — the same audit-completeness posture as
    ``RETENTION_PRUNED``, which is emitted in both dry-run and apply mode
    even over zero candidates. Nothing in this codebase branches on the
    event's ``restored`` count, so there is no consumer to protect by
    suppressing it, and an audit trail with a gap for "requested but
    nothing happened" is a worse trail than one that records the attempt.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        item_ids, reason = _validate_restore_args(command)
        restored, skipped, refused, unfinished = self._restore_batch(item_ids, command)

        payload: dict[str, Any] = {
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "restored": len(restored),
            "skipped": len(skipped),
            "refused": len(refused),
            "restored_ids": restored,
            "skipped_ids": skipped,
            "refused_ids": [r["item_id"] for r in refused],
            "refused_items": refused,
        }
        message = f"retention.restore: restored {len(restored)} item(s)"
        if skipped:
            message += f", {len(skipped)} not archived (skipped)"
        if refused:
            message += f", {len(refused)} refused (history unavailable)"
            command.metadata.setdefault("handler_warnings", []).extend(
                f"retention.restore: {r['item_id']} refused ({r['code']}): "
                f"{r['message']}"
                for r in refused
            )
        try:
            self._registry.operational.event_log.emit(
                EventType.RETENTION_RESTORED,
                source="mutation_executor",
                entity_id=command.command_id,
                entity_type="retention_restore",
                payload=payload,
            )
        except Exception:
            logger.warning(
                "retention_restore_audit_emit_failed",
                command_id=command.command_id,
                restore_payload=payload,
                exc_info=True,
            )
            message += " (WARNING: RETENTION_RESTORED audit emit failed)"
        if unfinished is not None:
            raise unfinished
        if not restored and refused:
            _raise_nothing_restored(refused, skipped)
        return command.command_id, message

    def _restore_batch(
        self, item_ids: list[str], command: Command
    ) -> tuple[list[str], list[str], list[dict[str, str]], Exception | None]:
        """Restore every id, collecting outcomes rather than aborting on a
        per-item history refusal (see ``_pre_archival_lifecycle``).
        Anything other than that specific, expected refusal still escapes
        to the outer ``except`` and aborts the remainder via the returned
        exception — what was restored before it stays restored. Split out
        of ``handle`` to keep that method's branch count under the
        linter's ceiling.
        """
        restored: list[str] = []
        skipped: list[str] = []
        refused: list[dict[str, str]] = []
        unfinished: Exception | None = None
        try:
            for item_id in item_ids:
                try:
                    if self._restore(item_id, command):
                        restored.append(item_id)
                    else:
                        skipped.append(item_id)
                except ValidationError as exc:
                    refused.append(
                        {"item_id": item_id, "code": exc.code, "message": str(exc)}
                    )
        except Exception as exc:
            unfinished = exc
        return restored, skipped, refused, unfinished

    def _restore(self, item_id: str, command: Command) -> bool:
        """Un-archive one item. Returns False if it was not archived."""
        current = Lifecycle(state="current").model_dump(mode="json")

        doc_store = self._registry.knowledge.document_store
        doc = doc_store.get(item_id)
        if doc is not None:
            metadata = dict(doc.get("metadata") or {})
            record = metadata.get(LIFECYCLE_KEY)
            if not isinstance(record, dict) or record.get("state") != ARCHIVED_STATE:
                return False
            metadata[LIFECYCLE_KEY] = current
            # Metadata-only, and unmasked (#406). Restore exists to make the
            # item servable again, so unlike the archive it undoes there is
            # no downstream filter left to render a bumped ``updated_at``
            # moot: without the flag, un-archiving a two-year-old note hands
            # it back to ``KeywordSearch`` as the freshest document in the
            # corpus. An operator walking back a bad prune from the
            # ``RETENTION_PRUNED`` payload would thereby *promote* exactly
            # the items they meant only to restore.
            #
            # Scoped to the document branch, and that scoping is now
            # *correct* rather than merely pending. The entity branch below
            # re-opens an SCD-2 version whose ``updated_at`` is now — but
            # SCD-2 carries ``created_at`` forward, ``GraphStore.query`` is
            # ``ORDER BY created_at DESC LIMIT n`` on every shipped backend,
            # and since #420 ``GraphSearch`` decays off that same column
            # (``retrieve.strategies.GRAPH_RECENCY_CLOCK_FIELD``). So a
            # restored node neither re-enters the graph axis's candidate
            # window nor gains rank inside it, and the rewrite needs no
            # ``preserve_updated_at`` equivalent. Fixed reader-side because
            # the writers that actually move a node's ``updated_at`` are the
            # ordinary ones (``entity.update``, extraction upserts), which no
            # kwarg here would ever have been passed on.
            put_document(
                doc_store,
                resolve_vector_store(self._registry),
                item_id,
                doc["content"],
                metadata,
                preserve_updated_at=True,
            )
            return True

        graph = self._registry.knowledge.graph_store

        def restore(node: dict[str, Any]) -> dict[str, Any] | None:
            props = dict(node["properties"])
            record = props.get(LIFECYCLE_KEY)
            if not isinstance(record, dict) or record.get("state") != ARCHIVED_STATE:
                return None
            if node["node_type"] == "precedent":
                # Not ``current`` unconditionally: read the state the node
                # held just before this archival (SCD-2 history), so a
                # precedent that was demoted and then pruned comes back
                # demoted, not silently reinstated (see
                # _pre_archival_lifecycle). Scoped to precedents: every
                # other node type has no "demoted" concept to launder, and
                # requiring pre-archival history from them would refuse a
                # restore for a node that was simply minted archived.
                props[LIFECYCLE_KEY] = _pre_archival_lifecycle(graph, item_id)
            else:
                props[LIFECYCLE_KEY] = current
            return {"properties": props}

        written = _rewrite_current_node(graph, item_id, restore)
        if written is None:
            return False
        # A graph node has no document write to piggyback a vector mirror
        # onto (unlike the document branch above, which gets one for
        # free from put_document), so re-stamp the vector row directly --
        # the same gap PrecedentDemoteHandler/PrecedentReinstateHandler
        # close with this call. Without it, a restored precedent's row
        # kept reading archived and stayed withheld on the semantic path.
        _sync_vector_lifecycle(
            self._registry,
            item_id,
            written["properties"][LIFECYCLE_KEY],
            command=command,
            log_event="retention_restore_vector_lifecycle_sync_failed",
        )
        return True


def create_curate_handlers(
    registry: StoreRegistry,
    *,
    evidence_embed: EvidenceEmbedMode = "soft",
) -> dict[str, Any]:
    """Create all curate operation handlers for a given registry."""
    return {
        Operation.EVIDENCE_INGEST: EvidenceIngestHandler(
            registry,
            embed=evidence_embed,
        ),
        Operation.TRACE_INGEST: TraceIngestHandler(registry),
        Operation.PRECEDENT_PROMOTE: PrecedentPromoteHandler(registry),
        Operation.PRECEDENT_DEMOTE: PrecedentDemoteHandler(registry),
        Operation.PRECEDENT_REINSTATE: PrecedentReinstateHandler(registry),
        Operation.LABEL_ADD: LabelAddHandler(registry),
        Operation.LABEL_REMOVE: LabelRemoveHandler(registry),
        Operation.FEEDBACK_RECORD: FeedbackRecordHandler(registry),
        Operation.ENTITY_CREATE: EntityCreateHandler(registry),
        Operation.ENTITY_UPDATE: EntityUpdateHandler(registry),
        Operation.ALIAS_UPSERT: AliasUpsertHandler(registry),
        Operation.LINK_CREATE: LinkCreateHandler(registry),
        Operation.OBSERVATION_RECORD: ObservationRecordHandler(registry),
        Operation.MEASUREMENT_RECORD: MeasurementRecordHandler(registry),
        Operation.REDACTION_APPLY: RedactionApplyHandler(registry),
        Operation.RETENTION_PRUNE: RetentionPruneHandler(registry),
        Operation.RETENTION_RESTORE: RetentionRestoreHandler(registry),
    }


def create_settings_handlers(registry: StoreRegistry) -> dict[str, Any]:
    """Handlers for the admin-only settings write path.

    Deliberately **not** folded into :func:`create_curate_handlers`. That
    dict backs every generic mutation surface — REST ``POST
    /commands/batch`` and MCP ``execute_mutation`` — which route any
    ``mutate``-scoped key straight past the admin-only ``GET``/``PUT``
    gate on ``/api/v1/settings``, so registering this handler there would
    let any ``mutate``-scoped key reach a write that is meant to require
    the admin scope. Only :func:`trellis.mutate.build_settings_executor`
    builds an executor
    from this dict, and only the admin REST route and
    ``trellis admin settings`` call that builder — so
    ``Operation.SETTINGS_UPDATE`` is unreachable from the generic surfaces
    by construction, not by a policy that could be misconfigured away.
    Stage 2 still always runs: this flows through
    ``MutationExecutor(..., policy_gate=build_policy_gate(registry), ...)``
    exactly like ``create_curate_handlers``, satisfying
    ``tests/unit/test_policy_gate_rule.py``.
    """
    return {
        Operation.SETTINGS_UPDATE: SettingsUpdateHandler(registry),
    }


def create_classify_handlers(registry: StoreRegistry) -> dict[str, Any]:
    """Handlers for the admin-only classify-overlay write path.

    Deliberately **not** folded into :func:`create_curate_handlers`, for
    the identical reason ``create_settings_handlers`` gives: that dict
    backs the generic mutation surfaces (REST ``POST /commands/batch``,
    MCP ``execute_mutation``), which have no admin-scope gate of their
    own. Only :func:`trellis.mutate.build_classify_rules_executor` builds
    an executor from this dict, and only the admin classify-rules REST
    routes and ``trellis classify apply-rule``/``revoke-rule`` call that
    builder. Stage 2 still always runs here, same as every other executor
    in this module.
    """
    return {
        Operation.CLASSIFY_RULE_APPLY: ClassifyRuleApplyHandler(registry),
        Operation.CLASSIFY_RULE_REVOKE: ClassifyRuleRevokeHandler(registry),
    }
