"""Claude Code session auto-capture — client-side by design (#255, ADR #257).

A nightly sweep that reads local Claude Code transcripts, distils durable
operator memories with a local model, hard-gates them against secret leakage,
and writes survivors through the sanctioned
:func:`~trellis.ingest_corpus.sync.sync_records` seam. No transcript parser
lives in ``trellis`` core — this is client tooling in ``trellis_workers``,
per the ADR normalization boundary (clients pre-process; core caps format
handlers).

See ``docs/agent-guide/session-auto-capture.md`` for the machine-side install
runbook (systemd timer, env flags, dry-run, verification probes).
"""

from trellis_workers.session_capture.capture import (
    DEFAULT_SAMPLE_DENOMINATOR,
    DEFAULT_SOURCE_SYSTEM,
    capture_doc_id,
    capture_id_prefix,
    render_memory,
    run_capture,
)
from trellis_workers.session_capture.models import (
    CandidateMemory,
    CaptureReport,
    SessionDigest,
    ToolCall,
)

__all__ = [
    "DEFAULT_SAMPLE_DENOMINATOR",
    "DEFAULT_SOURCE_SYSTEM",
    "CandidateMemory",
    "CaptureReport",
    "SessionDigest",
    "ToolCall",
    "capture_doc_id",
    "capture_id_prefix",
    "render_memory",
    "run_capture",
]
