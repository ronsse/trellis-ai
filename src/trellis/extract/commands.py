"""Bridge extraction results into governed mutation commands.

:class:`~trellis.schemas.extraction.ExtractionResult` sits between an
extractor and the :class:`~trellis.mutate.executor.MutationExecutor`.
:func:`result_to_batch` is the canonical conversion — every consumer
(CLI, MCP, workers) should route drafts through it so the "drafts
never touch a store" rule stays intact.

Batches default to :attr:`BatchStrategy.CONTINUE_ON_ERROR` so a single
unresolved reference (e.g. a dbt model that points at a missing source)
doesn't abort the whole submission.  Callers that need
stop-on-first-error semantics can rebuild the batch with a different
strategy.

Draft ``confidence`` is persisted as the :data:`CONFIDENCE_PROPERTY`
property on the created node / edge.  It used to be dropped here, which
made every downstream confidence question unanswerable — including the
``min_confidence`` gate below, which can only be audited if the value it
filtered on also lands on the rows it kept.

:func:`batch_draft_counts` and :func:`reconcile_node_roles` are the
post-build companions: what a batch will really write, and the one
store-state check that has to happen before submitting it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from trellis.mutate.commands import BatchStrategy, Command, CommandBatch, Operation

if TYPE_CHECKING:
    from trellis.schemas.extraction import EdgeDraft, EntityDraft, ExtractionResult
    from trellis.stores.base.graph import GraphStore

logger = structlog.get_logger(__name__)

#: Property key carrying the extractor's per-draft confidence onto the
#: stored node / edge.  Namespaced with ``extraction_`` so it sits beside
#: the other property-based extraction provenance (``extractor_tier``,
#: ``source_trace_id``) instead of colliding with a domain property that
#: happens to be called ``confidence``.
CONFIDENCE_PROPERTY = "extraction_confidence"


def result_to_batch(
    result: ExtractionResult,
    *,
    requested_by: str,
    strategy: BatchStrategy = BatchStrategy.CONTINUE_ON_ERROR,
    min_confidence: float | None = None,
) -> CommandBatch:
    """Convert an :class:`ExtractionResult` into a :class:`CommandBatch`.

    Entity drafts become ``ENTITY_CREATE`` commands.  When the draft
    supplies an ``entity_id``, it's carried through so the graph node
    is deterministic; otherwise the handler assigns one.  Edge drafts
    become ``LINK_CREATE`` commands with the source entity's id as the
    ``target_id`` so the command routes to the right handler.

    Args:
        result: Output of an extractor.
        requested_by: Identifier of the caller submitting the batch
            (shows up in audit events; e.g. ``"save_memory_extractor"``
            or ``"trellis ingest dbt-manifest"``).
        strategy: Batch execution strategy.  Defaults to
            ``CONTINUE_ON_ERROR`` — individual draft failures don't
            tank the whole extraction.
        min_confidence: Optional confidence floor.  ``None`` (the
            default) means **no gate**: every draft is converted, which
            is the historical behaviour.  When set, drafts scoring below
            the floor are dropped, along with any edge left pointing at
            a dropped entity.  Opt-in on purpose — silently discarding
            extraction output on a deployment that never asked for a
            threshold is worse than passing a weak draft through.
    """
    entities = list(result.entities)
    edges = list(result.edges)
    if min_confidence is not None:
        entities, edges = _apply_confidence_gate(
            entities,
            edges,
            min_confidence=min_confidence,
            requested_by=requested_by,
        )

    commands: list[Command] = []

    for entity in entities:
        entity_props = dict(entity.properties)
        entity_props[CONFIDENCE_PROPERTY] = entity.confidence
        args: dict[str, object] = {
            "entity_type": entity.entity_type,
            "name": entity.name,
            "properties": entity_props,
            "node_role": entity.node_role.value,
        }
        if entity.entity_id is not None:
            args["entity_id"] = entity.entity_id
        if entity.generation_spec is not None:
            args["generation_spec"] = entity.generation_spec
        # Only forward a link the draft actually carries.  Omitting the arg
        # is what both entity handlers read as "leave the existing
        # graph↔document link alone"; forwarding an empty list would mean
        # "unlink", which no extractor is ever trying to say.
        if entity.document_ids is not None:
            args["document_ids"] = list(entity.document_ids)
        commands.append(
            Command(
                operation=Operation.ENTITY_CREATE,
                args=args,
                target_type="entity",
                requested_by=requested_by,
            )
        )

    for edge in edges:
        edge_props = dict(edge.properties)
        edge_props[CONFIDENCE_PROPERTY] = edge.confidence
        link_args: dict[str, object] = {
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            "edge_kind": edge.edge_kind,
            "properties": edge_props,
        }
        # Only forward the flag when set — keeps the args minimal and
        # leaves the LinkCreateHandler default (strict FK) intact for
        # the common case.
        if edge.allow_dangling:
            link_args["allow_dangling"] = True
        commands.append(
            Command(
                operation=Operation.LINK_CREATE,
                args=link_args,
                target_id=edge.source_id,
                target_type="entity",
                requested_by=requested_by,
            )
        )

    return CommandBatch(
        commands=commands,
        strategy=strategy,
        requested_by=requested_by,
    )


def _apply_confidence_gate(
    entities: list[EntityDraft],
    edges: list[EdgeDraft],
    *,
    min_confidence: float,
    requested_by: str,
) -> tuple[list[EntityDraft], list[EdgeDraft]]:
    """Drop sub-threshold drafts, plus any edge the drop would orphan.

    An edge that clears the floor but references an entity that didn't
    would otherwise be written anyway on extractors that set
    ``allow_dangling`` — the gate would have "dropped" the node while
    leaving its relationships behind.  Both halves go, or neither does.
    """
    kept_entities: list[EntityDraft] = []
    dropped_ids: set[str] = set()
    for entity in entities:
        if entity.confidence >= min_confidence:
            kept_entities.append(entity)
        elif entity.entity_id is not None:
            dropped_ids.add(entity.entity_id)

    kept_edges = [
        e
        for e in edges
        if e.confidence >= min_confidence
        and e.source_id not in dropped_ids
        and e.target_id not in dropped_ids
    ]

    dropped_entities = len(entities) - len(kept_entities)
    dropped_edges = len(edges) - len(kept_edges)
    if dropped_entities or dropped_edges:
        logger.info(
            "extraction_confidence_gate_applied",
            requested_by=requested_by,
            min_confidence=min_confidence,
            dropped_entities=dropped_entities,
            dropped_edges=dropped_edges,
        )
    return kept_entities, kept_edges


def batch_draft_counts(batch: CommandBatch | None) -> tuple[int, int]:
    """``(entities, edges)`` a batch will actually attempt to write.

    ``result.entities`` / ``result.edges`` count what the *extractor*
    produced; once a confidence gate is in play that overstates what
    survives into the batch.  Telemetry should report what was
    submitted, so count the commands.
    """
    if batch is None:
        return 0, 0
    entities = sum(1 for c in batch.commands if c.operation is Operation.ENTITY_CREATE)
    edges = sum(1 for c in batch.commands if c.operation is Operation.LINK_CREATE)
    return entities, edges


def reconcile_node_roles(batch: CommandBatch, graph_store: GraphStore) -> list[str]:
    """Keep the stored ``node_role`` where the batch would change it.

    ``node_role`` is immutable across SCD-2 versions
    (:func:`~trellis.stores.base.graph.check_node_role_immutable`), so an
    ``ENTITY_CREATE`` naming an existing node with a *different* role does
    not update it — it fails the command outright, permanently, on every
    re-extraction.  A batch built from a newer extractor therefore breaks
    against a graph an older extractor already populated: trace extraction
    started minting ``tool:<slug>`` nodes ``STRUCTURAL``, and every
    deployment that already had ``tool:bash`` as ``SEMANTIC`` would emit a
    ``FAILED`` command for it forever.

    Rewriting the arg to the stored role makes the batch succeed and leaves
    the node exactly as it was — no worse than before the role change, and
    no silent stream of failures.  Each reconciled node is logged and
    returned so an operator can build a migration list; promoting those
    nodes for real needs a delete-and-recreate, which is destructive enough
    that it must stay an explicit human decision, not a side effect of
    ingest.

    Returns the node ids left at their stored role (empty when none).
    """
    reconciled: list[str] = []
    for command in batch.commands:
        if command.operation is not Operation.ENTITY_CREATE:
            continue
        node_id = command.args.get("entity_id")
        requested = command.args.get("node_role")
        if not isinstance(node_id, str) or requested is None:
            continue
        existing = graph_store.get_node(node_id)
        if existing is None:
            continue
        stored = existing.get("node_role", "semantic")
        if stored == requested:
            continue
        command.args["node_role"] = stored
        reconciled.append(node_id)
        logger.warning(
            "extraction_node_role_conflict",
            node_id=node_id,
            stored_role=stored,
            requested_role=requested,
            requested_by=batch.requested_by,
        )
    return reconciled
