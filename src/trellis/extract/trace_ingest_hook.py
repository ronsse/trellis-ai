"""Shared post-ingest trace→graph extraction hook.

The CLI (`trellis ingest trace`), REST API (`POST /api/v1/traces`), and
MCP (`save_experience`) trace-ingest paths all want the *same* opt-in
behaviour: once a trace is durably stored, run :class:`TraceExtractor`
over it and route the resulting drafts through the governed
``MutationExecutor``.  Factoring it here keeps the three call sites from
triplicating the flag check, dispatch, and fail-soft handling — the same
way ``build_curate_executor`` is shared.

Contract (mirrors the ``save_memory`` extraction stage):

* Gated by ``TRELLIS_ENABLE_TRACE_EXTRACTION`` — off by default, so an
  existing deployment sees byte-identical behaviour.
* Runs **after** the trace is durably stored.  It only ever *reads* the
  trace; it never mutates it (traces are immutable).
* Fully best-effort: any failure is logged and swallowed.  A broken
  extraction must NEVER fail the ingest.
* Drafts go through ``result_to_batch`` → ``execute_batch`` with the
  default ``CONTINUE_ON_ERROR`` strategy.

Returns a small summary dict (``entities`` / ``edges`` draft counts plus
``executed``) so callers that want to surface extraction telemetry can,
without having to re-derive it.  When the flag is off the hook returns
``None`` and does nothing.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from trellis.extract.commands import result_to_batch
from trellis.extract.trace import TRACE_SOURCE_HINT, TraceExtractor

if TYPE_CHECKING:
    from trellis.schemas.trace import Trace
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Truthy spellings that turn the post-ingest extraction stage on.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Feature flag — off by default.
TRACE_EXTRACTION_FLAG = "TRELLIS_ENABLE_TRACE_EXTRACTION"


def trace_extraction_enabled() -> bool:
    """``True`` iff ``TRELLIS_ENABLE_TRACE_EXTRACTION`` is set truthy."""
    import os  # noqa: PLC0415

    return os.environ.get(TRACE_EXTRACTION_FLAG, "").strip().lower() in _TRUTHY


def run_trace_extraction(
    registry: StoreRegistry,
    trace: Trace,
    *,
    requested_by: str,
) -> dict[str, Any] | None:
    """Post-ingest hook: extract a stored trace into the graph.

    Args:
        registry: The active :class:`StoreRegistry`.
        trace: The trace that was **already** durably stored.  Read-only.
        requested_by: Audit identifier for the governed batch
            (e.g. ``"cli:ingest-trace"``, ``"api:ingest-trace"``,
            ``"mcp:save_experience"``).

    Returns:
        ``None`` when the feature flag is off.  Otherwise a summary dict
        ``{"entities": int, "edges": int, "executed": bool}`` describing
        the drafts produced and whether the batch was submitted.  Any
        failure is caught, logged, and reported as
        ``{"entities": 0, "edges": 0, "executed": False, "error": "..."}``
        — it never propagates.
    """
    if not trace_extraction_enabled():
        return None

    from trellis.mutate import build_curate_executor  # noqa: PLC0415

    try:
        extractor = TraceExtractor()
        result = asyncio.run(
            extractor.extract(trace, source_hint=TRACE_SOURCE_HINT),
        )
        entity_count = len(result.entities)
        edge_count = len(result.edges)
        if entity_count == 0 and edge_count == 0:
            return {"entities": 0, "edges": 0, "executed": False}

        batch = result_to_batch(result, requested_by=requested_by)
        build_curate_executor(registry).execute_batch(batch)
    except Exception as exc:
        # GRACEFUL-DEGRADATION: trace ingest's success contract is "the
        # trace is durably stored". Trace→graph extraction is a
        # feature-flagged bonus pass; its failure must never roll back a
        # successful trace write. Logged at exception level so persistent
        # breakage is visible in stderr.
        logger.exception("trace_extraction_failed", trace_id=trace.trace_id)
        return {"entities": 0, "edges": 0, "executed": False, "error": str(exc)}

    logger.info(
        "trace_extraction_completed",
        trace_id=trace.trace_id,
        entities=entity_count,
        edges=edge_count,
    )
    return {"entities": entity_count, "edges": edge_count, "executed": True}
