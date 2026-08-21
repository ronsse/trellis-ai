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
* Optionally gated by ``TRELLIS_TRACE_EXTRACTION_MIN_CONFIDENCE`` — also
  off by default, so turning trace extraction on never *also* turns a
  silent drop on.

* Node roles are reconciled against the stored graph before submission
  (``reconcile_node_roles``) — ``node_role`` is immutable across SCD-2
  versions, so a batch that would change one is rewritten to keep the
  stored role instead of failing forever.

Returns a small summary dict (``entities`` / ``edges`` submitted counts,
``failed``, and ``executed``) so callers that want to surface extraction
telemetry can, without having to re-derive it.  When the flag is off the
hook returns ``None`` and does nothing.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from trellis.core.write_config import (
    TRACE_EXTRACTION_FLAG,
    TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG,
    WriteBehaviourConfig,
)
from trellis.extract.commands import (
    batch_draft_counts,
    reconcile_node_roles,
    result_to_batch,
)
from trellis.extract.evidence import apply_trace_evidence, parse_trace_evidence
from trellis.extract.trace import TRACE_SOURCE_HINT, TraceExtractor
from trellis.mutate.commands import CommandStatus

if TYPE_CHECKING:
    from trellis.mutate.commands import CommandBatch
    from trellis.schemas.extraction import ExtractionResult
    from trellis.schemas.trace import Trace
    from trellis.stores.registry import StoreRegistry

__all__ = [
    "TRACE_EXTRACTION_FLAG",
    "TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG",
    "batch_draft_counts",
    "extract_trace_batch",
    "run_trace_extraction",
    "trace_extraction_enabled",
    "trace_extraction_min_confidence",
]

logger = structlog.get_logger(__name__)

# ``TRACE_EXTRACTION_FLAG`` / ``TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG`` are
# re-exported from :mod:`trellis.core.write_config`, which owns the names
# and the parsing for every write-behaviour knob.

#: Command outcomes that mean "this draft did not land".
_UNSUCCESSFUL = frozenset({CommandStatus.FAILED, CommandStatus.REJECTED})

#: Cap on failure messages carried into a single log line.
_MAX_LOGGED_FAILURES = 5


def trace_extraction_enabled() -> bool:
    """``True`` iff ``TRELLIS_ENABLE_TRACE_EXTRACTION`` is set truthy."""
    return WriteBehaviourConfig.from_env().trace_extraction


def trace_extraction_min_confidence() -> float | None:
    """Confidence floor from the environment, or ``None`` for no gate.

    Unset / blank means **off**: every draft the extractor produced is
    submitted, which is what an existing deployment already gets.  A
    gate that silently drops extraction output has to be asked for.

    An unparseable or out-of-range value is treated as unset (with a
    warning) rather than as ``0.0`` — misreading "0.85" as "drop
    nothing" is recoverable, misreading it as "drop everything" is not.
    """
    return WriteBehaviourConfig.from_env().trace_extraction_min_confidence


def extract_trace_batch(
    trace: Trace,
    *,
    requested_by: str,
) -> tuple[ExtractionResult, CommandBatch | None]:
    """Extract one stored trace and build its governed batch.

    The single shared core of trace→graph extraction — the live ingest
    hook and the ``trellis extract traces`` backfill both call this, so
    the extractor wiring (``source_hint``, batch construction,
    ``requested_by`` stamping, confidence gate) cannot drift between the
    two paths.

    Returns ``(result, batch)``; ``batch`` is ``None`` when the trace
    produced no drafts.

    The deterministic evidence gate (#308) runs here, between extraction
    and batch construction: verifiable fields (files touched, files
    read, commands run) are parsed straight from the trace's tool-call
    payloads and override whatever the extractor put on the Activity
    draft — extractor-supplied values may extend the evidence, never
    contradict it.  Sitting at this seam (rather than inside
    ``TraceExtractor``) means the same guarantee holds for any future
    LLM/hybrid extractor routed through this path.
    """
    extractor = TraceExtractor()
    result = asyncio.run(
        extractor.extract(trace, source_hint=TRACE_SOURCE_HINT),
    )
    result = apply_trace_evidence(result, parse_trace_evidence(trace))
    if not result.entities and not result.edges:
        return result, None
    return result, result_to_batch(
        result,
        requested_by=requested_by,
        min_confidence=trace_extraction_min_confidence(),
    )


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
        ``{"entities": int, "edges": int, "failed": int, "executed":
        bool}``.  ``entities`` / ``edges`` count the commands *submitted*
        and ``failed`` counts those the executor rejected — the batch runs
        ``CONTINUE_ON_ERROR``, so reporting only the submitted counts
        would present a partly-failed batch as a clean one.  Any failure
        is caught, logged, and reported as ``{"entities": 0, "edges": 0,
        "failed": 0, "executed": False, "error": "..."}`` — it never
        propagates.
    """
    if not trace_extraction_enabled():
        return None

    from trellis.mutate import build_curate_executor  # noqa: PLC0415

    try:
        _result, batch = extract_trace_batch(trace, requested_by=requested_by)
        if batch is None:
            return {"entities": 0, "edges": 0, "failed": 0, "executed": False}

        reconcile_node_roles(batch, registry.knowledge.graph_store)
        entity_count, edge_count = batch_draft_counts(batch)
        results = build_curate_executor(registry).execute_batch(batch)
        failed = [r for r in results if r.status in _UNSUCCESSFUL]
    except Exception as exc:
        # GRACEFUL-DEGRADATION: trace ingest's success contract is "the
        # trace is durably stored". Trace→graph extraction is a
        # feature-flagged bonus pass; its failure must never roll back a
        # successful trace write. Logged at exception level so persistent
        # breakage is visible in stderr.
        logger.exception("trace_extraction_failed", trace_id=trace.trace_id)
        return {
            "entities": 0,
            "edges": 0,
            "failed": 0,
            "executed": False,
            "error": str(exc),
        }

    if failed:
        # CONTINUE_ON_ERROR means a rejected command is not an exception,
        # so without this the only trace of a permanently-failing draft is
        # one executor-level log line nobody reads.
        logger.warning(
            "trace_extraction_commands_failed",
            trace_id=trace.trace_id,
            failed=len(failed),
            messages=[r.message for r in failed[:_MAX_LOGGED_FAILURES]],
        )
    logger.info(
        "trace_extraction_completed",
        trace_id=trace.trace_id,
        entities=entity_count,
        edges=edge_count,
        failed=len(failed),
    )
    return {
        "entities": entity_count,
        "edges": edge_count,
        "failed": len(failed),
        "executed": True,
    }
