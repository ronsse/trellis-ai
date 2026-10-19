"""Ops helpers — raw signal recording for the feedback loop.

The ``ops`` layer is the thin wiring between tuneable components and
the ops-tier stores (:class:`OutcomeStore`, :class:`ParameterStore`,
:class:`TunerStateStore`).  Components call :func:`record_outcome` on
every governed call; tuners consume outcomes and propose parameter
updates through the governed mutation pipeline.
"""

from trellis.ops.capture_health import (
    CaptureHealthWarning,
    check_capture_health,
    format_capture_warning,
    resolve_capture_thresholds,
)
from trellis.ops.recording import record_outcome
from trellis.ops.registry import ParameterRegistry
from trellis.ops.serving_builds import ServingBuildsReport, summarize_serving_builds
from trellis.ops.write_health import (
    BackendHealthReport,
    ServeAttributionReport,
    WriteHealthReport,
    classify_rejection,
    hints_for_trace_rejections,
    record_write_rejection,
    summarize_backend_health,
    summarize_serve_attribution,
    summarize_write_health,
)

__all__ = [
    "BackendHealthReport",
    "CaptureHealthWarning",
    "ParameterRegistry",
    "ServeAttributionReport",
    "ServingBuildsReport",
    "WriteHealthReport",
    "check_capture_health",
    "classify_rejection",
    "format_capture_warning",
    "hints_for_trace_rejections",
    "record_outcome",
    "record_write_rejection",
    "resolve_capture_thresholds",
    "summarize_backend_health",
    "summarize_serve_attribution",
    "summarize_serving_builds",
    "summarize_write_health",
]
