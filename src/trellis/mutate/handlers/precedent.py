"""Precedent promote, demote, reinstate and candidate handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from pydantic import ValidationError as PydanticValidationError

from trellis.core.document_write import is_redaction_tombstone, put_document
from trellis.core.error_sanitize import describe_validation_error
from trellis.core.precedent_document import (
    PRECEDENT_ENTITY_ID_PREFIX,
    is_promoted_precedent_document,
)
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import IdempotencyError, PolicyViolationError, ValidationError
from trellis.learning.candidate_auto_promote import AUTO_PROMOTE_REQUESTED_BY
from trellis.mutate.commands import Command, Operation
from trellis.mutate.handlers._common import (
    _current_lifecycle_state,
    _rewrite_current_node,
    _sync_vector_lifecycle,
)
from trellis.mutate.retention import CURRENT_STATE
from trellis.retrieve.precedents import is_miner_candidate_event
from trellis.schemas.classification import (
    DATA_CLASSIFICATION_KEY,
    LIFECYCLE_KEY,
    Lifecycle,
)
from trellis.schemas.precedent_record import PrecedentRecord
from trellis.stores.base.event_log import EventLog, EventType
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


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

    A re-promotion rebuilds the row's metadata from the node, so a
    ``data_classification`` label stored on the row is carried over verbatim
    (as :func:`_rewrite_precedent_document` does by keeping the whole bag):
    the node holds no label, and a rewrite that dropped it would read the row
    as ``internal`` again.

    A row that is a redaction tombstone is refused before any write
    (``precedent_document_redacted``): nothing is written and the caller emits
    no event.
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
    document_store = registry.knowledge.document_store
    stored = document_store.get(precedent_id)
    if stored is not None and is_redaction_tombstone(stored):
        # ``put_document`` would refuse this with a message about a re-ingest
        # flag that ``precedent.promote`` has no way to set. Promotion does not
        # re-ingest, so say what the row is and what can be done about it.
        msg = (
            f"precedent.promote will not write {precedent_id}: its document "
            "row is the blank row a redaction left, and promotion does not "
            "re-ingest. If its graph node still exists the redaction did not "
            "finish; re-run redaction.apply on it"
        )
        raise ValidationError(msg, errors=[msg], code="precedent_document_redacted")
    stored_label = ((stored or {}).get("metadata") or {}).get(DATA_CLASSIFICATION_KEY)
    if stored_label is not None:
        metadata[DATA_CLASSIFICATION_KEY] = stored_label
    put_document(
        document_store,
        resolve_vector_store(registry),
        precedent_id,
        content,
        metadata,
        preserve_updated_at=False,
    )
    run_embed_on_ingest(registry, precedent_id, content, metadata, source=source)


#: What :func:`_stamp_precedent_document` did to a precedent's document row.
StampOutcome = Literal["stamped", "no_row", "redaction_tombstone"]


def _stamp_precedent_document(
    registry: StoreRegistry, precedent_id: str, lifecycle: dict[str, Any]
) -> StampOutcome:
    """Stamp ``lifecycle`` onto a precedent's document row, if it has one.

    A metadata-only write that also mirrors the stamp onto the vector row
    (:func:`~trellis.core.document_write.put_document`). A precedent promoted
    before it wrote a document has none, and is not given one here
    (``"no_row"``). A row that is a redaction tombstone is not stamped either
    (``"redaction_tombstone"``): the stamp would replace the redaction's
    lifecycle and unmark it. The two skips are told apart so a caller can say
    which one it made; ``"stamped"`` means a row was written. A document-store
    failure propagates.
    """
    document_store = registry.knowledge.document_store
    document = document_store.get(precedent_id)
    if not document:
        return "no_row"
    if is_redaction_tombstone(document):
        return "redaction_tombstone"
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
    return "stamped"


#: ``event_payload["document_row"]`` when a lifecycle stamp skipped a redaction
#: tombstone. Promote's ``skipped_outside_namespace`` is the other value.
DOCUMENT_ROW_SKIPPED_TOMBSTONE = "skipped_redaction_tombstone"


def _note_skipped_tombstone(
    command: Command, precedent_id: str, *, operation: str
) -> None:
    """Record that a lifecycle stamp skipped a precedent's redacted document row.

    A warning log line (target id only) and one entry on the result's warnings.
    The caller also sets ``document_row`` on its event, so the audit log says
    it too. Nothing was written to the row.
    """
    logger.warning("precedent_document_stamp_skipped_redacted", target_id=precedent_id)
    command.metadata.setdefault("handler_warnings", []).append(
        f"precedent_document_stamp_skipped_redacted: {operation} left the "
        f"document row for {precedent_id} alone; it is the blank row a "
        "redaction left. If the precedent's graph node still exists the "
        "redaction did not finish; re-run redaction.apply on it"
    )


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
    is unchanged. A precedent-typed target whose id lies outside the
    ``precedent://learning/`` namespace gets the event and no row: the row's
    exemptions key on that namespace alone, so a row written beside it would
    be left open to the passes the exemption closes.
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
                if is_promoted_precedent_document(command.target_id):
                    _write_precedent_document(
                        self._registry,
                        command.target_id,
                        node.get("properties") or {},
                        source=command.requested_by,
                    )
                else:
                    # A row outside the namespace would not be exempt from the
                    # noise pass, retention or classify refresh, which key on
                    # the id alone. Only a hand-built command reaches here:
                    # every learning promotion mints its id in the namespace.
                    logger.warning(
                        "precedent_document_skipped_outside_namespace",
                        target_id=command.target_id,
                    )
                    # The log line is not a record: the event and the result
                    # say it too, so a reader of either learns the node has no
                    # row for keyword or semantic retrieval to serve.
                    payload["document_row"] = "skipped_outside_namespace"

        event = self._registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="mutation_executor",
            entity_id=command.target_id or trace_id,
            entity_type=args.get("entity_type", "trace"),
            payload=payload,
        )
        message = f"Precedent promoted: {args['title']}"
        if payload.get("document_row") == "skipped_outside_namespace":
            message += f" (no document row: id outside {PRECEDENT_ENTITY_ID_PREFIX})"
        return event.event_id, message


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
        stamp_outcome = _stamp_precedent_document(self._registry, target_id, lifecycle)
        if stamp_outcome == "redaction_tombstone":
            _note_skipped_tombstone(command, target_id, operation="precedent.demote")

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
        if stamp_outcome == "redaction_tombstone":
            event_payload["document_row"] = DOCUMENT_ROW_SKIPPED_TOMBSTONE
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

    A row that is a redaction tombstone is never stamped. The reinstatement
    stands, the result's warnings say the row was skipped and the event carries
    ``document_row: "skipped_redaction_tombstone"``; a resync-only repair, whose
    whole job is that stamp, is refused (``precedent_document_redacted``)
    instead.
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
    ) -> StampOutcome | Literal["failed"]:
        """Re-stamp the precedent's document row; what happened to it.

        ``"failed"`` is a store failure; the other values are
        :func:`_stamp_precedent_document`'s. After the node write a failure is
        fail-soft: the reinstatement stands, the row stays withheld, and the
        warning names the repair (re-issue the command). ``strict`` is the
        repair itself, where the node is already current and this stamp is the
        whole job, so a failure raises rather than reporting a repair that did
        not happen. A redaction tombstone is a skip that repairs nothing, so it
        raises under ``strict`` too (``precedent_document_redacted``) and is
        recorded, with the row left alone, otherwise.
        """
        try:
            outcome = _stamp_precedent_document(self._registry, target_id, lifecycle)
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
            return "failed"
        if outcome == "redaction_tombstone":
            if strict:
                msg = (
                    f"precedent.reinstate repaired nothing for {target_id}: "
                    "its document row is the blank row a redaction left, so "
                    "there is no row to re-stamp. If its graph node still "
                    "exists the redaction did not finish; re-run "
                    "redaction.apply on it"
                )
                raise ValidationError(
                    msg, errors=[msg], code="precedent_document_redacted"
                )
            _note_skipped_tombstone(command, target_id, operation="precedent.reinstate")
        return outcome

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

        document_outcome = self._restamp_document(
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
        if document_outcome == "failed":
            event_payload["document_sync_failed"] = True
        if document_outcome == "redaction_tombstone":
            event_payload["document_row"] = DOCUMENT_ROW_SKIPPED_TOMBSTONE
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


#: ``source`` stamped on a governed candidate event. It is the executor's own,
#: so a candidate is told apart from the miner's rows by event type, not source.
CANDIDATE_EVENT_SOURCE = "mutation_executor"
CANDIDATE_RECORDED_ACTION = "precedent_candidate_recorded"

#: The only ``provenance_tier`` this operation records for now. ``local_model``
#: and ``human`` are reserved by the schema for later tiers, each of which will
#: get its own caller check; until then a record naming one is refused rather
#: than recorded under a claim nothing here can back.
PERMITTED_PROVENANCE_TIERS = frozenset({"deterministic"})
#: ``local_model`` is recorded only for the callers named here, the local-model
#: distillation command (``requested_by`` is the caller's own claim, as for every
#: command; it keeps an unrelated caller from stamping a tier it did not earn).
LOCAL_MODEL_CANDIDATE_REQUESTERS = frozenset({"cli:distill-precedents-local"})
CANDIDATE_TIER_POLICY_ID = "precedent_candidate_provenance_tier"


def precedent_candidate_recorded(event_log: EventLog, candidate_id: str) -> bool:
    """Whether ``candidate_id`` already has a recorded candidate event or key."""
    if event_log.has_idempotency_key(candidate_id):
        return True
    return bool(
        event_log.get_events(
            event_type=EventType.PRECEDENT_CANDIDATE_MINED,
            entity_id=candidate_id,
            limit=1,
        )
    )


class PrecedentCandidateHandler:
    """Record one candidate precedent as a single ``PRECEDENT_CANDIDATE_MINED`` event.

    Wires :data:`Operation.PRECEDENT_CANDIDATE`. Event-only by design: no
    document, graph or vector row is written, so no retrieval surface can serve
    a candidate. ``args["record"]`` must validate as a
    :class:`~trellis.schemas.precedent_record.PrecedentRecord` whose
    ``candidate_id`` is the id its own content derives (evidence and text both),
    so a forged record cannot take the id the distiller derives for a real
    trace; the event carries the whole record. The ``deterministic`` provenance
    tier is recorded, and ``local_model`` from the local distillation command
    only; ``human`` and any other ``local_model`` caller are refused as a policy
    violation. A candidate
    already recorded is a duplicate, whether or not the caller named an
    idempotency key.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        try:
            record = PrecedentRecord.model_validate(command.args["record"])
        except PydanticValidationError as exc:
            msg = (
                "precedent.candidate record is not a valid PrecedentRecord: "
                f"{describe_validation_error(exc)}"
            )
            raise ValidationError(
                msg, errors=[msg], code="precedent_record_invalid"
            ) from exc
        local_ok = (
            record.provenance_tier == "local_model"
            and command.requested_by in LOCAL_MODEL_CANDIDATE_REQUESTERS
        )
        if record.provenance_tier not in PERMITTED_PROVENANCE_TIERS and not local_ok:
            msg = (
                "precedent.candidate records the deterministic provenance tier, "
                "and the local_model tier from the local distillation command; "
                f"{record.provenance_tier!r} records from this caller are refused"
            )
            raise PolicyViolationError(msg, policy_id=CANDIDATE_TIER_POLICY_ID)
        if record.candidate_id != record.expected_candidate_id():
            msg = "precedent.candidate candidate_id does not match the record's content"
            raise ValidationError(msg, errors=[msg], code="candidate_id_mismatch")

        event_log = self._registry.operational.event_log
        if precedent_candidate_recorded(event_log, record.candidate_id):
            raise IdempotencyError(idempotency_key=record.candidate_id)

        event_log.emit(
            EventType.PRECEDENT_CANDIDATE_MINED,
            source=CANDIDATE_EVENT_SOURCE,
            entity_id=record.candidate_id,
            entity_type="precedent_candidate",
            payload={
                "action": CANDIDATE_RECORDED_ACTION,
                "candidate_id": record.candidate_id,
                "record": record.model_dump(mode="json"),
            },
        )
        return record.candidate_id, "precedent_candidate"
