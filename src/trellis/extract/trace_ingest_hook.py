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
from trellis.mutate.commands import Operation

if TYPE_CHECKING:
    from trellis.schemas.trace import Trace
    from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)

#: Truthy spellings that turn the post-ingest extraction stage on.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Feature flag — off by default.
TRACE_EXTRACTION_FLAG = "TRELLIS_ENABLE_TRACE_EXTRACTION"

#: Optional confidence floor for the drafts this path produces.  Unset
#: (the default) means no gate — see :func:`trace_extraction_min_confidence`.
TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG = "TRELLIS_TRACE_EXTRACTION_MIN_CONFIDENCE"


def trace_extraction_enabled() -> bool:
    """``True`` iff ``TRELLIS_ENABLE_TRACE_EXTRACTION`` is set truthy."""
    import os  # noqa: PLC0415

    return os.environ.get(TRACE_EXTRACTION_FLAG, "").strip().lower() in _TRUTHY


def trace_extraction_min_confidence() -> float | None:
    """Confidence floor from the environment, or ``None`` for no gate.

    Unset / blank means **off**: every draft the extractor produced is
    submitted, which is what an existing deployment already gets.  A
    gate that silently drops extraction output has to be asked for.

    An unparseable or out-of-range value is treated as unset (with a
    warning) rather than as ``0.0`` — misreading "0.85" as "drop
    nothing" is recoverable, misreading it as "drop everything" is not.
    """
    import os  # noqa: PLC0415

    raw = os.environ.get(TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "trace_extraction_min_confidence_unparseable",
            flag=TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG,
            value=raw,
        )
        return None
    if not 0.0 <= value <= 1.0:
        logger.warning(
            "trace_extraction_min_confidence_out_of_range",
            flag=TRACE_EXTRACTION_MIN_CONFIDENCE_FLAG,
            value=value,
        )
        return None
    return value


def extract_trace_batch(
    trace: Trace,
    *,
    requested_by: str,
) -> tuple[Any, Any | None]:
    """Extract one stored trace and build its governed batch.

    The single shared core of trace→graph extraction — the live ingest
    hook and the ``trellis extract traces`` backfill both call this, so
    the extractor wiring (``source_hint``, batch construction,
    ``requested_by`` stamping, confidence gate) cannot drift between the
    two paths.

    Returns ``(result, batch)``; ``batch`` is ``None`` when the trace
    produced no drafts.
    """
    extractor = TraceExtractor()
    result = asyncio.run(
        extractor.extract(trace, source_hint=TRACE_SOURCE_HINT),
    )
    if not result.entities and not result.edges:
        return result, None
    return result, result_to_batch(
        result,
        requested_by=requested_by,
        min_confidence=trace_extraction_min_confidence(),
    )


def batch_draft_counts(batch: Any | None) -> tuple[int, int]:
    """``(entities, edges)`` a batch will actually write.

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
        _result, batch = extract_trace_batch(trace, requested_by=requested_by)
        entity_count, edge_count = batch_draft_counts(batch)
        if batch is None:
            return {"entities": 0, "edges": 0, "executed": False}

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
