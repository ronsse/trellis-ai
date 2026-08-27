"""Trace → embedded summary: making traces reachable by semantic search.

``save_experience`` writes a trace and nothing else. The trace store is read by
no retrieval strategy — keyword reads documents, semantic reads vectors, graph
reads the graph — so the only surface a trace has ever had is the name-only
``trace:<id>`` Activity node trace extraction mints. Measured on the reference
deployment on 2026-08-27: **80 traces, 0 vector rows keyed to any of them.**

This worker renders each trace's authored prose into a document, embeds it, and
records the write through the governed :class:`~trellis.mutate.MutationExecutor`.
It never modifies a trace.
"""

from __future__ import annotations

from trellis_workers.trace_embed.handler import (
    TraceSummaryIngestHandler,
    build_trace_summary_command,
)
from trellis_workers.trace_embed.render import (
    DOC_ID_PREFIX,
    build_trace_metadata,
    render_trace_summary,
    trace_summary_doc_id,
)
from trellis_workers.trace_embed.watermark import TraceCursor, TraceEmbedWatermark
from trellis_workers.trace_embed.worker import (
    TraceEmbedReport,
    TraceEmbedScanLimitError,
    TraceEmbedUnavailableError,
    collect_candidates,
    default_watermark_path,
    run_trace_embed_pass,
    trace_is_embedded,
)

__all__ = [
    "DOC_ID_PREFIX",
    "TraceCursor",
    "TraceEmbedReport",
    "TraceEmbedScanLimitError",
    "TraceEmbedUnavailableError",
    "TraceEmbedWatermark",
    "TraceSummaryIngestHandler",
    "build_trace_metadata",
    "build_trace_summary_command",
    "collect_candidates",
    "default_watermark_path",
    "render_trace_summary",
    "run_trace_embed_pass",
    "trace_is_embedded",
    "trace_summary_doc_id",
]
