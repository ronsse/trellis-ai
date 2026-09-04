"""Shared flag-gated memory-extraction hook for bulk ingest paths.

Mirrors :func:`~trellis.retrieve.embed_ingest_hook.run_embed_on_ingest`:
an opt-in, fail-soft pass that mines entity/edge drafts out of stored
document text (the deterministic ``AliasMatchExtractor`` + an LLM residue
stage via :func:`~trellis.extract.save_memory.build_save_memory_extractor`)
and routes them through the governed ``MutationExecutor``. Used by the
``trellis ingest corpus`` / ``ingest conversations`` ``--extract`` flag so
prose (people, accounts, preferences buried in notes and chat) becomes
graph structure.

**Double-gated** per ADR §5 (``adr-corpus-ingestion.md``): the caller's
explicit ``--extract`` opt-in *and* the ``TRELLIS_ENABLE_MEMORY_EXTRACTION``
env flag must both be set — at corpus scale this is a per-run LLM-cost
decision, never a default. When either is off, or no LLM client is
configured, extraction is silently skipped and ingest is unaffected.

The MCP ``save_memory`` path keeps its own cached wiring in
``trellis.mcp.server``; this is the non-MCP hook for the CLI ingest paths.
Both build the *same* extractor, so behaviour cannot drift.

Mention resolution goes through
:func:`~trellis.extract.entity_resolution.build_name_alias_resolver`: an
indexed ``entity_aliases`` read, with a bounded scan only to bootstrap a
name the index has never seen. See that module for the matching rule and
its failure modes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.write_config import MEMORY_EXTRACTION_FLAG, WriteBehaviourConfig
from trellis.extract.entity_resolution import build_name_alias_resolver

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

__all__ = [
    "MEMORY_EXTRACTION_FLAG",
    "build_memory_extractor",
    "memory_extraction_env_enabled",
    "run_memory_extraction",
]

# ``MEMORY_EXTRACTION_FLAG`` — shared with the MCP ``save_memory``
# extraction path — is re-exported from :mod:`trellis.core.write_config`,
# which owns the name and the parsing for every write-behaviour knob.

# LLM budget per document — one call, short residue prompt. Matches the
# save_memory path so cost per extracted document is identical.
_MAX_LLM_CALLS = 1
_MAX_EXTRACT_TOKENS = 400


def memory_extraction_env_enabled() -> bool:
    """``True`` iff ``TRELLIS_ENABLE_MEMORY_EXTRACTION`` is set truthy."""
    return WriteBehaviourConfig.from_env().memory_extraction


def build_memory_extractor(registry: StoreRegistry, *, opt_in: bool) -> Any | None:
    """Build the save-memory extractor, or ``None`` if extraction is off.

    Returns ``None`` — extraction silently skipped — unless **both** the
    caller's ``opt_in`` (the ``--extract`` flag) and the
    ``TRELLIS_ENABLE_MEMORY_EXTRACTION`` env flag are set, and an LLM
    client can be built from the registry. Never raises: a bulk ingest
    must not fail because the optional extractor could not be constructed.

    Note for anyone reading event provenance: the ``env_flags`` stamp
    (:mod:`trellis.core.write_provenance`) records only the env half of
    that conjunction, so ``memory_extraction: true`` on a row means the
    environment permitted extraction, not that this run performed it.
    """
    if not opt_in or not memory_extraction_env_enabled():
        return None
    try:
        llm_client = registry.build_llm_client()
    except Exception:
        logger.exception("memory_extractor_llm_build_failed")
        return None
    if llm_client is None:
        logger.info("memory_extractor_skipped_no_llm_client")
        return None
    try:
        from trellis.extract.save_memory import (  # noqa: PLC0415
            build_save_memory_extractor,
        )

        extractor = build_save_memory_extractor(
            alias_resolver=_graph_alias_resolver(registry),
            llm_client=llm_client,
        )
    except Exception:
        logger.exception("memory_extractor_build_failed")
        return None
    logger.info("memory_extractor_enabled")
    return extractor


def run_memory_extraction(
    registry: StoreRegistry,
    extractor: Any,
    doc_id: str,
    content: str,
    *,
    requested_by: str,
    participant_names: Iterable[str] = (),
) -> tuple[int, int]:
    """Mine drafts from *content* and route them through the executor.

    Every result passes through
    :func:`~trellis.extract.draft_policy.apply_memory_draft_policy`
    before conversion — participant drafts are dropped and fresh mints
    are stamped with their source ``document_ids`` plus the
    unconfirmed/mentioned claim floor (#299 / #300). ``participant_names``
    extends the built-in speaker labels for sources that know their
    speakers (the conversation reader passes its turn labels).

    Best-effort — returns ``(0, 0)`` on any failure or when *extractor*
    is ``None``. Returns ``(entities_created, edges_created)``.
    """
    if extractor is None:
        return (0, 0)
    try:
        import asyncio  # noqa: PLC0415

        from trellis.extract.commands import result_to_batch  # noqa: PLC0415
        from trellis.extract.context import ExtractionContext  # noqa: PLC0415
        from trellis.extract.draft_policy import (  # noqa: PLC0415
            apply_memory_draft_policy,
        )
        from trellis.mutate import build_curate_executor  # noqa: PLC0415

        context = ExtractionContext(
            allow_llm_fallback=True,
            max_llm_calls=_MAX_LLM_CALLS,
            max_tokens=_MAX_EXTRACT_TOKENS,
        )
        result = asyncio.run(
            extractor.extract(
                {"doc_id": doc_id, "text": content},
                source_hint="save_memory",
                context=context,
            )
        )
        result = apply_memory_draft_policy(
            result, doc_id=doc_id, participant_names=participant_names
        )
        if not result.entities and not result.edges:
            return (0, 0)

        batch = result_to_batch(result, requested_by=requested_by)
        results = build_curate_executor(registry).execute_batch(batch)
    except Exception:
        # GRACEFUL-DEGRADATION: ingest's success contract is "the document
        # is stored + MEMORY_STORED emitted". Extraction is a bonus pass;
        # its failure must never roll back a successful document write.
        logger.exception("memory_extraction_failed", doc_id=doc_id)
        return (0, 0)
    return _count_created(results)


def _count_created(results: list[Any]) -> tuple[int, int]:
    """Count SUCCESS ENTITY_CREATE / LINK_CREATE outcomes in a batch."""
    from trellis.mutate.commands import CommandStatus, Operation  # noqa: PLC0415

    entities = sum(
        1
        for r in results
        if r.operation == Operation.ENTITY_CREATE and r.status == CommandStatus.SUCCESS
    )
    edges = sum(
        1
        for r in results
        if r.operation == Operation.LINK_CREATE and r.status == CommandStatus.SUCCESS
    )
    return (entities, edges)


def _graph_alias_resolver(registry: StoreRegistry) -> Callable[[str], list[str]]:
    """Name→entity-id resolver over the graph store.

    Delegates to :func:`trellis.extract.entity_resolution.build_name_alias_resolver`,
    the same builder the MCP ``save_memory`` path uses: indexed
    ``entity_aliases`` lookup first, then a bounded read-only fallback scan.
    Governed entity writes and the governed backfill maintain the index.

    A store failure during the scan stays soft here — a bulk ingest must
    not die because one mention could not be resolved.
    """
    return build_name_alias_resolver(registry.knowledge.graph_store)
