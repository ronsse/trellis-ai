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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trellis.mutate.commands import BatchStrategy, Command, CommandBatch, Operation

if TYPE_CHECKING:
    from trellis.schemas.extraction import ExtractionResult


def result_to_batch(
    result: ExtractionResult,
    *,
    requested_by: str,
    strategy: BatchStrategy = BatchStrategy.CONTINUE_ON_ERROR,
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
    """
    commands: list[Command] = []

    for entity in result.entities:
        args: dict[str, object] = {
            "entity_type": entity.entity_type,
            "name": entity.name,
            "properties": dict(entity.properties),
            "node_role": entity.node_role.value,
        }
        if entity.entity_id is not None:
            args["entity_id"] = entity.entity_id
        if entity.generation_spec is not None:
            args["generation_spec"] = entity.generation_spec
        commands.append(
            Command(
                operation=Operation.ENTITY_CREATE,
                args=args,
                target_type="entity",
                requested_by=requested_by,
            )
        )

    commands.extend(
        Command(
            operation=Operation.LINK_CREATE,
            args={
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "edge_kind": edge.edge_kind,
                "properties": dict(edge.properties),
            },
            target_id=edge.source_id,
            target_type="entity",
            requested_by=requested_by,
        )
        for edge in result.edges
    )

    return CommandBatch(
        commands=commands,
        strategy=strategy,
        requested_by=requested_by,
    )
